"""Research is observational even when the separate controller is armed."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from alpaca_agents.marketdata.client import DataCredentials, MarketDataClient, MarketDataError
from alpaca_agents.research_scan import main, read_reports, research_lock, scan_pass, universe
from alpaca_agents.scanner import intraday
from alpaca_agents.rules import RiskState, evaluate
from alpaca_agents.dashboard import build, collect
from alpaca_agents.studio import display_snapshot
from tests import test_marketdata as data_fixture

NOW = datetime(2026, 9, 21, 14, 2, tzinfo=timezone.utc)


def minutes(symbol='QQQ', short=False):
    opening = NOW.replace(hour=13, minute=30)
    rows = []
    for i in range(32):
        rows.append({'t': int((opening + timedelta(minutes=i)).timestamp() * 1000),
                     'o': 100., 'h': 100.1, 'l': 99.9, 'c': 100., 'v': 20000., 'vw': 100.})
    rows[-1].update(h=100.2, c=100.15, v=40000., vw=100.1)
    if short:
        for r in rows:
            r.update(o=200-r['o'], h=200-r['l'], l=200-r['h'], c=200-r['c'], vw=200-r['vw'])
    return {'ticker': symbol, 'status': 'OK', 'adjusted': True, 'results': rows}


class IntradayTests(unittest.TestCase):
    def test_long_short_orb_are_underlying_levels_not_contracts_or_probabilities(self):
        for short in (False, True):
            result = intraday.evaluate(minutes(short=short), symbol='QQQ', now=NOW)
            self.assertEqual(result['status'], 'setup', result)
            self.assertEqual(result['technique'], 'opening_range_15m')
            self.assertEqual(result['direction'], 'short' if short else 'long')
            self.assertNotIn('confidence', result)
            self.assertNotIn('legs', result)
            self.assertAlmostEqual(abs(result['target']-result['entry'])/abs(result['entry']-result['stop']), 1.5)
            self.assertEqual(result, intraday.evaluate(minutes(short=short), symbol='QQQ', now=NOW))

    def test_vwap_reclaim_requires_fresh_cross(self):
        raw = minutes()
        for r in raw['results'][:15]:
            r.update(h=100.5, l=99.5)
        result = intraday.evaluate(raw, symbol='QQQ', now=NOW)
        self.assertEqual(result['status'], 'setup', result)
        self.assertEqual(result['technique'], 'vwap_reclaim')
        raw['results'][-2].update(c=100.05)
        self.assertIn('No fresh', intraday.evaluate(raw, symbol='QQQ', now=NOW)['reason'])

    def test_partial_bar_excluded_future_bar_rejected(self):
        raw = minutes()
        expected = intraday.evaluate(raw, symbol='QQQ', now=NOW)
        partial = deepcopy(raw['results'][-1]); partial.update(t=int(NOW.timestamp()*1000), c=999)
        raw['results'].append(partial)
        self.assertEqual(intraday.evaluate(raw, symbol='QQQ', now=NOW), expected)
        partial['t'] += 60000
        with self.assertRaises(ValueError):
            intraday.evaluate(raw, symbol='QQQ', now=NOW)

    def test_malformed_missing_duplicate_gaps_stale_and_foreign_bars_fail_closed(self):
        for kind in ('nan', 'bool', 'missing_close', 'zero_volume', 'duplicate', 'gap', 'opening', 'stale', 'symbol', 'adjusted', 'paging', 'unordered', 'ohlc'):
            raw = minutes()
            if kind == 'nan': raw['results'][5]['c'] = float('nan')
            if kind == 'bool': raw['results'][5]['v'] = True
            if kind == 'missing_close': del raw['results'][5]['c']
            if kind == 'zero_volume': raw['results'][5]['v'] = 0
            if kind == 'duplicate': raw['results'][5] = raw['results'][4]
            if kind == 'gap': del raw['results'][5]
            if kind == 'opening': raw['results'] = raw['results'][1:]
            if kind == 'stale': raw['results'] = raw['results'][:-3]
            if kind == 'symbol': raw['ticker'] = 'AAPL'
            if kind == 'adjusted': raw['adjusted'] = False
            if kind == 'paging': raw['next_url'] = 'ignored'
            if kind == 'unordered': raw['results'].reverse()
            if kind == 'ohlc': raw['results'][5]['h'] = 99
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                intraday.evaluate(raw, symbol='QQQ', now=NOW)

    def test_provider_vwap_is_ignored_and_bar_age_tolerates_feed_lag(self):
        # Live feed put a late block print in the wrong minute (vw 1.8% below the bar's low).
        # Session VWAP must come from hlc3 x volume so one such bar cannot fabricate a reclaim.
        raw = minutes()
        expected = intraday.evaluate(raw, symbol='QQQ', now=NOW)
        raw['results'][10]['vw'] = raw['results'][10]['l'] * 0.982
        self.assertEqual(intraday.evaluate(raw, symbol='QQQ', now=NOW), expected)
        for r in raw['results']:
            del r['vw']
        self.assertEqual(intraday.evaluate(raw, symbol='QQQ', now=NOW), expected)
        raw = minutes()
        self.assertEqual(intraday.evaluate(raw, symbol='QQQ', now=NOW + timedelta(seconds=110))['status'], 'setup')
        with self.assertRaises(ValueError):
            intraday.evaluate(raw, symbol='QQQ', now=NOW + timedelta(seconds=125))

    def test_volume_liquidity_and_noise_gates(self):
        raw = minutes(); raw['results'][-1]['v'] = 20000
        self.assertIn('Confirmation volume', intraday.evaluate(raw, symbol='QQQ', now=NOW)['reason'])
        raw = minutes()
        for r in raw['results']: r['v'] /= 10
        self.assertIn('liquidity floor', intraday.evaluate(raw, symbol='QQQ', now=NOW)['reason'])
        raw = minutes(); raw['results'][-1].update(c=100.7, h=100.8)
        self.assertIn('Structural stop', intraday.evaluate(raw, symbol='QQQ', now=NOW)['reason'])

    def test_session_windows_holidays_dst_and_naive_clock(self):
        for stamp in (NOW.replace(hour=13, minute=45), NOW.replace(hour=16), NOW.replace(hour=19, minute=31),
                      NOW.replace(day=20), datetime(2026, 12, 25, 15, tzinfo=timezone.utc)):
            self.assertEqual(intraday.evaluate({}, symbol='QQQ', now=stamp)['status'], 'skipped')
        self.assertTrue(intraday.research_window(datetime(2026, 12, 21, 15, 2, tzinfo=timezone.utc)))
        with self.assertRaises(ValueError):
            intraday.research_window(NOW.replace(tzinfo=None))


class ResearchRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.rt = Path(self.tmp.name)

    def run_pass(self, client=None, **kwargs):
        return scan_pass(client or Mock(minute_bars=lambda symbol, session: minutes(symbol)),
                         symbols=kwargs.pop('symbols', ['QQQ']), style=kwargs.pop('style', 'scalp'),
                         clock=kwargs.pop('clock', lambda: NOW), pause=lambda seconds: None, **kwargs)

    def test_stocks_can_be_observed_but_never_bypass_earnings_or_execution(self):
        report = self.run_pass(symbols=['AAPL', 'QQQ'])
        stock, etf = report['rows']
        self.assertEqual(stock['status'], 'setup')
        self.assertIn('EARNINGS_AND_INSTRUMENT_TYPE_UNVERIFIED', stock['blockers'])
        self.assertNotIn('EARNINGS_AND_INSTRUMENT_TYPE_UNVERIFIED', etf['blockers'])
        state = RiskState(NOW.date(), NOW, 0, 0, Decimal(0), Decimal(2000), (), reconciled=True)
        for row in report['rows']:
            self.assertFalse(row['execution_eligible'])
            self.assertFalse(evaluate(row, state, now=NOW, trading_day=NOW.date(), kill_switch=False)['approved'])
        self.assertFalse(report['options_pnl_modelled'])

    def test_swing_reuses_daily_signals_not_minute_candles(self):
        payload = data_fixture.bars_payload()
        now = data_fixture.NOW
        client = Mock(daily_bars=Mock(return_value=payload))
        report = self.run_pass(client, style='swing', symbols=['IWM'], clock=lambda: now)
        self.assertEqual(report['rows'][0]['status'], 'setup')
        self.assertEqual(report['rows'][0]['bar_at'], str(data_fixture.SESSION))
        client.minute_bars.assert_not_called()

    def test_errors_are_unknown_sanitized_and_three_failures_stop_the_sweep(self):
        client = Mock(minute_bars=Mock(side_effect=MarketDataError('api_key=SECRET')))
        report = self.run_pass(client, symbols=['AAPL', 'MSFT', 'QQQ', 'SPY'])
        self.assertEqual(report['status'], 'partial_error')
        self.assertEqual(report['requested'], 4)
        self.assertEqual(report['observed'], 3)
        self.assertTrue(all(r['status'] == 'error' for r in report['rows']))
        self.assertNotIn('SECRET', json.dumps(report))
        self.assertEqual(client.minute_bars.call_count, 3)

    def test_no_requests_outside_windows_and_no_unknown_styles(self):
        client = Mock()
        report = self.run_pass(client, clock=lambda: NOW.replace(hour=22))
        self.assertEqual(report['rows'][0]['status'], 'skipped')
        client.minute_bars.assert_not_called()
        with self.assertRaises(ValueError): self.run_pass(client, style='typo')
        for symbols in ([], ['QQQ', 'QQQ'], ['QQQ/../../'], ['aapl'], ['QQQ']*506, 'QQQ'):
            with self.assertRaises((ValueError, MarketDataError)): universe(symbols)

    def test_independent_lock_is_exclusive_and_does_not_touch_controller(self):
        (self.rt / 'controller.lock').write_text('owner', encoding='utf-8')
        with research_lock(self.rt / 'research-scans', 'scalp'):
            with self.assertRaises(FileExistsError):
                with research_lock(self.rt / 'research-scans', 'scalp'): pass
            with research_lock(self.rt / 'research-scans', 'swing'): pass
        self.assertEqual((self.rt / 'controller.lock').read_text(), 'owner')
        self.assertFalse((self.rt / 'research-scans/scalp.lock').exists())

    def test_cli_repeat_has_no_submit_or_approval_path(self):
        (self.rt / 'trading-control').write_text('ARMED_PAPER', encoding='utf-8')
        with patch('alpaca_agents.research_scan.MarketDataClient') as client, \
             patch('alpaca_agents.research_scan.DataCredentials.from_environment', return_value=DataCredentials('fake')), \
             patch('alpaca_agents.research_scan.scan_pass', return_value=self.run_pass()) as scan, \
             patch('alpaca_agents.research_scan.time.sleep', side_effect=KeyboardInterrupt):
            self.assertEqual(main(['--style', 'scalp', '--symbols', 'QQQ', '--every', '15', '--runtime', str(self.rt)]), 0)
            scan.assert_called_once()
        self.assertEqual((self.rt / 'trading-control').read_text(), 'ARMED_PAPER')
        self.assertFalse((self.rt / 'orders.sqlite3').exists())
        self.assertFalse((self.rt / 'playbooks').exists())
        self.assertFalse((self.rt / 'cycles.jsonl').exists())
        self.assertTrue((self.rt / 'research-scans/scalp.json').exists())
        for extra in (['--submit'], ['--enable-playbook', 'scalp'], ['--every', '1']):
            with self.assertRaises(SystemExit): main(['--style', 'scalp'] + extra)

    def test_report_projection_dashboard_sanitization_and_corrupt_file(self):
        report = self.run_pass()
        report['authorization_id'] = 'SECRET-AUTH'
        report['rows'][0]['reason'] = '<img src=x onerror=alert(1)> api_key=LEAKED'
        report['rows'][0]['body'] = 'SECRET-BODY'
        folder = self.rt / 'research-scans'; folder.mkdir()
        file = folder / 'scalp.json'; file.write_text(json.dumps(report), encoding='utf-8')
        projected = read_reports(self.rt)
        self.assertIsNone(projected['swing'])
        page = build(self.rt, self.rt / 'dashboard.html', now=NOW).read_text(encoding='utf-8')
        served = display_snapshot(collect(self.rt, now=NOW))
        self.assertIn('research_scans', served)
        for secret in ('SECRET-AUTH', 'SECRET-BODY', 'LEAKED'):
            self.assertNotIn(secret, json.dumps(projected))
            self.assertNotIn(secret, page)
            self.assertNotIn(secret, json.dumps(served, default=str))
        self.assertNotIn('<img src=x', page)
        self.assertIn('Research view only', page)
        file.write_text('{"broken"', encoding='utf-8')
        self.assertIsNone(read_reports(self.rt)['scalp'])
        file.write_text(json.dumps({**report, 'execution_eligible': True}), encoding='utf-8')
        self.assertIsNone(read_reports(self.rt)['scalp'])

    def test_minute_endpoint_uses_existing_allowlist_and_bounded_get(self):
        opener = data_fixture.Opener([minutes()])
        events = []
        client = MarketDataClient(DataCredentials('PRIVATE'), audit=events.append, opener=opener)
        result = client.minute_bars('QQQ', session=NOW.date())
        self.assertEqual(result['ticker'], 'QQQ')
        self.assertIn('/range/1/minute/2026-09-21/2026-09-21', opener.requests[0].full_url)
        self.assertNotIn('PRIVATE', json.dumps(events))
        with self.assertRaises(MarketDataError): client.minute_bars('QQQ', session='2026-09-21')
        with self.assertRaises(MarketDataError): client._get('/v2/aggs/ticker/QQQ/range/5/minute/2026-09-21/2026-09-21', {})
        with self.assertRaises(MarketDataError):
            bad = MarketDataClient(DataCredentials('fake'), audit=events.append, opener=data_fixture.Opener([{**minutes(), 'next_url': 'more'}]))
            bad.minute_bars('QQQ', session=NOW.date())
