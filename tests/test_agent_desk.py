"""Passive desk: evidence isolation, bounded reads, HTTP guards, no trading path."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from alpaca_agents.agent_desk import MAX_BYTES, MAX_CYCLES, MAX_ROWS, project_cycle, read_state, route
from alpaca_agents.dashboard import build
from alpaca_agents.executor.orders import OrderJournal
from alpaca_agents.rules import RiskState
from tests import test_controller as controller_fixture, test_studio as studio_fixture

NOW = datetime(2026, 9, 18, 15, tzinfo=timezone.utc)
CID = 'a' * 32
SECOND = 'b' * 32


def cycle(cid=CID, mode='ARMED_PAPER'):
    return {'cycle_id': cid, 'started_at': NOW.isoformat(), 'finished_at': NOW.isoformat(),
            'control_mode': mode, 'submit': False, 'stages': {
                'reconcile': {'ok': True, 'reasons': [], 'open_positions': 0, 'pending': 0,
                              'settled_cash': '2000', 'daily_loss': '0', 'breaker': False},
                'exits': [], 'scan': {'shadow': 1, 'proposals': 1, 'skipped': 0, 'refused': []},
                'entries': {'entries': [{'symbol': 'IWM', 'playbook': 'trend_directional',
                                         'reserved': True, 'reason': 'APPROVED', 'claimed': False,
                                         'claim_reason': 'DRY_RUN: reservation left to expire'}]}}}


class AgentDeskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rt = Path(self.tmp.name)

    def save(self, *records):
        (self.rt / 'cycles.jsonl').write_text(''.join(json.dumps(c) + '\n' for c in records), encoding='utf-8')

    def state(self):
        return read_state(self.rt, now=NOW)

    def test_missing_files_are_unknown_no_files_created_and_static_render_is_offline(self):
        state = self.state()
        self.assertEqual(state['cycles'], [])
        self.assertIn('unknown', state['warnings'][0])
        self.assertEqual(list(self.rt.iterdir()), [])
        page = build(self.rt, self.rt / 'dashboard.html', now=NOW).read_text(encoding='utf-8')
        self.assertIn('id="agent-desk"', page)
        self.assertIn('No recorded cycle', page)
        self.assertIn('not a live stage stream', page)
        self.assertEqual([p.name for p in self.rt.iterdir()], ['dashboard.html'])
        self.assertNotIn('"studio":', page)

    def test_same_cycle_id_and_no_fabricated_models_market_snapshot_or_fill(self):
        self.save(cycle())
        result = self.state()['cycles'][0]
        self.assertEqual(result['id'], CID)
        self.assertEqual(len(result['agents']), 6)
        self.assertEqual(result['status'], 'dry_run')
        self.assertIsNone(result['market_snapshot'])
        self.assertEqual(result['flow'][1]['name'], 'Existing exits')
        self.assertEqual(result['flow'][3]['name'], 'Scan & candidate gates')
        for a in result['agents']:
            self.assertIsNone(a['confidence'])
            self.assertIsNone(a['latency_ms'])
        for a in result['agents'][1:3]:
            self.assertEqual(a['status'], 'not_implemented')
        self.assertIsNone(result['entries'][0]['journal'])
        self.assertTrue(all(a['stage_timestamp'] is None for a in result['activity']))
        self.assertNotIn('executed', result['status'])

    def test_disabled_exits_only_and_halt_are_skipped_not_analyzing(self):
        for mode in ('DISABLED', 'EXITS_ONLY'):
            c = cycle(mode=mode)
            del c['stages']['scan']
            c['stages']['entries'] = {'skipped': f'control mode {mode}'}
            r = project_cycle(c, cycle_id=CID)
            self.assertEqual(r['status'], 'skipped')
            self.assertEqual(r['agents'][0]['status'], 'skipped')
            self.assertEqual(r['agents'][3]['status'], 'skipped')
            self.assertIn('does not prove', r['agents'][5]['summary'])
        c['stages'] = {'reconcile': {'ok': False, 'reasons': ['POSITION_MISMATCH']}, 'halt': {'reason': 'unreconciled'}}
        r = project_cycle(c, cycle_id=CID)
        self.assertEqual(r['status'], 'halted')
        self.assertEqual(r['agents'][0]['status'], 'skipped')
        self.assertIn('POSITION_MISMATCH', json.dumps(r))

    def test_missing_malformed_and_unknown_outcomes_never_become_pass_or_filled(self):
        c = cycle()
        c['stages']['entries'] = {'error': 'Options snapshot HTTP 403'}
        del c['stages']['scan']
        r = project_cycle(c, cycle_id=CID)
        self.assertEqual(r['agents'][0]['status'], 'error')
        self.assertIn('403', json.dumps(r))
        c = cycle()
        c['stages']['entries']['entries'][0].update(claimed=True, submission={'outcome': 'unknown', 'local_id': 'PRIVATE-LOCAL'})
        r = project_cycle(c, cycle_id=CID)
        self.assertEqual(r['status'], 'uncertain')
        self.assertNotIn('PRIVATE-LOCAL', json.dumps(r))
        c['stages']['entries']['entries'][0]['submission'] = {'outcome': 'submitted', 'broker_order_id': 'PRIVATE-ORDER', 'broker_status': 'accepted'}
        self.assertEqual(project_cycle(c, cycle_id=CID)['status'], 'submission_recorded')
        c['stages']['entries']['entries'][0]['reserved'] = 'true'
        self.assertEqual(project_cycle(c, cycle_id=CID)['agents'][3]['status'], 'unknown')
        c['stages']['reconcile']['ok'] = 1
        c['stages']['scan']['shadow'] = False
        r = project_cycle(c, cycle_id=CID)
        self.assertEqual(r['status'], 'unknown')
        self.assertIsNone(r['agents'][0]['outputs']['shadow'])
        c['finished_at'] = 'bad timestamp'
        c['stages']['reconcile']['ok'] = True
        self.assertEqual(project_cycle(c, cycle_id=CID)['status'], 'incomplete')

    def test_no_unrelated_scans_research_or_fills_are_joined(self):
        self.save(cycle())
        before = self.state()
        for filename in ('shadow-scan.json', 'bt-IWM.json', 'fills.sqlite3'):
            (self.rt / filename).write_text('UNRELATED-CANARY', encoding='utf-8')
        self.assertEqual(self.state(), before)
        self.assertNotIn('UNRELATED-CANARY', json.dumps(self.state()))

    def test_journal_match_is_exact_readonly_and_current_status_is_separate(self):
        idea = json.loads((Path(__file__).parents[1] / 'examples/long_call.json').read_text())
        idea['playbook'] = 'trend_directional'
        control = self.rt / 'trading-control'
        control.write_text('ARMED_PAPER', encoding='utf-8')
        journal = OrderJournal(self.rt / 'orders.sqlite3', account_id='PRIVATE-ACCOUNT', control_file=control)
        state = RiskState(NOW.date(), NOW, 0, 0, Decimal(0), Decimal(2000), (), reconciled=True)
        key = f'entry-{CID}-IWM-trend_directional'
        reservation = journal.reserve(key, idea, state_provider=lambda: state, now=NOW, trading_day=NOW.date())
        self.assertTrue(reservation['approved'], reservation)
        self.save(cycle(), cycle(SECOND))
        before = {p.name: p.read_bytes() for p in self.rt.iterdir()}
        observed = self.state()
        self.assertEqual({p.name: p.read_bytes() for p in self.rt.iterdir()}, before)
        unmatched, matched = observed['cycles']
        self.assertIsNone(unmatched['entries'][0]['journal'])
        p = matched['entries'][0]['journal']['proposal']
        self.assertEqual(p['strategy'], 'long_call')
        self.assertEqual(p['quantity'], 1)
        self.assertEqual(p['estimated_max_risk_usd'], '91.00')
        self.assertEqual(matched['entries'][0]['journal']['journal_status_now'], 'reserved')
        for secret in ('PRIVATE-ACCOUNT', reservation['authorization_id'], 'client_order_id', 'prepared_order', '"body"'):
            self.assertNotIn(secret, json.dumps(observed))
        # Time passing must not mutate/expire a reservation from the desk.
        later = read_state(self.rt, now=NOW.replace(hour=16))
        self.assertEqual(later['cursor'], observed['cursor'])
        self.assertEqual(later['cycles'][1]['entries'][0]['journal']['journal_status_now'], 'reserved')
        # The real journal, not a desk read, can later expire this reservation.
        journal.reserve('another-decision', idea, state_provider=lambda: state,
                        now=NOW + timedelta(minutes=2), trading_day=NOW.date())
        updated = self.state()
        old_entry = updated['cycles'][1]['entries'][0]
        self.assertEqual(old_entry['journal']['journal_status_now'], 'expired')
        self.assertTrue(old_entry['risk_pass_at_reservation'])  # original verdict stays historical
        self.assertEqual(updated['cycles'][1]['status'], 'dry_run')
        self.assertNotEqual(updated['cursor'], observed['cursor'])

    def test_rejection_and_exit_records_are_not_hidden_or_inferred_as_profitable(self):
        c = cycle()
        c['stages']['entries']['entries'][0].update(reserved=False, reason='MAX_RISK', claimed=None)
        c['stages']['exits'] = [{'contract': 'IWM261016C00205000', 'note': 'protective only', 'prepared': False,
                                'decision': {'exit_reason': 'premium_stop', 'quantity': 1, 'limit_price': '0.40'}}]
        r = project_cycle(c, cycle_id=CID)
        self.assertEqual(r['status'], 'rejected')
        self.assertEqual(r['agents'][3]['status'], 'rejected')
        self.assertEqual(r['exits'][0]['decision']['exit_reason'], 'premium_stop')
        self.assertIsNone(r['exits'][0]['realized_pnl'])
        self.assertIsNone(r['exits'][0]['entry_link'])

    def test_bounded_history_partial_corrupt_duplicates_and_cursor(self):
        self.save(*(cycle(f'{i:032x}') for i in range(MAX_CYCLES + 5)))
        state = self.state()
        self.assertEqual(len(state['cycles']), MAX_CYCLES)
        self.assertTrue(state['history_truncated'])
        self.assertEqual(self.state()['cursor'], state['cursor'])
        with (self.rt / 'cycles.jsonl').open('ab') as f:
            f.write(b'{"unfinished"')
        state = self.state()
        self.assertTrue(any('incomplete' in w for w in state['warnings']))
        self.assertEqual(state['cycles'][0]['id'], f'{MAX_CYCLES + 4:032x}')
        self.save(cycle(), cycle())
        self.assertEqual(self.state()['cycles'], [])
        self.assertTrue(any('Duplicate' in w for w in self.state()['warnings']))
        (self.rt / 'cycles.jsonl').write_bytes(b'x' * (MAX_BYTES + 100) + b'\n' + json.dumps(cycle()).encode() + b'\nnull\n[]\n')
        state = self.state()
        self.assertEqual(len(state['cycles']), 1)
        self.assertTrue(state['history_truncated'])
        self.assertTrue(any('Malformed' in w for w in state['warnings']))

    def test_duplicate_record_keys_are_not_last_value_wins(self):
        record = json.dumps(cycle()).replace('"ok": true', '"ok": false, "ok": true')
        (self.rt / 'cycles.jsonl').write_text(record + '\n', encoding='utf-8')
        state = self.state()
        self.assertEqual(state['cycles'], [])
        self.assertTrue(any('Malformed' in w for w in state['warnings']))

    def test_private_fields_redacted_and_malformed_numbers_are_unknown(self):
        c = cycle()
        c['stages']['reconcile'].update(settled_cash='Infinity', daily_loss='NaN', resolved=[{'authorization_id': 'PRIVATE-AUTH'}])
        c['stages']['entries']['entries'][0].update(reason='api_key=LEAKED account_id=PRIVATE-ACCOUNT', body='PRIVATE-BODY')
        c['stages']['exits'] = [{'contract': '<img src=x onerror=alert(1)>', 'note': 'secret=LEAKED'}] * (MAX_ROWS + 1)
        self.save(c)
        state = self.state()
        rendered = build(self.rt, self.rt / 'dashboard.html', now=NOW).read_text(encoding='utf-8')
        for secret in ('LEAKED', 'PRIVATE-AUTH', 'PRIVATE-ACCOUNT', 'PRIVATE-BODY'):
            self.assertNotIn(secret, json.dumps(state))
            self.assertNotIn(secret, rendered)
        self.assertNotIn('<img src=x onerror=alert(1)>', rendered)
        self.assertTrue(state['cycles'][0]['details_truncated'])
        rec = state['cycles'][0]['agents'][3]['outputs']['cycle_reconciliation']
        self.assertIsNone(rec['daily_loss'])
        self.assertIsNone(rec['settled_cash'])

    def test_old_records_get_display_identity_without_ambiguous_journal_linkage(self):
        c = cycle('not-a-controller-id')
        self.save(c)
        r = self.state()['cycles'][0]
        self.assertTrue(r['id'].startswith('legacy-'))
        self.assertIsNone(r['entries'][0]['journal'])
        self.assertNotIn('not-a-controller-id', json.dumps(self.state()))

    def test_corrupt_journal_does_not_destroy_cycle_evidence(self):
        self.save(cycle())
        (self.rt / 'orders.sqlite3').write_text('not SQLite', encoding='utf-8')
        s = self.state()
        self.assertEqual(s['cycles'][0]['id'], CID)
        self.assertIsNone(s['cycles'][0]['entries'][0]['journal'])
        self.assertTrue(any('unreadable' in w for w in s['warnings']))

    def test_real_lifecycle_projects_without_triggering_any_additional_work(self):
        f = controller_fixture.ControllerTests('test_full_lifecycle_entry_fill_hold_premium_stop_exit')
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.test_full_lifecycle_entry_fill_hold_premium_stop_exit()
        shutil.copy(f.root / 'c.jsonl', self.rt / 'cycles.jsonl')
        shutil.copy(f.root / 'o.sqlite3', self.rt / 'orders.sqlite3')
        before = {p.name: p.read_bytes() for p in self.rt.iterdir()}
        s = self.state()
        entries = [e for c in s['cycles'] for e in c['entries']]
        self.assertTrue(any(e['journal'] and e['journal']['journal_status_now'] == 'resolved:filled' for e in entries))
        self.assertTrue(any(e['submission']['outcome'] == 'submitted' for e in entries))
        self.assertTrue(any(c['exits'] for c in s['cycles']))
        self.assertEqual({p.name: p.read_bytes() for p in self.rt.iterdir()}, before)


class AgentDeskAPITests(unittest.TestCase):
    setUp = studio_fixture.StudioTests.setUp
    request = studio_fixture.StudioTests.request

    def get(self, path, **kwargs):
        # Each test case exercises many guard combinations; isolate rate-window behavior.
        with self.app.rate_lock:
            self.app.requests.clear()
        return self.request(path, **kwargs)

    def test_guarded_read_routes_no_controller_or_model_calls_no_writes(self):
        (self.rt / 'cycles.jsonl').write_text(json.dumps(cycle()) + '\n', encoding='utf-8')
        self.app.cycle = Mock(side_effect=AssertionError('must not run'))
        self.app.market = Mock(side_effect=AssertionError('must not fetch'))
        before = {p.name: p.read_bytes() for p in self.rt.iterdir()}
        state = json.loads(self.get('/api/agent-desk/state')[2])
        for path in ('/api/agent-desk/state', '/api/agent-desk/cycles', '/api/agent-desk/cycles/' + CID):
            code, headers, body = self.get(path)
            self.assertEqual(code, 200, body)
            self.assertEqual(headers['Cache-Control'], 'no-store')
            for h, status in (({'X-Studio-Token': None}, 401), ({'X-Studio-Token': 'bad'}, 401),
                              ({'Origin': 'http://evil.test'}, 403), ({'Host': 'evil.test'}, 403),
                              ({'Sec-Fetch-Site': 'cross-site'}, 403)):
                self.assertEqual(self.get(path, headers=h)[0], status)
            self.assertEqual(self.get(path, body={}, method='POST')[0], 405)
        unchanged = json.loads(self.get('/api/agent-desk/state?after=' + state['cursor'])[2])
        self.assertTrue(unchanged['unchanged'])
        self.assertNotIn('cycles', unchanged)
        self.assertEqual(self.get('/api/agent-desk/cycles/' + SECOND)[0], 404)
        for suffix in ('run', 'approve', 'reject', 'proposals/x/approve'):
            self.assertEqual(self.get('/api/agent-desk/' + suffix, body={})[0], 404)
        self.app.cycle.assert_not_called()
        self.app.market.assert_not_called()
        self.assertEqual({p.name: p.read_bytes() for p in self.rt.iterdir()}, before)

    def test_route_inputs_bounded_and_not_files(self):
        for path in ('/api/agent-desk/state?after=x', '/api/agent-desk/state?after=' + 'a' * 64 + '&after=' + 'a' * 64,
                     '/api/agent-desk/cycles/../../keys.ps1', '/api/agent-desk/state?file=keys.ps1',
                     '/api/agent-desk/cycles?limit=999999', '/api/agent-desk/state?after=' + 'x' * 9999):
            self.assertIsNone(route(path))
            self.assertEqual(self.get(path)[0], 404)

    def test_reads_use_the_same_serial_worker_and_re_read_appended_records(self):
        import threading
        import time
        entered, release = threading.Event(), threading.Event()
        events = []
        def slow():
            events.append('start')
            entered.set()
            release.wait(5)
            events.append('end')
        original = self.app.agent_desk
        def record_read():
            events.append('read')
            return original()
        self.app.agent_desk = record_read
        worker = threading.Thread(target=lambda: self.app.call(slow))
        result = []
        reader = threading.Thread(target=lambda: result.append(self.request('/api/agent-desk/state')))
        worker.start()
        self.assertTrue(entered.wait(5))
        reader.start()
        time.sleep(.1)
        self.assertEqual(events, ['start'])
        release.set()
        worker.join(5); reader.join(5)
        self.assertEqual(events, ['start', 'end', 'read'])
        first = json.loads(result[0][2])
        (self.rt / 'cycles.jsonl').write_text(json.dumps(cycle()) + '\n', encoding='utf-8')
        new = json.loads(self.get('/api/agent-desk/state?after=' + first['cursor'])[2])
        self.assertEqual(new['cycles'][0]['id'], CID)
        self.assertNotEqual(first['cursor'], new['cursor'])
