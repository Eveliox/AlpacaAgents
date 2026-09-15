"""Manual read-only connectivity check and support trace report."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

from .client import Credentials, ExecutorError, PaperClient, TraceStore


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Alpaca PAPER tools; cannot place orders")
    parser.add_argument("command", choices=("account", "traces"))
    parser.add_argument("--trace-db", type=Path, default=Path("runtime/api-requests.sqlite3"))
    args = parser.parse_args()
    try:
        traces = TraceStore(args.trace_db)
        if args.command == "traces":
            print(json.dumps(traces.recent(), indent=2))
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
        print("Trace persistence failed; operation stopped. Check local storage.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
