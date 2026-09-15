from datetime import date, timedelta
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from alpaca_agents.backtest.history import fetch_history, load_bars, save_bars
from alpaca_agents.backtest.replay import (
    HOLD_LIMIT, MAX_FILL_SESSIONS, MIN_BARS, TradeOutcome, outcome_dicts, replay, summarize,
)
from alpaca_agents.backtest.__main__ import main
from alpaca_agents.marketdata.client import MarketDataError
from alpaca_agents.scanner.indicators import Bar

START = date(2024, 1, 1)


def make_bars(closes, volumes=None, spread=0.005):
    """Ascending weekday bars; open = previous close (so gaps are only when we say so)."""
    out, day, prev = [], START, closes[0]
    volumes = volumes or [1e6] * len(closes)
    for c, v in zip(closes, volumes):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        hi, lo = max(prev, c) * (1 + spread), min(prev, c) * (1 - spread)
        out.append(Bar(day, prev, hi, lo, c, v))
        prev, day = c, day + timedelta(days=1)
    return tuple(out)


def uptrend_then_crash(n_up=320, n_down=60):
    closes = [40 * 1.003 ** i for i in range(n_up)]
    for i in range(n_down):
        closes.append(closes[-1] * 0.985)
    return closes


class ReplayTests(unittest.TestCase):
    def test_no_lookahead_and_ordering_invariants(self):
        bars = make_bars(uptrend_then_crash())
        outcomes = replay(bars, "TEST")
        self.assertTrue(outcomes)
        for o in outcomes:
            self.assertGreaterEqual(o.signal_day, bars[MIN_BARS - 1].day)
            if o.fill_day:
                self.assertGreater(o.fill_day, o.signal_day)
                self.assertLessEqual(o.exit_day or o.fill_day, bars[-1].day)
            if o.exit_day:
                self.assertGreaterEqual(o.exit_day, o.fill_day)
        # Truncating the future must not change earlier signals (no look-ahead).
        cut = len(bars) - 40
        early = [o for o in replay(bars[:cut], "TEST") if o.signal_day < bars[cut - MAX_FILL_SESSIONS - max(HOLD_LIMIT.values()) - 1].day]
        full_by_day = {(o.playbook, o.signal_day): o for o in outcomes}
        for o in early:
            self.assertEqual(o, full_by_day[(o.playbook, o.signal_day)])

    def test_one_trade_at_a_time_per_playbook(self):
        outcomes = replay(make_bars(uptrend_then_crash()), "TEST")
        for playbook in {o.playbook for o in outcomes}:
            mine = sorted((o for o in outcomes if o.playbook == playbook), key=lambda o: o.signal_day)
            for a, b in zip(mine, mine[1:]):
                busy_end = a.exit_day or (a.signal_day + timedelta(days=MAX_FILL_SESSIONS * 2))
                self.assertGreater(b.signal_day, busy_end if a.exit_day else a.signal_day)

    def test_crash_produces_trend_losses_with_close_based_stops(self):
        outcomes = replay(make_bars(uptrend_then_crash()), "TEST", playbooks=("trend",))
        losses = [o for o in outcomes if o.outcome == "loss"]
        self.assertTrue(losses)
        for o in losses:
            self.assertLess(o.r_multiple, 0)
            if o.direction == "long":
                self.assertLessEqual(o.exit_price, o.fill_price)

    def test_steady_uptrend_yields_wins_or_timeouts_not_losses(self):
        closes = [40 * 1.003 ** i for i in range(320)]
        outcomes = replay(make_bars(closes), "TEST", playbooks=("trend",))
        resolved = [o for o in outcomes if o.outcome in ("win", "loss", "timeout")]
        self.assertTrue(resolved)
        self.assertEqual([o for o in resolved if o.outcome == "loss"], [])
        self.assertTrue(all(o.r_multiple is not None and math.isfinite(o.r_multiple) for o in resolved))

    def test_unresolved_at_end_of_data_not_counted(self):
        closes = [40 * 1.003 ** i for i in range(MIN_BARS + 2)]
        outcomes = replay(make_bars(closes), "TEST", playbooks=("trend",))
        self.assertTrue(outcomes)
        self.assertTrue(all(o.outcome == "unresolved" for o in outcomes))
        self.assertEqual(summarize(outcomes)["trend"]["resolved"], 0)
        self.assertIsNone(summarize(outcomes)["trend"]["expectancy_r"])

    def test_summary_math_and_sample_gate(self):
        def o(playbook, outcome, r, held):
            return TradeOutcome(playbook, "X", START, "long", 1, .9, 1.2, START, 1, START, 1 + r * .1,
                                outcome, r, held)
        outcomes = [o("breakout", "win", 2.0, 5), o("breakout", "loss", -1.0, 2),
                    o("breakout", "loss", -1.0, 6), o("breakout", "timeout", 0.5, 15),
                    TradeOutcome("breakout", "X", START, "long", 1, .9, 1.2, None, None, None, None, "no_fill", None, None)]
        s = summarize(outcomes)["breakout"]
        self.assertEqual((s["signals"], s["resolved"], s["no_fill"]), (5, 4, 1))
        self.assertEqual(s["win_rate"], 0.25)
        self.assertEqual(s["expectancy_r"], 0.125)
        self.assertEqual(s["failed_breakout_rate"], 0.25)   # one loss within 3 sessions of 4 resolved
        self.assertFalse(s["sample_sufficient"])
        self.assertFalse(s["negative_expectancy"])
        rows = outcome_dicts(outcomes)
        json.dumps(rows)
        self.assertIsNone(rows[-1]["fill_day"])

    def test_rejects_unknown_playbook_and_short_history(self):
        with self.assertRaises(ValueError):
            replay(make_bars([40.0] * 300), "TEST", playbooks=("credit_spread",))
        with self.assertRaises(ValueError):
            replay(make_bars([40.0] * 50), "TEST")


class FakeClient:
    def __init__(self, bars, symbol="SPY"):
        self.bars, self.symbol, self.calls = bars, symbol, []

    def daily_bars(self, symbol, *, start, end):
        self.calls.append((start, end))
        rows = [{"t": int((date(b.day.year, b.day.month, b.day.day) - date(1970, 1, 1)).days * 86400000 + 4 * 3600000),
                 "o": b.open, "h": b.high, "l": b.low, "c": b.close, "v": b.volume}
                for b in self.bars if start <= b.day <= end]
        return {"status": "OK", "ticker": self.symbol, "adjusted": True, "results": rows}


class HistoryTests(unittest.TestCase):
    def test_stitching_multiple_chunks_is_continuous(self):
        bars = make_bars([40 * 1.001 ** i for i in range(900)])
        client = FakeClient(bars)
        got = fetch_history(client, "SPY", start=bars[0].day, end=bars[-1].day)
        self.assertEqual(got, bars)
        self.assertGreater(len(client.calls), 1)
        self.assertTrue(all((e - s).days <= 730 for s, e in client.calls))

    def test_overlap_and_bad_window_rejected(self):
        bars = make_bars([40.0 + i * 0.01 for i in range(300)])
        class Overlapping(FakeClient):
            def daily_bars(self, symbol, *, start, end):
                return super().daily_bars(symbol, start=self.bars[0].day, end=self.bars[-1].day)
        with self.assertRaises(MarketDataError):
            fetch_history(Overlapping(bars), "SPY", start=bars[0].day, end=bars[0].day + timedelta(days=1500))
        with self.assertRaises(MarketDataError):
            fetch_history(FakeClient(bars), "SPY", start=bars[-1].day, end=bars[0].day)

    def test_save_and_load_roundtrip_and_symbol_check(self):
        bars = make_bars([40 * 1.001 ** i for i in range(250)])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spy.json"
            save_bars(path, "SPY", bars)
            self.assertEqual(load_bars(path, "SPY"), bars)
            with self.assertRaises(MarketDataError):
                load_bars(path, "QQQ")
            path.write_text('{"symbol":"SPY","bars":[{"day":"2024-01-01","open":NaN,"high":1,"low":1,"close":1,"volume":1}]}')
            with self.assertRaises(MarketDataError):
                load_bars(path, "SPY")


class CliTests(unittest.TestCase):
    def test_cli_from_file_writes_report_without_network_or_enablement(self):
        bars = make_bars(uptrend_then_crash())
        with tempfile.TemporaryDirectory() as tmp:
            bars_file, out = Path(tmp) / "bars.json", Path(tmp) / "report.json"
            save_bars(bars_file, "TEST", bars)
            with patch("sys.argv", ["backtest", "--symbol", "TEST", "--bars-file", str(bars_file),
                                    "--output", str(out)]), patch("builtins.print"):
                self.assertEqual(main(), 0)
            report = json.loads(out.read_text())
            self.assertEqual(report["level"], "underlying")
            self.assertFalse(report["options_pnl_modelled"])
            self.assertIn("trend", report["summary"])
            self.assertTrue(report["trades"])
            self.assertNotIn("enabled", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
