"""Manual read-only connectivity check and support trace report."""
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
from .orders import OrderJournal
from .reconcile import build_risk_state


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Alpaca PAPER tools; cannot place orders")
    parser.add_argument("command", choices=("account", "traces", "import-activities", "reconcile"))
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


if __name__ == "__main__":
    raise SystemExit(main())
