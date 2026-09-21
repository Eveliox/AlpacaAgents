"""Recurring, underlying-only scalp/swing research. No broker, journal or approvals.

A setup is NOT an executable option proposal. Execution eligibility is always false.
This runner may coexist with the controller; its reports and lock are separate.
"""
import argparse
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import time

from .calendar import previous_session
from .executor.eastern import eastern_date
from .marketdata.client import DataCredentials, JsonlAudit, MarketDataClient, MarketDataError, symbol_checked
from .marketdata.snapshot import normalize_bars
from .scanner import intraday
from .scanner.__main__ import _save_report
from .scanner.scan import INDEX_ETFS
from .scanner.signals import Signal, trend_signal

STYLES = ('scalp', 'swing')
MAX_SYMBOLS = 505
REPORT_LIMIT = 1024 * 1024
BASE_BLOCKERS = ['RESEARCH_ONLY', 'OPTION_CONTRACT_AND_LIQUIDITY_NOT_EVALUATED', 'NO_EXECUTION_AUTHORIZATION']


def universe(symbols):
    if not isinstance(symbols, list) or not 1 <= len(symbols) <= MAX_SYMBOLS:
        raise ValueError('Provide 1-505 explicit symbols; no implicit universe expansion')
    checked = [symbol_checked(s) for s in symbols]
    if len(set(checked)) != len(checked):
        raise ValueError('Duplicate symbols in watchlist')
    return sorted(checked)


def scan_pass(client, *, symbols, style, clock=lambda: datetime.now(timezone.utc), pause=time.sleep):
    if style not in STYLES:
        raise ValueError('Unknown research style')
    symbols = universe(symbols)
    started = clock()
    report = {'schema_version': 1, 'style': style, 'mode': 'underlying_research_only',
              'options_pnl_modelled': False, 'execution_eligible': False,
              'started_at': started.isoformat(), 'requested': len(symbols), 'rows': [], 'status': 'complete'}
    failures = 0
    for symbol in symbols:
        now = clock()
        row = {'symbol': symbol, 'execution_eligible': False, 'blockers': BASE_BLOCKERS.copy()}
        if symbol not in INDEX_ETFS:
            row['blockers'].append('EARNINGS_AND_INSTRUMENT_TYPE_UNVERIFIED')
        if style == 'scalp':
            row['blockers'].append('SCALP_EXECUTION_AND_SAME_DAY_EXIT_POLICY_NOT_IMPLEMENTED')
        try:
            if style == 'scalp':
                if not intraday.research_window(now):
                    row.update(status='skipped', reason='Outside scheduled intraday research windows')
                else:
                    payload = client.minute_bars(symbol, session=eastern_date(now))
                    finished = clock()
                    if finished < now or eastern_date(finished) != eastern_date(now):
                        raise ValueError('Clock/session changed during retrieval')
                    row.update(intraday.evaluate(payload, symbol=symbol, now=finished))
            else:
                session = previous_session(eastern_date(now))
                payload = client.daily_bars(symbol, start=session - timedelta(days=450), end=session)
                if previous_session(eastern_date(clock())) != session:
                    raise ValueError('Completed session changed during retrieval')
                bars = normalize_bars(payload, symbol=symbol, completed_session=session)
                signal = trend_signal(bars)
                row.update(bar_at=str(session), technique='daily_trend')
                if isinstance(signal, Signal):
                    values = asdict(signal)
                    row.update(status='setup', reason=values['thesis'],
                               **{k: values[k] for k in ('direction', 'entry', 'stop', 'target')})
                else:
                    row.update(status='skipped', reason=signal.reason)
            failures = 0
        except (MarketDataError, ValueError, TypeError, KeyError, OverflowError, ArithmeticError):
            # No raw provider values/exceptions in local reports or chat.
            row.update(status='error', reason='Data unavailable, malformed, stale or incomplete; setup unknown')
            failures += 1
        row['observed_at'] = clock().isoformat()
        report['rows'].append(row)
        if failures >= 3:
            report['status'] = 'partial_error'
            break  # do not hammer 500 endpoints during an outage
        pause(.25)
    report['generated_at'] = clock().isoformat()
    report['observed'] = len(report['rows'])
    return report


@contextmanager
def research_lock(folder, style):
    """Independent research-writer lock. Never removes another process's/stale lock."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'{style}.lock'
    with path.open('x', encoding='utf-8') as stream:
        stream.write(str(os.getpid()))
    try:
        yield
    finally:
        path.unlink()


def read_reports(runtime):
    """Bounded fixed-field display projection, never raw provider payloads."""
    from .dashboard_chat import redact_secrets
    result = {}
    for style in STYLES:
        try:
            with (runtime / 'research-scans' / f'{style}.json').open('rb') as stream:
                raw = stream.read(REPORT_LIMIT + 1)
            if len(raw) > REPORT_LIMIT:
                raise ValueError('Oversize')
            def unique(pairs):
                d = {}
                for key, value in pairs:
                    if key in d:
                        raise ValueError('Duplicate')
                    d[key] = value
                return d
            data = json.loads(raw, object_pairs_hook=unique)
            if (not isinstance(data, dict) or data.get('schema_version') != 1 or data.get('style') != style
                    or data.get('mode') != 'underlying_research_only' or data.get('execution_eligible') is not False
                    or data.get('options_pnl_modelled') is not False or not isinstance(data.get('rows'), list)
                    or len(data['rows']) > MAX_SYMBOLS):
                raise ValueError('Unsupported report')
            def display(v):
                if type(v) is float and not math.isfinite(v):
                    return 'Unknown'
                return redact_secrets(str(v))[:400] if type(v) in (str, int, float) else 'Unknown'
            item = {k: display(data.get(k)) for k in ('generated_at', 'started_at', 'status', 'requested', 'observed')}
            item['rows'] = [{k: display(r.get(k)) for k in
                             ('symbol', 'observed_at', 'bar_at', 'status', 'reason', 'technique', 'direction', 'entry', 'stop', 'target')}
                            for r in data['rows'] if isinstance(r, dict)]
            result[style] = item
        except (OSError, ValueError, TypeError, UnicodeError):
            result[style] = None
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='Underlying research ONLY; no orders, option selection or approvals')
    parser.add_argument('--style', choices=STYLES, required=True, help='Research profile, never a trading-mode switch')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--symbols', nargs='+', default=None)
    group.add_argument('--watchlist', type=Path, help='Local JSON array of explicit symbols (1-505); not verified S&P membership')
    parser.add_argument('--every', type=int, help='Repeat after each pass, seconds >=15 scalp / >=60 swing; no overlapping passes')
    parser.add_argument('--runtime', type=Path, default=Path('runtime'))
    args = parser.parse_args(argv)
    if args.every is not None and not (15 if args.style == 'scalp' else 60) <= args.every <= 86400:
        parser.error('Interval must be 15-86400s for scalp, 60-86400s for swing')
    try:
        symbols = args.symbols or ['SPY', 'QQQ', 'IWM']
        if args.watchlist is not None:
            with args.watchlist.open('rb') as stream:
                raw = stream.read(16385)
            if len(raw) > 16384:
                raise ValueError('Oversize watchlist')
            symbols = json.loads(raw)
        symbols = universe(symbols)
        folder = args.runtime / 'research-scans'
        with research_lock(folder, args.style):
            client = MarketDataClient(DataCredentials.from_environment(), audit=JsonlAudit(folder / f'{args.style}-data.jsonl'))
            while True:
                report = scan_pass(client, symbols=symbols, style=args.style)
                _save_report(folder / f'{args.style}.json', report)
                print(f"{args.style}: {report['observed']}/{report['requested']} observed; {report['status']}; research only", flush=True)
                if args.every is None or report['status'] == 'partial_error':
                    return 1 if any(r['status'] == 'error' for r in report['rows']) else 0
                time.sleep(args.every)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, MarketDataError):
        print('Research stopped: check credentials, watchlist, storage or an existing research lock. No trading changes.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
