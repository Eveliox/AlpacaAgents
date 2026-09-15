"""One trading cycle: reconcile -> resolve -> exits -> entries -> report.

Everything that touches the network is injected (broker client, idea provider,
close provider) so a complete cycle runs offline in tests. The controller is
the only component that holds the broker client AND the journal, and the only
one allowed to pass submit=True to submit_claimed().

Fail-closed at every stage:
- unreconciled state   -> no exits, no entries (position truth unknown)
- control != ARMED     -> no entries; EXITS_ONLY still evaluates exits
- no enabled playbooks -> ideas are still recorded as shadow, never reserved
- any exception in a provider -> that stage is skipped with an incident note
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from .executor.client import ExecutorError
from .executor.exits import HeldOption, evaluate_exit
from .executor.reconcile import build_risk_state
from .executor.submit import submit_claimed
from .gateway import control_mode
from .scanner.scan import PLAYBOOKS

APPROVAL_TOKEN = "APPROVED"


@dataclass(frozen=True)
class CycleConfig:
    submit: bool = False                       # only the CLI --submit flag sets this
    enabled_playbooks: frozenset = frozenset()
    max_entries_per_cycle: int = 1


def approved_playbooks(requested, approvals_dir: Path) -> tuple[frozenset, list]:
    """A playbook is enabled only if <approvals_dir>/<name>.approved contains exactly APPROVED."""
    enabled, refused = set(), []
    for name in requested:
        if name not in PLAYBOOKS:
            refused.append({"playbook": name, "reason": "unknown playbook"})
            continue
        marker = approvals_dir / f"{name}.approved"
        try:
            content = marker.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            refused.append({"playbook": name, "reason": f"missing approval marker {marker}"})
            continue
        if content != APPROVAL_TOKEN:
            refused.append({"playbook": name, "reason": "approval marker content is not APPROVED"})
            continue
        enabled.add(name)
    return frozenset(enabled), refused


def _append(path: Path, record: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, allow_nan=False, sort_keys=True, default=str) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _marks(positions: list) -> dict:
    marks = {}
    for p in positions:
        try:
            marks[p["symbol"]] = Decimal(str(p["current_price"]))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            continue
    return marks


def run_cycle(*, client, ledger, store, journal, notifier, ideas_provider, closes_provider,
              now: datetime, config: CycleConfig, report_path: Path) -> dict:
    cycle_id = uuid4().hex
    report = {"cycle_id": cycle_id, "started_at": now.isoformat(), "submit": config.submit is True,
              "control_mode": control_mode(journal.control_file), "enabled_playbooks": sorted(config.enabled_playbooks),
              "stages": {}}

    def finish(stage: str, **extra):
        report["stages"][stage] = extra
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        _append(report_path, report)
        return report

    # --- 1. reconcile, resolving broker-terminal intents once ----------------
    try:
        rec = build_risk_state(client, ledger, store, journal, now=now)
        resolved = []
        for item in rec.details.get("resolvable", []):
            if item.get("broker_order_id") and journal.resolve(item["authorization_id"], broker_status=item["broker_status"],
                                                               broker_order_id=item["broker_order_id"], now=now):
                resolved.append(item)
        if resolved:
            now = datetime.now(timezone.utc)
            rec = build_risk_state(client, ledger, store, journal, now=now)
    except ExecutorError as exc:
        notifier.send("incident", "Reconciliation failed", {"cycle_id": cycle_id, "error": str(exc)}, now=now)
        return finish("reconcile", ok=False, error=str(exc))
    report["stages"]["reconcile"] = {"ok": rec.state.reconciled, "reasons": list(rec.reasons), "resolved": resolved,
                                     "open_positions": rec.state.open_positions, "pending": rec.state.pending_entries,
                                     "settled_cash": str(rec.state.settled_cash),
                                     "daily_loss": str(rec.state.daily_realized_loss),
                                     "breaker": rec.state.breaker_tripped}
    if not rec.state.reconciled:
        level = "incident" if any(r.startswith(("CLAIMED_INTENT", "UNKNOWN_OPEN_ORDER", "POSITION_MISMATCH")) for r in rec.reasons) else "warning"
        notifier.send(level, "Unreconciled; cycle stopped", {"cycle_id": cycle_id, "reasons": list(rec.reasons)}, now=now)
        return finish("halt", reason="unreconciled")
    trading_day = rec.state.trading_day
    mode = control_mode(journal.control_file)

    # --- 2. exits -------------------------------------------------------------
    exits = []
    inventory = ledger.inventory()
    if inventory and mode in ("ARMED_PAPER", "EXITS_ONLY"):
        try:
            marks = _marks(client.positions())
            closes = closes_provider(sorted({row["contract"][:-15] for row in inventory}))
        except Exception as exc:  # provider failure must not crash the cycle
            marks, closes = {}, {}
            notifier.send("warning", "Exit inputs unavailable", {"cycle_id": cycle_id, "error": type(exc).__name__}, now=now)
        for row in inventory:
            contract = row["contract"]
            entry = journal.entry_for(contract)
            if entry is None:
                exits.append({"contract": contract, "note": "no journaled entry idea; manual exit only"})
                continue
            held = HeldOption(contract, row["quantity"], row["remaining_basis"], marks.get(contract),
                              closes.get(contract[:-15]), date.fromisoformat(entry["trading_day"]))
            decision, note = evaluate_exit(held, entry["idea"], trading_day=trading_day)
            record = {"contract": contract, "note": note}
            if decision is not None and config.submit is not True:
                record["decision"] = decision
                record["prepared"] = False
                record["reason"] = "DRY_RUN: exit not journaled"
            elif decision is not None:
                key = f"exit-{contract}-{trading_day.isoformat()}"
                prepared = journal.prepare_exit(key, decision, inventory=inventory, now=now, trading_day=trading_day)
                record["prepared"] = prepared["approved"]
                record["reason"] = prepared["reason"]
                if prepared["approved"]:
                    record["submission"] = submit_claimed(client, journal, prepared, now=now, submit=config.submit)
                    notifier.send("info", f"Exit {decision['exit_reason']} {contract}",
                                  {"cycle_id": cycle_id, **record["submission"]}, now=now)
            exits.append(record)
    report["stages"]["exits"] = exits

    # --- 3. entries -----------------------------------------------------------
    entries = []
    if mode != "ARMED_PAPER":
        return finish("entries", skipped=f"control mode {mode}")
    open_symbols = frozenset(row["contract"][:-15] for row in inventory) | frozenset(
        i["symbol"] for i in journal.live_intents() if i["kind"] == "entry" and i["symbol"])
    try:
        ideas = ideas_provider(open_symbols=open_symbols, trading_day=trading_day)
    except Exception as exc:
        notifier.send("warning", "Idea provider failed", {"cycle_id": cycle_id, "error": type(exc).__name__}, now=now)
        return finish("entries", error=type(exc).__name__)
    # Layer 1 is untrusted: re-apply the playbook gate and the one-idea-per-held-
    # underlying rule here regardless of what the scanner claims to have filtered.
    proposals, refused = [], []
    for idea in ideas.get("proposals", []):
        if not isinstance(idea, dict) or idea.get("playbook") not in config.enabled_playbooks:
            refused.append({"symbol": idea.get("symbol") if isinstance(idea, dict) else None, "reason": "playbook not enabled"})
        elif idea.get("symbol") in open_symbols:
            refused.append({"symbol": idea["symbol"], "reason": "underlying already held or pending"})
        else:
            proposals.append(idea)
    report["stages"]["scan"] = {"shadow": len(ideas.get("shadow", [])), "proposals": len(proposals),
                                "skipped": len(ideas.get("skipped", [])), "refused": refused}

    def provider(live_intents, now):
        # Called under the journal lock: use the journal's locked view, never reopen it.
        fresh = build_risk_state(client, ledger, store, None, live_intents=live_intents, now=now)
        return fresh.state

    for idea in sorted(proposals, key=lambda i: (-i.get("score", 0), i.get("symbol", "")))[:config.max_entries_per_cycle]:
        key = f"entry-{cycle_id}-{idea['symbol']}-{idea['playbook']}"
        reserved = journal.reserve(key, idea, state_provider=provider, now=datetime.now(timezone.utc), trading_day=trading_day)
        record = {"symbol": idea["symbol"], "playbook": idea["playbook"], "reserved": reserved["approved"], "reason": reserved["reason"]}
        if reserved["approved"] and config.submit is not True:
            # Dry run stops here: a claimed body that is never sent would become a
            # CLAIMED_INTENT_WITHOUT_BROKER_RECORD incident. The reservation expires.
            record["claimed"] = False
            record["claim_reason"] = "DRY_RUN: reservation left to expire"
        elif reserved["approved"]:
            claimed = journal.claim(reserved["authorization_id"], state_provider=provider,
                                    now=datetime.now(timezone.utc), trading_day=trading_day)
            record["claimed"] = claimed["approved"]
            record["claim_reason"] = claimed["reason"]
            if claimed["approved"]:
                record["submission"] = submit_claimed(client, journal, claimed, now=datetime.now(timezone.utc), submit=config.submit)
                notifier.send("info", f"Entry {idea['playbook']} {idea['symbol']}",
                              {"cycle_id": cycle_id, **record["submission"]}, now=now)
        entries.append(record)
    return finish("entries", entries=entries)


def previous_weekday(day: date) -> date:
    """Calendar previous weekday. Not holiday-aware: on a post-holiday day the scanner
    will find bars 'not current for session' and produce no ideas (fail closed)."""
    d = day - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _live_providers(session: date, audit_path: Path, universe):
    """Massive-backed idea and close providers. Imported lazily; tests never call this."""
    from .marketdata.client import DataCredentials, JsonlAudit, MarketDataClient, MarketDataError
    from .marketdata.snapshot import load_snapshot
    from .scanner.scan import ScanConfig, scan
    audit = JsonlAudit(audit_path)
    client = MarketDataClient(DataCredentials.from_environment(), audit=audit)
    cache = {}

    def snapshot(symbol):
        if symbol not in cache:
            now = datetime.now(timezone.utc)
            cache[symbol] = load_snapshot(client, symbol=symbol, completed_session=session, now=now,
                                          clock=lambda: datetime.now(timezone.utc)).snapshot
        return cache[symbol]

    def ideas_provider(*, open_symbols, trading_day):
        snapshots, errors = [], []
        for symbol in sorted(universe):
            try:
                snapshots.append(snapshot(symbol))
            except MarketDataError as exc:
                errors.append({"symbol": symbol, "reason": str(exc)})
        result = scan(snapshots, ScanConfig(universe=frozenset(universe), enabled_playbooks=enabled), as_of=session,
                      open_symbols=open_symbols)
        for idea in result.shadow:
            audit({"event": "shadow_idea", "timestamp": datetime.now(timezone.utc).isoformat(), "idea": idea})
        return {"proposals": result.proposals, "shadow": result.shadow, "skipped": result.skipped + errors}

    def closes_provider(symbols):
        closes = {}
        for symbol in symbols:
            try:
                bars = snapshot(symbol).bars
                if bars and bars[-1].day == session:
                    closes[symbol] = Decimal(str(bars[-1].close))
            except MarketDataError:
                continue
        return closes

    enabled = frozenset()

    def bind(playbooks):
        nonlocal enabled
        enabled = playbooks
    return ideas_provider, closes_provider, bind


def main() -> int:
    import argparse
    import sqlite3
    import sys
    from .executor.client import Credentials, PaperClient, TraceStore
    from .executor.fills import FillLedger
    from .executor.history import ActivityStore
    from .executor.orders import OrderJournal
    from .notify import Notifier
    from .scanner.scan import INDEX_ETFS

    parser = argparse.ArgumentParser(description="One PAPER trading cycle. Submits nothing unless --submit.")
    parser.add_argument("--submit", action="store_true", help="allow journal-prepared PAPER orders to be POSTed")
    parser.add_argument("--enable-playbook", action="append", default=[], metavar="NAME",
                        help="requires runtime/playbooks/NAME.approved containing APPROVED")
    parser.add_argument("--session", type=date.fromisoformat, help="last completed session (default: previous weekday)")
    parser.add_argument("--symbols", nargs="+", choices=sorted(INDEX_ETFS), default=["SPY", "QQQ", "IWM"])
    parser.add_argument("--runtime", type=Path, default=Path("runtime"))
    args = parser.parse_args()
    rt = args.runtime
    try:
        traces = TraceStore(rt / "api-requests.sqlite3")
        client = PaperClient(Credentials.from_environment(), traces)
        journal = OrderJournal(rt / "orders.sqlite3", account_id=str(client.account().get("id")),
                               control_file=rt / "trading-control")
        notifier = Notifier.from_environment(rt / "notifications.jsonl")
        enabled, refused = approved_playbooks(args.enable_playbook, rt / "playbooks")
        for item in refused:
            print(f"playbook refused: {item['playbook']}: {item['reason']}", file=sys.stderr)
        now = datetime.now(timezone.utc)
        from .executor.eastern import eastern_date
        session = args.session or previous_weekday(eastern_date(now))
        ideas_provider, closes_provider, bind = _live_providers(session, rt / "market-data.jsonl", args.symbols)
        bind(enabled)
        report = run_cycle(client=client, ledger=FillLedger(rt / "fills.sqlite3"), store=ActivityStore(rt / "activities.sqlite3"),
                           journal=journal, notifier=notifier, ideas_provider=ideas_provider, closes_provider=closes_provider,
                           now=now, config=CycleConfig(submit=args.submit, enabled_playbooks=enabled),
                           report_path=rt / "cycles.jsonl")
        print(json.dumps(report, indent=2, default=str))
        return 0 if report["stages"].get("reconcile", {}).get("ok") else 2
    except ExecutorError as exc:
        print(str(exc), file=sys.stderr)
    except (OSError, sqlite3.Error):
        print("Persistence failed; cycle stopped.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
