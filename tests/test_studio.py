"""Real loopback HTTP, fake broker only. No credentials or real orders."""
from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from alpaca_agents.controller import RuntimeLock, _make_cycle, _run
from alpaca_agents.dashboard import AGENTS, collect
from alpaca_agents.dashboard_chat import PROMPTS, answer_for, conversation_data
from alpaca_agents.executor.client import Credentials, ExecutorError, PaperClient, TraceStore
from alpaca_agents.studio import Studio, display_snapshot
from tests.test_controller import CONTRACT, FakeBroker


class StudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rt = Path(self.tmp.name)
        self.app = Studio(self.rt).start()
        self.addCleanup(self.app.close)

    def request(self, path='/api/ask', *, body=None, method=None, headers=None):
        if body is None and path == '/api/ask':
            body = {'agent': 'houston', 'text': 'Show my positions'}
        method = method or ('POST' if body is not None else 'GET')
        raw = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else None
        h = {'X-Studio-Token': self.app.token, 'Origin': self.app.url, 'Content-Type': 'application/json'}
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        conn = http.client.HTTPConnection(self.app.host, timeout=15)
        try:
            conn.request(method, path, body=raw, headers=h)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def ask(self, text, agent='houston'):
        code, _, body = self.request(body={'agent': agent, 'text': text})
        self.assertEqual(code, 200, body)
        return json.loads(body)

    def test_bootstrap_loopback_token_csp_and_no_files_served(self):
        self.assertEqual(self.app.server.server_address[0], '127.0.0.1')
        code, headers, raw = self.request('/', headers={'X-Studio-Token': None, 'Origin': None})
        self.assertEqual(code, 200)
        self.assertIn(self.app.token, raw.decode())
        self.assertIn("connect-src 'self'", headers['Content-Security-Policy'])
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertIn("script-src 'sha256-", headers['Content-Security-Policy'])
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['X-Frame-Options'], 'DENY')
        for path in ('/orders.sqlite3', '/api/confirm', '/api/idea', '/../keys.ps1', '/?token=x'):
            code, _, raw = self.request(path)
            self.assertEqual((code, raw), (404, b''))
        self.assertEqual(list(self.rt.iterdir()), [])

    def test_token_host_origin_and_csrf_guards(self):
        for h, expected in (
            ({'X-Studio-Token': None}, 401), ({'X-Studio-Token': 'bad'}, 401),
            ({'Origin': 'http://evil.test'}, 403), ({'Origin': 'null'}, 403),
            ({'Origin': None}, 403), ({'Host': 'evil.test'}, 403),
            ({'Sec-Fetch-Site': 'cross-site'}, 403),
        ):
            self.assertEqual(self.request(headers=h)[0], expected)
        self.assertEqual(self.request('/', headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.assertEqual(self.request('/', headers={'Host': 'localhost'})[0], 403)
        self.assertEqual(self.request('/api/snapshot', headers={'X-Studio-Token': None})[0], 401)

    def test_malformed_and_oversized_requests_fail_closed(self):
        for payload, expected in ((b'x' * 8193, 413), (b'{bad', 400), (b'[]', 400),
                                  ({'agent': 'houston', 'text': 'x' * 801}, 400),
                                  ({'agent': 'unknown', 'text': 'hello'}, 400),
                                  ({'agent': 'houston', 'text': 123}, 400),
                                  ({'agent': 'houston', 'text': 'hello', 'submit': True}, 400)):
            self.assertEqual(self.request(body=payload)[0], expected)
        self.assertEqual(self.request(headers={'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.request(headers={'Transfer-Encoding': 'chunked'})[0], 400)

    def test_duplicate_headers_and_json_keys_are_rejected(self):
        for name, value in [('Host', self.app.host), ('X-Studio-Token', self.app.token),
                            ('Origin', self.app.url), ('Content-Length', '2')]:
            conn = http.client.HTTPConnection(self.app.host, timeout=5)
            try:
                conn.putrequest('POST', '/api/ask', skip_host=True)
                for key, val in {'Host': self.app.host, 'Origin': self.app.url,
                                 'X-Studio-Token': self.app.token, 'Content-Length': '2'}.items():
                    conn.putheader(key, val)
                conn.putheader(name, value)
                conn.endheaders(b'{}')
                result = conn.getresponse()
                self.assertIn(result.status, (400, 401, 403))
                result.read()
            finally:
                conn.close()
        duplicate = b'{"agent":"houston","text":"buy","text":"reconcile"}'
        self.assertEqual(self.request(body=duplicate)[0], 400)
        self.assertEqual(self.request('/', method='GET', body=b'x' * 8193)[0], 413)

    def test_global_rate_limit(self):
        for _ in range(10):
            self.assertEqual(self.request('/missing')[0], 404)
        self.assertEqual(self.request('/missing')[0], 429)

    def test_read_fresh_records_on_each_ask_and_unknown_not_zero(self):
        self.assertIn('Unknown is not zero', self.ask('positions')['text'])
        self.assertIn('DISABLED', self.ask('control mode')['text'])
        (self.rt / 'trading-control').write_text('EXITS_ONLY', encoding='utf-8')
        reply = self.ask('control mode')
        self.assertIn('currently reads EXITS_ONLY', reply['text'])
        self.assertTrue(reply['live'])
        self.assertIsNone(reply['last_cycle'])
        (self.rt / 'notifications.jsonl').write_text(json.dumps({'title': 'fresh notification'}) + '\n', encoding='utf-8')
        self.assertIn('fresh notification', self.ask('notifications')['text'])
        code, _, payload = self.request('/api/snapshot')
        self.assertEqual(code, 200)
        self.assertIsNone(json.loads(payload)['snapshot']['daily_net'])

    def test_secrets_errors_and_mutation_requests_never_reflected_or_executed(self):
        calls = []
        def fail(**kw):
            calls.append(kw)
            raise ExecutorError('api_key=TOPSECRET account_id=private-account')
        self.app.cycle = fail
        for text in ('buy 1 QQQ call', 'reconcile and submit', 'arm trading'):
            self.assertIn("can't place", self.ask(text)['text'])
        self.assertEqual(calls, [])
        self.assertIn('hidden', self.ask('api_key=TOPSECRET')['text'])
        self.assertNotIn('TOPSECRET', self.ask('reconcile')['text'])
        self.assertEqual(calls, [{'dry_run': True}])
        self.assertEqual(list(self.rt.iterdir()), [])

    def test_private_projection_excludes_nested_journal_bodies_ids_and_redacts_text(self):
        raw = collect(self.rt, now=datetime.now(timezone.utc))
        raw['live'] = [{'body': 'private-body', 'account_id': 'private-account', 'authorization_id': 'private-auth'}]
        raw['intents'] = [{'broker_order_id': 'private-order', 'reason': 'api_key=TOPSECRET'}]
        raw['order_events'] = [{'payload': 'private-payload'}]
        raw['cycles'] = [{'cycle_id': 'private-cycle', 'stages': {'reconcile': {'resolved': [{'request_id': 'private-trace'}]}}}]
        raw['notifications'] = [{'title': 'token=TOPSECRET', 'details': {'account': 'private-account'}}]
        raw['shadow'] = {'shadow': [{'thesis': 'secret=TOPSECRET', 'body': 'private-body'}]}
        with patch('alpaca_agents.studio.collect', return_value=raw):
            for route in ('/', '/api/snapshot'):
                code, _, body = self.request(route)
                self.assertEqual(code, 200)
                for secret in (b'private-body', b'private-account', b'private-auth', b'private-order', b'private-payload',
                               b'private-cycle', b'private-trace', b'TOPSECRET'):
                    self.assertNotIn(secret, body)
        self.assertNotIn('body', display_snapshot(raw)['live'][0])

    def test_cycle_and_reads_serialized_on_single_worker(self):
        started, release = threading.Event(), threading.Event()
        events, results = [], []
        def cycle(**kw):
            events.append(('cycle-start', threading.get_ident()))
            started.set()
            self.assertTrue(release.wait(5))
            events.append(('cycle-end', threading.get_ident()))
        self.app.cycle = cycle
        original = self.app.snapshot
        def snapshot():
            events.append(('snapshot', threading.get_ident()))
            return original()
        self.app.snapshot = snapshot
        a = threading.Thread(target=lambda: results.append(self.request(body={'agent': 'houston', 'text': 'reconcile'})))
        b = threading.Thread(target=lambda: results.append(self.request('/api/snapshot')))
        a.start()
        self.assertTrue(started.wait(5))
        b.start()
        time.sleep(0.1)
        self.assertEqual([e[0] for e in events], ['cycle-start'])
        release.set()
        a.join(10); b.join(10)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r[0] == 200 for r in results))
        self.assertEqual([e[0] for e in events][:2], ['cycle-start', 'cycle-end'])
        self.assertEqual(len({e[1] for e in events}), 1)

    def test_fake_broker_fill_appears_after_dry_diagnostic_without_page_reload(self):
        # Production controller factory; patches only network/data providers.
        now = datetime.now(timezone.utc)
        broker = FakeBroker(lambda: datetime.now(timezone.utc))
        client = PaperClient(Credentials('k', 's'), TraceStore(self.rt / 'api-requests.sqlite3'), opener=broker)
        args = SimpleNamespace(enable_playbook=['trend_directional'], symbols=['IWM'], session=None,
                               submit=True, serve=True, dashboard=False)
        (self.rt / 'trading-control').write_text('ARMED_PAPER', encoding='utf-8')
        (self.rt / 'playbooks').mkdir()
        (self.rt / 'playbooks/trend_directional.approved').write_text('APPROVED', encoding='utf-8')
        idea = json.loads((Path(__file__).parents[1] / 'examples/long_call.json').read_text())
        idea['playbook'] = 'trend_directional'
        providers = (lambda **kw: {'proposals': [idea]}, lambda s: {}, lambda s: {}, lambda s: {}, lambda p: None)
        with patch('alpaca_agents.executor.client.Credentials.from_environment', return_value=Credentials('k', 's')), \
             patch('alpaca_agents.executor.client.PaperClient', return_value=client), \
             patch('alpaca_agents.controller._live_providers', return_value=providers):
            one = self.app.call(lambda: _make_cycle(args, self.rt))
            self.app.call(lambda: one(now))  # Existing explicit controller path, fake POST.
            order = broker.orders['bo-1']
            broker.clock = lambda: datetime.now(timezone.utc) - timedelta(seconds=1)
            self.app.call(lambda: broker.fill(order['client_order_id'], '0.90'))
            broker.clock = lambda: datetime.now(timezone.utc)
            broker.marks[CONTRACT] = '0.40'  # Stop would fire, but chat may NOT submit it.
            before = len([x for x in broker.calls if x[0] == 'POST'])
            self.assertNotIn(CONTRACT, self.ask('positions')['text'])
            self.app.cycle = lambda **kw: one(datetime.now(timezone.utc), **kw)
            reply = self.ask('reconcile')
            self.assertIn('Dry diagnostic cycle completed', reply['text'])
            self.assertIn(CONTRACT, self.ask('positions')['text'])
            self.assertEqual(len([x for x in broker.calls if x[0] == 'POST']), before)
            saved = json.loads((self.rt / 'cycles.jsonl').read_text().splitlines()[-1])
            self.assertFalse(saved['submit'])
            self.assertEqual(saved['enabled_playbooks'], [])
            self.assertNotIn(self.app.token, (self.rt / 'dashboard.html').read_text(encoding='utf-8'))

    def test_serve_start_is_lazy_and_keeps_runtime_lock(self):
        args = SimpleNamespace(serve=True, every=None)
        def serving(rt, *, cycle, every):
            self.assertEqual(rt, self.rt)
            with self.assertRaises(ExecutorError):
                with RuntimeLock(rt):
                    pass
            return 0
        with patch('alpaca_agents.studio.serve', side_effect=serving), patch('alpaca_agents.controller._make_cycle') as factory:
            with RuntimeLock(self.rt):
                self.assertEqual(_run(args, self.rt), 0)
            factory.assert_not_called()
        self.assertFalse((self.rt / 'controller.lock').exists())

    def test_cli_rejects_external_bind_or_submit_without_scheduler(self):
        for flags in (['--serve', '--host', '0.0.0.0'], ['--serve', '--submit']):
            proc = subprocess.run([sys.executable, '-m', 'alpaca_agents.controller', '--runtime', str(self.rt), *flags],
                                  capture_output=True, timeout=10)
            self.assertEqual(proc.returncode, 2)
            self.assertFalse((self.rt / 'controller.lock').exists())

    def test_router_covers_suggestions_and_refuses_unknown_facts(self):
        context = conversation_data(collect(self.rt, now=datetime.now(timezone.utc)), AGENTS)
        for agent, prompts in PROMPTS.items():
            for question in prompts:
                self.assertNotEqual(answer_for(question, agent, context), context['topics']['help'])
        self.assertEqual(answer_for('unrecognised', 'star', context), context['topics']['help'])


class ScheduleTests(unittest.TestCase):
    def test_shutdown_waits_for_in_progress_diagnostic_before_returning(self):
        with tempfile.TemporaryDirectory() as tmp:
            entered, release, finished = threading.Event(), threading.Event(), threading.Event()
            def operation():
                entered.set()
                release.wait(5)
                finished.set()
            app = Studio(Path(tmp)).start()
            request = threading.Thread(target=lambda: app.call(operation))
            request.start()
            self.assertTrue(entered.wait(5))
            closer = threading.Thread(target=app.close)
            closer.start()
            time.sleep(0.1)
            self.assertTrue(closer.is_alive())
            self.assertFalse(finished.is_set())
            release.set()
            request.join(5)
            closer.join(5)
            self.assertTrue(finished.is_set())
            self.assertFalse(closer.is_alive())

    def test_periodic_cycles_halt_on_incident_or_closed_session_without_auto_restart(self):
        for reasons in (['MARKET_CLOSED'], ['POSITION_MISMATCH']):
            with self.subTest(reasons=reasons), tempfile.TemporaryDirectory() as tmp:
                calls = []
                def cycle(**kw):
                    calls.append(kw)
                    return {'stages': {'reconcile': {'ok': False, 'reasons': reasons}}}
                app = Studio(Path(tmp), cycle=cycle, every=0.01).start()
                try:
                    time.sleep(0.25)
                    self.assertEqual(calls, [{'dry_run': False}])
                    self.assertNotEqual(app.schedule, 'scheduled')
                finally:
                    app.close()


if __name__ == '__main__':
    unittest.main()
