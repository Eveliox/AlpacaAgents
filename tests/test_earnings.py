"""The earnings file is a human-verified input to an existing fail-closed gate, never a bypass."""
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from alpaca_agents.controller import _live_providers, load_universe, main as controller_main
from alpaca_agents.dashboard import build, collect
from alpaca_agents.earnings import MAX_CHECK_AGE_DAYS, load, main
from alpaca_agents.marketdata.client import MarketDataError
from alpaca_agents.studio import display_snapshot

TODAY = date(2026, 9, 21)


def entry(when="2026-10-30", checked="2026-09-21", **extra):
    return {"date": when, "checked": checked, **extra}


class EarningsFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "earnings.json"

    def save(self, entries, schema=1):
        self.path.write_text(json.dumps({"schema_version": schema, "entries": entries}), encoding="utf-8")

    def test_missing_file_means_no_stocks_not_an_error(self):
        self.assertEqual(load(self.path, today=TODAY), {"verified": {}, "issues": [], "file": "missing"})

    def test_valid_entry_is_verified_and_everything_else_is_excluded_with_a_reason(self):
        self.save({
            "AAPL": entry(),
            "MSFT": entry(checked=(TODAY - timedelta(days=MAX_CHECK_AGE_DAYS + 1)).isoformat()),
            "NVDA": entry(when="2026-09-21"),
            "AMD": entry(when="2027-09-01"),
            "TSLA": entry(checked="2026-09-22"),
            "META": {"date": "10/30/2026", "checked": "2026-09-21"},
            "GOOG": "2026-10-30",
            "SPY": entry(),
            "aapl": entry(),
            "AMZN": entry(source="x" * 201),
            "NFLX": entry(source="investor.netflix.com"),
        })
        state = load(self.path, today=TODAY)
        self.assertEqual(state["verified"], {"AAPL": date(2026, 10, 30), "NFLX": date(2026, 10, 30)})
        reasons = {i["symbol"]: i["reason"] for i in state["issues"]}
        self.assertIn("re-check", reasons["MSFT"])
        self.assertIn("has passed", reasons["NVDA"])
        self.assertIn("far out", reasons["AMD"])
        self.assertIn("future", reasons["TSLA"])
        self.assertIn("YYYY-MM-DD", reasons["META"])
        self.assertIn("object", reasons["GOOG"])
        self.assertIn("no earnings", reasons["SPY"])
        self.assertIn("invalid symbol", reasons["aapl"])
        self.assertIn("source", reasons["AMZN"])

    def test_malformed_duplicate_oversize_and_wrong_schema_verify_nothing(self):
        for raw in (b'{"schema_version": 1, "entries": {"AAPL": 1, "AAPL": 2}}', b"{", b"[]", b"\xff\xfe{",
                    b'{"schema_version": 2, "entries": {}}', b'{"schema_version": 1, "entries": []}',
                    b'{"schema_version": 1, "entries": {}, "pad": "' + b"x" * 300_000 + b'"}'):
            self.path.write_bytes(raw)
            state = load(self.path, today=TODAY)
            self.assertEqual(state["verified"], {})
            self.assertEqual(state["file"], "malformed")
            self.assertEqual(state["issues"][0]["symbol"], "*")

    def test_cli_set_stamps_today_writes_utf8_and_refuses_past_dates_and_etfs(self):
        rt = Path(self.tmp.name)
        with patch("alpaca_agents.earnings.eastern_date", return_value=TODAY, create=True), \
             patch("alpaca_agents.executor.eastern.eastern_date", return_value=TODAY):
            self.assertEqual(main(["--runtime", str(rt), "set", "aapl", "2026-10-30", "--source", "investor.apple.com"]), 0)
            raw = (rt / "earnings.json").read_bytes()
            self.assertFalse(raw.startswith(b"\xff\xfe"))  # never UTF-16
            data = json.loads(raw.decode("utf-8"))
            self.assertEqual(data["entries"]["AAPL"], {"date": "2026-10-30", "checked": "2026-09-21", "source": "investor.apple.com"})
            for bad in (["set", "AAPL", "2026-09-21"], ["set", "SPY", "2026-10-30"], ["set", "AAPL", "Oct 30"], ["set", "AAPL", "2026-13-01"]):
                with self.assertRaises(SystemExit):
                    main(["--runtime", str(rt)] + bad)
            self.assertEqual(main(["--runtime", str(rt), "list"]), 0)
            self.assertEqual(main(["--runtime", str(rt), "remove", "AAPL"]), 0)
            self.assertEqual(main(["--runtime", str(rt), "remove", "AAPL"]), 1)
            self.assertEqual(json.loads((rt / "earnings.json").read_text(encoding="utf-8"))["entries"], {})
            (rt / "earnings.json").write_text('{"schema_version": 9}', encoding="utf-8")
            self.assertEqual(main(["--runtime", str(rt), "set", "AAPL", "2026-10-30"]), 1)

    def test_dashboard_shows_verified_and_excluded_without_defaulting(self):
        rt = Path(self.tmp.name)
        self.save({"AAPL": entry(), "MSFT": entry(when="2026-09-01"), "EVIL": entry(source="<img src=x onerror=alert(1)>")})
        now = datetime(2026, 9, 21, 15, tzinfo=timezone.utc)
        page = build(rt, rt / "dashboard.html", now=now).read_text(encoding="utf-8")
        self.assertIn("Verified earnings calendar", page)
        self.assertIn("2026-10-30", page)
        self.assertIn("has passed", page)
        self.assertNotIn("<img src=x", page)
        served = display_snapshot(collect(rt, now=now))
        self.assertEqual([r["symbol"] for r in served["earnings"]["verified"]], ["AAPL", "EVIL"])
        self.assertTrue(any(i["symbol"] == "MSFT" for i in served["earnings"]["issues"]))
        (rt / "earnings.json").unlink()
        page = build(rt, rt / "dashboard.html", now=now).read_text(encoding="utf-8")
        self.assertIn("No verified stocks", page)


class UniverseAndProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rt = Path(self.tmp.name)

    def test_universe_is_explicit_validated_and_bounded(self):
        self.assertEqual(load_universe(None, None), ["SPY", "QQQ", "IWM"])
        self.assertEqual(load_universe(["QQQ", "AAPL", "QQQ"], None), ["QQQ", "AAPL"])
        wl = self.rt / "wl.json"
        wl.write_text('["AAPL", "MSFT"]', encoding="utf-8")
        self.assertEqual(load_universe(None, wl), ["AAPL", "MSFT"])
        for bad in (["aapl"], ["BRK.B"], [], ["A"] * 506, ["AAPL/../x"], [1]):
            with self.assertRaises(ValueError):
                load_universe(bad, None)
        for raw in (b"{}", b"[1]", b'["x"]', b"\xff", b"[" + b'"AAPL",' * 3000 + b'"X"]'):
            wl.write_bytes(raw)
            with self.assertRaises(ValueError):
                load_universe(None, wl)
        with self.assertRaises(ValueError):
            load_universe(None, self.rt / "absent.json")

    def test_unverified_stock_is_skipped_before_any_network_call_and_verified_date_reaches_the_gate(self):
        (self.rt / "earnings.json").write_text(json.dumps({"schema_version": 1, "entries": {
            "AAPL": entry(), "MSFT": entry(when="2026-09-01")}}), encoding="utf-8")
        calls = []
        fake_snapshot = Mock()
        fake_snapshot.snapshot = Mock(symbol="AAPL", bars=(), chain=())
        def fake_load(client, *, symbol, completed_session, now, next_earnings=None, clock=None):
            calls.append((symbol, next_earnings))
            return fake_snapshot
        scanned = []
        def fake_scan(snapshots, config, *, as_of, open_symbols):
            self.assertEqual(config.enabled_playbooks, frozenset({"trend_directional"}))
            scanned.extend(s.symbol for s in snapshots)
            return Mock(proposals=[], shadow=[], skipped=[])
        with patch("alpaca_agents.marketdata.client.DataCredentials.from_environment", return_value=Mock(key="k")), \
             patch("alpaca_agents.marketdata.client.MarketDataClient"), \
             patch("alpaca_agents.marketdata.snapshot.load_snapshot", side_effect=fake_load), \
             patch("alpaca_agents.scanner.scan.scan", side_effect=fake_scan):
            providers = _live_providers(TODAY - timedelta(days=3), self.rt / "audit.jsonl", ["AAPL", "MSFT", "NVDA", "QQQ"], self.rt / "earnings.json")
            providers[4](frozenset({"trend_directional", "manual"}))   # 'manual' must never reach scan()
            result = providers[0](open_symbols=frozenset(), trading_day=TODAY)
        self.assertEqual(sorted(calls), [("AAPL", date(2026, 10, 30)), ("QQQ", None)])
        reasons = {s["symbol"]: s["reason"] for s in result["skipped"]}
        self.assertIn("has passed", reasons["MSFT"])
        self.assertIn("no entry", reasons["NVDA"])
        self.assertNotIn("AAPL", reasons)
        self.assertNotIn("QQQ", reasons)

    def test_exit_inputs_for_a_held_stock_do_not_depend_on_the_entry_gate(self):
        from tests.test_marketdata import bars_payload, SESSION
        payload = {**bars_payload(), "ticker": "MSFT"}
        client = Mock(daily_bars=Mock(return_value=payload))
        with patch("alpaca_agents.marketdata.client.DataCredentials.from_environment", return_value=Mock(key="k")), \
             patch("alpaca_agents.marketdata.client.MarketDataClient", return_value=client):
            providers = _live_providers(SESSION, self.rt / "audit.jsonl", ["QQQ"], self.rt / "earnings.json")
            closes = providers[1](["MSFT"])
            bars = providers[2](["MSFT"])
        self.assertIn("MSFT", closes)
        self.assertEqual(bars["MSFT"][-1].day, SESSION)
        client.option_chain.assert_not_called()
        failing = Mock(daily_bars=Mock(side_effect=MarketDataError("down")))
        with patch("alpaca_agents.marketdata.client.DataCredentials.from_environment", return_value=Mock(key="k")), \
             patch("alpaca_agents.marketdata.client.MarketDataClient", return_value=failing):
            providers = _live_providers(SESSION, self.rt / "audit.jsonl", ["QQQ"], self.rt / "earnings.json")
            self.assertEqual(providers[1](["MSFT"]), {})

    def test_cli_rejects_both_symbols_and_watchlist_and_bad_symbols(self):
        for argv in (["--symbols", "AAPL", "--watchlist", "x.json"], ["--symbols", "brk.b"], ["--watchlist", str(self.rt / "none.json")]):
            with patch("sys.argv", ["controller", "--runtime", str(self.rt)] + argv), self.assertRaises(SystemExit):
                controller_main()
        self.assertFalse((self.rt / "controller.lock").exists())
