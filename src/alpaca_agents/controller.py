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
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from .calendar import previous_session
from .executor.client import ExecutorError
from .executor.exits import HeldOption, PDT_WINDOW_SESSIONS, evaluate_exit
from .executor.reconcile import ReconcileConfig, build_risk_state, config_from_runtime
from .executor.submit import submit_claimed
from .gateway import control_mode
from .scanner.scan import MANUAL_PLAYBOOK, PLAYBOOKS

APPROVAL_TOKEN = "APPROVED"
APPROVABLE = PLAYBOOKS + (MANUAL_PLAYBOOK,)


@dataclass(frozen=True)
class CycleConfig:
    submit: bool = False                       # only the CLI --submit flag sets this
    enabled_playbooks: frozenset = frozenset()
    max_entries_per_cycle: int = 1
    reconcile: ReconcileConfig = ReconcileConfig()


def approved_playbooks(requested, approvals_dir: Path) -> tuple[frozenset, list]:
    """A playbook is enabled only if <approvals_dir>/<name>.approved contains exactly APPROVED."""
    enabled, refused = set(), []
    for name in requested:
        if name not in APPROVABLE:
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


MAX_UNIVERSE = 505


def load_universe(symbols, watchlist: Path | None) -> list:
    """Explicit, validated, de-duplicated underlyings. No implicit index membership; a
    watchlist is whatever the owner put in the file, and the earnings gate still applies."""
    if watchlist is not None:
        try:
            raw = watchlist.read_bytes()
        except OSError:
            raise ValueError(f"cannot read watchlist {watchlist}") from None
        if len(raw) > 16384:
            raise ValueError("watchlist over 16 KiB")
        try:
            symbols = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("watchlist must be a JSON array of symbols") from None
    if symbols is None:
        symbols = ["SPY", "QQQ", "IWM"]
    if not isinstance(symbols, list) or not 1 <= len(symbols) <= MAX_UNIVERSE:
        raise ValueError(f"1-{MAX_UNIVERSE} symbols required")
    checked = []
    for s in symbols:
        if not isinstance(s, str) or not re.fullmatch(r"[A-Z]{1,6}", s):
            raise ValueError(f"invalid symbol {s!r}: 1-6 uppercase letters")
        if s not in checked:
            checked.append(s)
    return checked


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
              now: datetime, config: CycleConfig, report_path: Path, bars_provider=None, bids_provider=None) -> dict:
    """closes_provider(symbols)   -> {symbol: Decimal close of the last completed session}
    bars_provider(symbols)     -> {symbol: ascending Bar tuple ending on that session} (optional)
    bids_provider(contracts)   -> {contract: fresh NBBO bid Decimal} (optional; exits fall back to 95% of mark)"""
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
        rec = build_risk_state(client, ledger, store, journal, now=now, config=config.reconcile)
        resolved = []
        for item in rec.details.get("resolvable", []):
            if item.get("broker_order_id") and journal.resolve(item["authorization_id"], broker_status=item["broker_status"],
                                                               broker_order_id=item["broker_order_id"], now=now):
                resolved.append(item)
        if resolved:
            now = datetime.now(timezone.utc)
            rec = build_risk_state(client, ledger, store, journal, now=now, config=config.reconcile)
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
            symbols = sorted({row["contract"][:-15] for row in inventory})
            closes = closes_provider(symbols)
            bars = bars_provider(symbols) if bars_provider is not None else {}
            bids = bids_provider([row["contract"] for row in inventory]) if bids_provider is not None else {}
        except Exception as exc:  # provider failure must not crash the cycle
            marks, closes, bars, bids = {}, {}, {}, {}
            notifier.send("warning", "Exit inputs unavailable", {"cycle_id": cycle_id, "error": type(exc).__name__}, now=now)
        window_start = trading_day
        for _ in range(PDT_WINDOW_SESSIONS - 1):
            window_start = previous_session(window_start)
        day_trades_used = ledger.day_trades_since(window_start)
        for row in inventory:
            contract = row["contract"]
            entry = journal.entry_for(contract)
            if entry is None:
                exits.append({"contract": contract, "note": "no journaled entry idea; manual exit only"})
                continue
            held = HeldOption(contract, row["quantity"], row["remaining_basis"], marks.get(contract),
                              closes.get(contract[:-15]), date.fromisoformat(entry["trading_day"]),
                              tuple(bars.get(contract[:-15], ())), bids.get(contract))
            decision, note = evaluate_exit(held, entry["idea"], trading_day=trading_day, day_trades_used=day_trades_used)
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
        fresh = build_risk_state(client, ledger, store, None, live_intents=live_intents, now=now, config=config.reconcile)
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
    """Most recent scheduled NYSE session before day (name kept for callers)."""
    return previous_session(day)


def _live_providers(session: date, audit_path: Path, universe, earnings_path: Path | None = None):
    """Massive-backed idea and close providers. Imported lazily; tests never call this.

    Single stocks are scanned only when the owner-verified earnings file has a valid
    entry; that lookup happens before any network call, so an unverified name costs
    nothing and is reported as skipped with the reason. Index ETFs need no entry.
    """
    from .earnings import load as load_earnings
    from .marketdata.client import DataCredentials, JsonlAudit, MarketDataClient, MarketDataError
    from .marketdata.snapshot import load_bars, load_snapshot
    from .scanner.scan import ScanConfig, earnings_exempt, scan
    audit = JsonlAudit(audit_path)
    client = MarketDataClient(DataCredentials.from_environment(), audit=audit)
    cache, bars_cache = {}, {}

    def snapshot(symbol, next_earnings=None):
        if symbol not in cache:
            now = datetime.now(timezone.utc)
            cache[symbol] = load_snapshot(client, symbol=symbol, completed_session=session, now=now,
                                          next_earnings=next_earnings, clock=lambda: datetime.now(timezone.utc)).snapshot
        return cache[symbol]

    def bars_for(symbol):
        if symbol not in bars_cache:
            bars_cache[symbol] = cache[symbol].bars if symbol in cache else load_bars(client, symbol=symbol, completed_session=session)
        return bars_cache[symbol]

    def ideas_provider(*, open_symbols, trading_day):
        snapshots, errors = [], []
        # Re-read every cycle: the owner may verify a name while the loop runs.
        calendar = load_earnings(earnings_path, today=trading_day) if earnings_path is not None else {"verified": {}, "issues": []}
        excluded = {i["symbol"]: i["reason"] for i in calendar["issues"]}
        for symbol in sorted(universe):
            if earnings_exempt(symbol):
                next_earnings = None
            elif symbol in calendar["verified"]:
                next_earnings = calendar["verified"][symbol]
            else:
                errors.append({"symbol": symbol, "playbook": "*",
                               "reason": "earnings not verified: " + excluded.get(symbol, excluded.get("*", "no entry in runtime/earnings.json"))})
                continue
            try:
                snapshots.append(snapshot(symbol, next_earnings))
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
                bars = bars_for(symbol)
                if bars and bars[-1].day == session:
                    closes[symbol] = Decimal(str(bars[-1].close))
            except MarketDataError:
                continue
        return closes

    def bars_provider(symbols):
        out = {}
        for symbol in symbols:
            try:
                bars = bars_for(symbol)
                if bars and bars[-1].day == session:
                    out[symbol] = bars
            except MarketDataError:
                continue
        return out

    def bids_provider(contracts):
        from .marketdata.snapshot import contract_bid
        out = {}
        for contract in contracts:
            bid = contract_bid(client, symbol=contract[:-15], contract=contract, now=datetime.now(timezone.utc))
            if bid is not None:
                out[contract] = bid
        return out

    enabled = frozenset()

    def bind(playbooks):
        nonlocal enabled
        enabled = playbooks
    return ideas_provider, closes_provider, bars_provider, bids_provider, bind


HALT_PREFIXES = ("CLAIMED_INTENT", "UNKNOWN_OPEN_ORDER", "POSITION_MISMATCH", "ACCOUNT_", "NOT_CASH_ACCOUNT", "HISTORY_")


def loop(one, *, every: int, log, sleep=None, clock=None) -> int:
    """Run cycles until the market closes or an incident-class halt needs a human.

    A MARKET_CLOSED-only halt ends the loop normally (exit 0). Any reason that
    implies broken state ends it with exit 3 so a scheduler can alert. Provider
    or transport errors already produce a halt stage inside run_cycle.
    """
    import time
    sleep = sleep or time.sleep
    clock = clock or (lambda: datetime.now(timezone.utc))
    cycles = 0
    while True:
        report = one(clock())
        cycles += 1
        rec = report["stages"].get("reconcile", {})
        reasons = rec.get("reasons", [])
        summary = "reconciled" if rec.get("ok") else ("unreconciled: " + "; ".join(reasons) if reasons else rec.get("error", "failed"))
        log(f"cycle {cycles} {report.get('finished_at', '')}: {summary}")
        if reasons and all(r == "MARKET_CLOSED" or r.startswith("NOT_A_SESSION") for r in reasons):
            log("market closed; loop finished")
            return 0
        if any(r.startswith(HALT_PREFIXES) for r in reasons) or "error" in rec:
            log("halted: state needs a human before the next cycle")
            return 3
        sleep(every)


class RuntimeLock:
    """Exclusive lock on a runtime directory. A stale lock after a crash must be
    removed by hand: refusing to run is the fail-closed choice."""

    def __init__(self, runtime: Path):
        self.path = runtime / "controller.lock"
        self.fd = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise ExecutorError(f"Another controller holds {self.path}; remove it only if that process is gone") from None
        os.write(self.fd, f"{os.getpid()} {datetime.now(timezone.utc).isoformat()}".encode())
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            self.path.unlink()
        except OSError:
            pass
        return False


def main() -> int:
    import argparse
    import sqlite3
    import sys
    from .executor.client import Credentials, PaperClient, TraceStore
    from .executor.fills import FillLedger
    from .executor.history import ActivityStore
    from .executor.orders import OrderJournal
    from .notify import Notifier

    parser = argparse.ArgumentParser(description="One PAPER trading cycle. Submits nothing unless --submit.")
    parser.add_argument("--submit", action="store_true", help="allow journal-prepared PAPER orders to be POSTed")
    parser.add_argument("--enable-playbook", action="append", default=[], metavar="NAME",
                        help="requires runtime/playbooks/NAME.approved containing APPROVED")
    parser.add_argument("--session", type=date.fromisoformat, help="last completed session (default: previous weekday)")
    parser.add_argument("--symbols", nargs="+", default=None, metavar="SYM",
                        help="underlyings to scan (default SPY QQQ IWM). Single stocks are scanned only with a valid entry in runtime/earnings.json")
    parser.add_argument("--watchlist", type=Path, metavar="FILE",
                        help="JSON array of underlyings (1-505), used instead of --symbols; stocks still need verified earnings")
    parser.add_argument("--manual-idea", type=Path, metavar="FILE",
                        help=f"single owner draft from 'python -m alpaca_agents.manual draft'; needs --enable-playbook {MANUAL_PLAYBOOK}")
    parser.add_argument("--runtime", type=Path, default=Path("runtime"))
    parser.add_argument("--every", type=int, metavar="SECONDS",
                        help="loop: run a cycle every N seconds (>=60) until the market closes or an incident halts it")
    parser.add_argument("--dashboard", action="store_true", help="re-render runtime/dashboard.html after each cycle")
    parser.add_argument("--serve", action="store_true", help="serve the local workspace on 127.0.0.1; chat cannot submit orders")
    args = parser.parse_args()
    if args.every is not None and args.every < 60:
        parser.error("--every must be at least 60 seconds")
    if args.serve and args.submit and args.every is None:
        parser.error("--serve --submit requires --every; on-demand chat cycles are always dry")
    if args.symbols and args.watchlist:
        parser.error("use --symbols or --watchlist, not both")
    try:
        args.symbols = load_universe(args.symbols, args.watchlist)
    except ValueError as exc:
        parser.error(str(exc))
    rt = args.runtime
    try:
        with RuntimeLock(rt):
            return _run(args, rt)
    except ExecutorError as exc:
        print(str(exc), file=sys.stderr)
    except (OSError, sqlite3.Error):
        print("Persistence failed; cycle stopped.", file=sys.stderr)
    return 1


def _run(args, rt: Path) -> int:
    import sys
    if args.serve:
        from .studio import serve
        one = None

        def queued_cycle(*, dry_run):
            nonlocal one
            # Initialized on the SAME worker that later runs all cycles/reads.
            # Opening the workspace alone needs no credentials or broker call.
            if one is None:
                one = _make_cycle(args, rt)
            return one(datetime.now(timezone.utc), dry_run=dry_run)

        return serve(rt, cycle=queued_cycle, every=args.every)
    one = _make_cycle(args, rt)
    if args.every is None:
        report = one(datetime.now(timezone.utc))
        print(json.dumps(report, indent=2, default=str))
        return 0 if report["stages"].get("reconcile", {}).get("ok") else 2
    return loop(one, every=args.every, log=lambda m: print(m, file=sys.stderr))


def _make_cycle(args, rt: Path):
    import sys
    from .executor.client import Credentials, PaperClient, TraceStore
    from .executor.fills import FillLedger
    from .executor.history import ActivityStore
    from .executor.orders import OrderJournal
    from .notify import Notifier
    traces = TraceStore(rt / "api-requests.sqlite3")
    client = PaperClient(Credentials.from_environment(), traces)
    journal = OrderJournal(rt / "orders.sqlite3", account_id=str(client.account().get("id")),
                           control_file=rt / "trading-control")
    notifier = Notifier.from_environment(rt / "notifications.jsonl")
    enabled, refused = approved_playbooks(args.enable_playbook, rt / "playbooks")
    for item in refused:
        print(f"playbook refused: {item['playbook']}: {item['reason']}", file=sys.stderr)
    from .executor.eastern import eastern_date
    ledger, store = FillLedger(rt / "fills.sqlite3"), ActivityStore(rt / "activities.sqlite3")
    config = CycleConfig(submit=args.submit, enabled_playbooks=enabled, reconcile=config_from_runtime(rt))
    if config.reconcile.margin_paper_acknowledged:
        print("paper margin account acknowledged: cash semantics enforced locally", file=sys.stderr)

    def one(now, *, dry_run=False):
        providers = None

        def provider(index, *a, **kw):
            nonlocal providers
            if providers is None:
                session = args.session or previous_weekday(eastern_date(now))
                providers = _live_providers(session, rt / "market-data.jsonl", args.symbols, rt / "earnings.json")
                providers[4](enabled)
            return providers[index](*a, **kw)

        # A chat diagnostic cannot reserve/claim entries or prepare exits, even
        # if the separately scheduled controller was launched with --submit.
        effective = replace(config, submit=False, enabled_playbooks=frozenset()) if dry_run else config
        draft, manual_path = None, getattr(args, "manual_idea", None)

        def ideas(**kw):
            nonlocal draft
            result = provider(0, **kw)
            if manual_path is None or dry_run:
                return result
            # The draft joins at the front of Layer 1 like any scanner idea: Moon and the
            # journal still decide. It is used at most once, whatever the outcome.
            from .manual import load_draft
            idea, status = load_draft(manual_path, now=datetime.now(timezone.utc))
            if idea is None:
                result["skipped"] = result.get("skipped", []) + [{"symbol": None, "playbook": MANUAL_PLAYBOOK, "reason": f"manual draft: {status}"}]
                return result
            draft = status
            result["proposals"] = result.get("proposals", []) + [idea]
            return result

        report = run_cycle(client=client, ledger=ledger, store=store, journal=journal, notifier=notifier,
                           ideas_provider=ideas, closes_provider=lambda s: provider(1, s),
                           bars_provider=lambda s: provider(2, s), bids_provider=lambda s: provider(3, s),
                           now=now, config=effective, report_path=rt / "cycles.jsonl")
        if draft is not None:
            from .manual import consume
            used = consume(manual_path, draft)
            print(f"manual draft {draft} consumed -> {used}" if used else f"WARNING: could not retire manual draft {draft}; remove {manual_path} by hand", file=sys.stderr)
        if args.dashboard or args.serve:
            from .dashboard import build
            build(rt, rt / "dashboard.html", now=datetime.now(timezone.utc))
        return report

    return one


if __name__ == "__main__":
    raise SystemExit(main())
