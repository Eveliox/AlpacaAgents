"""Manual executor tools: connectivity, traces, reconciliation, and the two
human-in-the-loop commands (release-intent, flatten) the automated cycle
deliberately cannot perform on its own."""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

from .client import Credentials, ExecutorError, PaperClient, TraceStore
from .fills import FillLedger
from .history import ActivityStore, import_activities
from .exits import exit_limit
from .orders import OrderJournal
from .reconcile import build_risk_state, trading_day_from_clock
from .submit import submit_claimed
from decimal import Decimal


def main() -> int:
    parser = argparse.ArgumentParser(description="Alpaca PAPER diagnostics (read-only commands; submission lives in alpaca_agents.controller --submit)")
    parser.add_argument("command", choices=("account", "traces", "import-activities", "reconcile",
                                            "intents", "release-intent", "flatten"))
    parser.add_argument("target", nargs="?", help="authorization id (release-intent) or OCC contract (flatten)")
    parser.add_argument("--note", help="release-intent: operator note explaining the verified situation")
    parser.add_argument("--submit", action="store_true", help="flatten: actually POST the sell (default: prepare only)")
    parser.add_argument("--fills-db", type=Path, default=Path("runtime/fills.sqlite3"))
    parser.add_argument("--orders-db", type=Path, default=Path("runtime/orders.sqlite3"))
    parser.add_argument("--control-file", type=Path, default=Path("runtime/trading-control"))
    parser.add_argument("--trace-db", type=Path, default=Path("runtime/api-requests.sqlite3"))
    parser.add_argument("--history-db", type=Path, default=Path("runtime/activities.sqlite3"))
    parser.add_argument("--after", type=datetime.fromisoformat)
    parser.add_argument("--until", type=datetime.fromisoformat)
    parser.add_argument("--max-pages", type=int, default=100)
    args = parser.parse_args()
    if args.command == "import-activities" and (args.after is None or args.until is None):
        parser.error("import-activities requires explicit --after and --until timestamps")
    try:
        traces = TraceStore(args.trace_db)
        if args.command in ("intents", "release-intent", "flatten"):
            return _manual(args, traces)
        if args.command == "traces":
            print(json.dumps(traces.recent(), indent=2))
        elif args.command == "reconcile":
            client = PaperClient(Credentials.from_environment(), traces)
            journal = OrderJournal(args.orders_db, account_id=str(client.account().get("id")),
                                   control_file=args.control_file)
            result = build_risk_state(client, FillLedger(args.fills_db), ActivityStore(args.history_db), journal,
                                      now=datetime.now(timezone.utc))
            state = asdict(result.state)
            state["trading_day"] = state["trading_day"].isoformat()
            state["observed_at"] = state["observed_at"].isoformat()
            state["daily_realized_loss"] = str(state["daily_realized_loss"])
            state["settled_cash"] = str(state["settled_cash"])
            state["order_attempts"] = len(state["order_attempts"])
            print(json.dumps({"reconciled": result.state.reconciled, "reasons": list(result.reasons),
                              "state": state, "details": result.details}, indent=2, default=str))
            print("A reconciled state is a 60-second snapshot and authorizes nothing by itself.", file=sys.stderr)
            return 0 if result.state.reconciled else 2
        elif args.command == "import-activities":
            store = ActivityStore(args.history_db)
            client = PaperClient(Credentials.from_environment(), traces)
            print(json.dumps(import_activities(client, store, after=args.after,
                                              until=args.until, max_pages=args.max_pages), indent=2))
            print("Pagination exhausted is NOT verified history completeness or settled cash.", file=sys.stderr)
        else:
            account = PaperClient(Credentials.from_environment(), traces).account()
            # Do not print account identifiers or unrestricted broker payloads.
            print(json.dumps({key: account.get(key) for key in (
                "status", "currency", "equity", "cash", "options_buying_power",
                "options_approved_level", "options_trading_level", "trading_blocked")}, indent=2))
            print("Cash/buying power are broker fields, NOT verified settled cash.", file=sys.stderr)
        return 0
    except ExecutorError as exc:
        print(str(exc), file=sys.stderr)
    except (OSError, sqlite3.Error):
        print("Persistence failed; operation stopped. Check local storage.", file=sys.stderr)
    return 1


def _manual(args, traces) -> int:
    now = datetime.now(timezone.utc)
    client = PaperClient(Credentials.from_environment(), traces)
    journal = OrderJournal(args.orders_db, account_id=str(client.account().get("id")), control_file=args.control_file)
    if args.command == "intents":
        print(json.dumps(journal.live_intents(), indent=2))
        return 0
    if args.command == "release-intent":
        if not args.target or not args.note:
            print("release-intent requires <authorization_id> --note '...'", file=sys.stderr)
            return 1
        intent = next((i for i in journal.live_intents() if i["authorization_id"] == args.target), None)
        if intent is None or intent["status"] != "claimed":
            print("No claimed intent with that id", file=sys.stderr)
            return 1
        record = client.order_by_client_id(intent["client_order_id"])
        if record is not None:
            status = record.get("status")
            if status in journal.TERMINAL and isinstance(record.get("id"), str):
                journal.resolve(args.target, broker_status=status, broker_order_id=record["id"], now=now)
                print(f"Broker HAS this order (status {status}); resolved from broker status instead of releasing.")
                return 0
            print(f"Broker HAS this order and it is live (status {status}); nothing to release.", file=sys.stderr)
            return 1
        if intent["broker_order_id"] is not None:
            print("Intent was acknowledged by the broker; it must resolve from broker status, not be released.", file=sys.stderr)
            return 1
        released = journal.release_unsent(args.target, broker_lookup_local_id=client.last_not_found_trace,
                                          operator_note=args.note, now=now)
        print("released" if released else "not released (state changed)")
        return 0 if released else 1
    # flatten
    if not args.target:
        print("flatten requires <OCC contract>", file=sys.stderr)
        return 1
    ledger = FillLedger(args.fills_db)
    inventory = ledger.inventory()
    held = sum(r["quantity"] for r in inventory if r["contract"] == args.target)
    if held < 1:
        print(f"Ledger does not hold {args.target}; reconcile first if the broker does.", file=sys.stderr)
        return 1
    mark = next((Decimal(str(p["current_price"])) for p in client.positions()
                 if p.get("symbol") == args.target and p.get("current_price") is not None), None)
    if mark is None:
        print("Broker reports no mark for that contract; cannot price a limit.", file=sys.stderr)
        return 1
    day = trading_day_from_clock(client.clock())
    decision = {"action": "close", "contract": args.target, "quantity": held, "limit_price": exit_limit(mark),
                "exit_reason": "manual_flatten", "detail": f"operator flatten at mark {mark}", "trading_day": day.isoformat()}
    from uuid import uuid4
    prepared = journal.prepare_exit(f"flatten-{args.target}-{uuid4().hex}", decision,
                                    inventory=inventory, now=now, trading_day=day)
    print(json.dumps(prepared, indent=2, default=str))
    if not prepared["approved"]:
        return 1
    if not args.submit:
        # A prepared-but-unsent exit would be an incident next cycle; release it.
        journal.release_unsent(prepared["authorization_id"], broker_lookup_local_id="not-sent",
                               operator_note="flatten dry run; body never sent", now=now)
        print("Dry run: exit prepared and released. Re-run with --submit to send it.", file=sys.stderr)
        return 0
    result = submit_claimed(client, journal, prepared, now=now, submit=True)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["outcome"] == "submitted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
