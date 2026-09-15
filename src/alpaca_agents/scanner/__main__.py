"""Manual current-market SHADOW scan. Cannot enable playbooks or submit orders."""
import argparse
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile

from alpaca_agents.marketdata.client import DataCredentials, JsonlAudit, MarketDataClient, MarketDataError
from alpaca_agents.marketdata.snapshot import load_snapshot, MAX_QUOTE_AGE
from .scan import INDEX_ETFS, ScanConfig, scan


def _save_report(path: Path, report: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            name = stream.name
            json.dump(report, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if name is not None and os.path.exists(name):
            os.unlink(name)


def main() -> int:
    parser = argparse.ArgumentParser(description="Polygon/Massive shadow scanner; NO orders or playbook enablement")
    parser.add_argument("--session", type=date.fromisoformat, required=True,
                        help="Last completed US trading session (explicit until calendar integration)")
    parser.add_argument("--symbols", nargs="+", choices=sorted(INDEX_ETFS), default=["SPY", "QQQ", "IWM"])
    parser.add_argument("--audit", type=Path, default=Path("runtime/market-data.jsonl"))
    parser.add_argument("--output", type=Path, default=Path("runtime/shadow-scan.json"))
    args = parser.parse_args()
    if args.audit.resolve() == args.output.resolve():
        parser.error("audit and output paths must differ")
    try:
        audit = JsonlAudit(args.audit)
        client = MarketDataClient(DataCredentials.from_environment(), audit=audit)
        sources, loads, errors = [], [], []
        clock = lambda: datetime.now(timezone.utc)
        for symbol in sorted(set(args.symbols)):
            try:
                loaded = load_snapshot(client, symbol=symbol, completed_session=args.session, now=clock(), clock=clock)
                loads.append(loaded)
                for issue in loaded.diagnostics:
                    audit({"event": "contract_filtered", "timestamp": clock().isoformat(), **issue})
            except MarketDataError as exc:
                for issue in getattr(exc, "diagnostics", ()):
                    audit({"event": "contract_filtered", "timestamp": clock().isoformat(), **issue})
                error = {"symbol": symbol, "reason": str(exc)}
                errors.append(error)
                audit({"event": "snapshot_rejected", "timestamp": clock().isoformat(), **error})
        # Multi-symbol retrieval may take time. Recheck age before scanning.
        now, snapshots = clock(), []
        for loaded in loads:
            if (not timedelta(0) <= now - loaded.oldest_quote_at <= MAX_QUOTE_AGE
                    or loaded.snapshot.valuation_day != now.date()):
                error = {"symbol": loaded.snapshot.symbol, "reason": "Quote clock/day changed or quotes expired before scan"}
                errors.append(error)
                audit({"event": "snapshot_rejected", "timestamp": now.isoformat(), **error})
                continue
            snapshots.append(loaded.snapshot)
            sources.append({"symbol": loaded.snapshot.symbol, "observed_at": loaded.observed_at.isoformat(),
                            "oldest_quote_at": loaded.oldest_quote_at.isoformat(), "bars_session": str(args.session),
                            "valuation_day": str(loaded.snapshot.valuation_day), "provider": "massive"})
        result = scan(snapshots, ScanConfig(universe=frozenset(args.symbols)), as_of=args.session)
        for idea in result.shadow:
            audit({"event": "shadow_idea", "timestamp": now.isoformat(), "idea": idea})
        for skipped in result.skipped:
            audit({"event": "scanner_skipped", "timestamp": now.isoformat(), **skipped})
        report = {"generated_at": now.isoformat(), "mode": "shadow_only", "backtested": False,
                  "sources": sources, "errors": errors, **asdict(result)}
        _save_report(args.output, report)
        print(f"Shadow ideas: {len(result.shadow)}; snapshot errors: {len(errors)}; report: {args.output}")
        return 1 if errors else 0
    except MarketDataError as exc:
        print(str(exc), file=sys.stderr)
    except OSError:
        print("Market-data audit/report persistence failed; stopped.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
