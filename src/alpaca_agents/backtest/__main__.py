"""Underlying-level playbook replay. Produces evidence to READ, never enables anything."""
import argparse
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile

from alpaca_agents.marketdata.client import DataCredentials, JsonlAudit, MarketDataClient, MarketDataError
from .history import fetch_history, load_bars, save_bars
from .replay import HOLD_LIMIT, MAX_FILL_SESSIONS, MIN_SAMPLE, SIGNALS, outcome_dicts, replay, summarize


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            name = stream.name
            json.dump(payload, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay playbook signals over daily bars (underlying-level R only)")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--bars-file", type=Path, help="Use a saved bars JSON instead of the network")
    parser.add_argument("--save-bars", type=Path, help="Cache fetched bars here for repeatable runs")
    parser.add_argument("--playbooks", nargs="+", choices=sorted(SIGNALS), default=sorted(SIGNALS))
    parser.add_argument("--audit", type=Path, default=Path("runtime/market-data.jsonl"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or Path(f"runtime/backtest-{args.symbol}.json")
    try:
        if args.bars_file:
            bars = load_bars(args.bars_file, args.symbol)
            source = {"kind": "file", "path": str(args.bars_file)}
        else:
            if not (args.start and args.end):
                parser.error("--start and --end are required unless --bars-file is given")
            client = MarketDataClient(DataCredentials.from_environment(), audit=JsonlAudit(args.audit))
            bars = fetch_history(client, args.symbol, start=args.start, end=args.end)
            source = {"kind": "massive", "start": str(args.start), "end": str(args.end)}
            if args.save_bars:
                save_bars(args.save_bars, args.symbol, bars)
        outcomes = replay(bars, args.symbol, playbooks=tuple(args.playbooks))
        summary = summarize(outcomes)
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "symbol": args.symbol, "source": source,
            "bars": len(bars), "first_bar": bars[0].day.isoformat(), "last_bar": bars[-1].day.isoformat(),
            "level": "underlying",
            "options_pnl_modelled": False,
            "caveats": [
                "R-multiples are on the underlying price; option premium, IV, theta, spreads and fees are not modelled.",
                "Stops are close-based; gaps exit at the worse close. Same-session stop+target: the stop is assumed first.",
                "win/loss is the SIGN of R; exits{stop,target,timeout} is WHY the trade ended. They are independent.",
                "Fills already through the stop or at/beyond the target are no_trade: counted, excluded from statistics.",
                "Signals whose stop is inside 0.5 x ATR14 are skipped (live and in replay): a noise-level stop is not a swing stop.",
                "expectancy_r is a MEAN and is dominated by gap outliers; read median_r, profit_factor and max_loss_r with it.",
                f"Fills occur at the next session's open or trigger touch within {MAX_FILL_SESSIONS} sessions.",
                f"Time stops approximated in sessions: {HOLD_LIMIT}.",
                f"A playbook needs >= {MIN_SAMPLE} resolved trades before its stats mean anything.",
                "This report does not enable any playbook; enablement is a reviewed, manual decision.",
            ],
            "summary": summary,
            "trades": outcome_dicts(outcomes),
        }
        _write(output, report)
        print(f"{args.symbol}: {len(bars)} bars {bars[0].day}..{bars[-1].day}; report {output}")
        for name, stats in summary.items():
            print(f"  {name:16s} signals={stats['signals']:4d} resolved={stats['resolved']:4d} no_trade={stats['no_trade']:3d} "
                  f"win_rate={stats['win_rate']} mean_r={stats['expectancy_r']} median_r={stats['median_r']} "
                  f"profit_factor={stats['profit_factor']} max_loss_r={stats['max_loss_r']} "
                  f"exits={stats['exits']} sufficient={stats['sample_sufficient']}")
        return 0
    except MarketDataError as exc:
        print(str(exc), file=sys.stderr)
    except OSError:
        print("Persistence failed; stopped.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
