from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import unittest

from alpaca_agents.rules import RiskState, evaluate
from alpaca_agents.scanner.contracts import OptionQuote, is_liquid, select_debit_spread, select_long
from alpaca_agents.scanner.indicators import Bar, ema, rsi
from alpaca_agents.scanner.scan import PLAYBOOKS, ScanConfig, SymbolSnapshot, scan
from alpaca_agents.scanner.signals import Signal, Skip, breakout_signal, oversold_bounce_signal, trend_signal

AS_OF = date(2026, 9, 15)
ALL_ON = frozenset(PLAYBOOKS)


def bars_from_closes(closes, volumes=None, last=AS_OF, spread=0.005):
    """Ascending bars ending on `last`; high/low wrap the close by +/- spread."""
    n = len(closes)
    volumes = volumes or [1_000_000.0] * n
    out = []
    for i, (c, v) in enumerate(zip(closes, volumes)):
        day = last - timedelta(days=n - 1 - i)
        out.append(Bar(day, c, c * (1 + spread), c * (1 - spread), c, v))
    return tuple(out)


def bounce_bars(spread=0.002):
    """Uptrend, then a 4-session slide with a real swing low (a wick) to lean a stop on.

    Without the wick the 10-session low is today's low, a few cents under the
    close: a stop inside noise, which the signal now refuses.
    """
    closes = uptrend(rate=0.002)
    for i in range(1, 5):
        closes[-i] = closes[-5] * (0.99 ** (5 - i))
    bars = list(bars_from_closes(closes, spread=spread))
    b = bars[-3]
    bars[-3] = Bar(b.day, b.open, b.high, b.close * 0.97, b.close, b.volume)
    return tuple(bars)


def breakout_bars(last_close=101.1, last_volume=2_000_000.0, spread=0.002):
    """Gentle 8-session oscillation (daily ranges well inside the 10-session range), then a close above it."""
    import math
    closes = [100 + 0.5 * math.sin(2 * math.pi * i / 8) for i in range(220)]
    volumes = [1_000_000.0] * 220
    closes[-1], volumes[-1] = last_close, last_volume
    return bars_from_closes(closes, volumes, spread=spread)


def uptrend(n=220, start=40.0, rate=0.003):
    return [start * (1 + rate) ** i for i in range(n)]


def downtrend(n=220, start=60.0, rate=0.003):
    return [start * (1 - rate) ** i for i in range(n)]


def chain(price, right="call", expiry=AS_OF + timedelta(days=38), oi=1000, scale=1.0):
    """Quotes with deltas stepping 0.03 per 1% strike step; prices scaled by `scale`."""
    quotes = []
    for k in range(-12, 13):
        strike = Decimal(str(round(price * (1 + k * 0.01), 2)))
        # Calls: higher strike -> lower delta. Puts: higher strike -> deeper ITM -> higher |delta|.
        delta = max(0.05, min(0.95, 0.50 - k * 0.03 if right == "call" else 0.50 + k * 0.03))
        mid = Decimal(str(round((0.20 + delta * 1.6) * scale, 2)))
        half = (mid * Decimal("0.04")).quantize(Decimal("0.01")) or Decimal("0.01")
        quotes.append(OptionQuote(expiry, strike, right, mid - half, mid + half,
                                  delta if right == "call" else -delta, oi))
    return tuple(quotes)


def snapshot(symbol, bars, quotes, earnings=AS_OF + timedelta(days=90), iv_rank=None):
    return SymbolSnapshot(symbol, bars, quotes, earnings, iv_rank)


def fresh_state():
    now = datetime(2026, 9, 15, 13, tzinfo=timezone.utc)
    return RiskState(AS_OF, now, 0, 0, Decimal("0"), Decimal("2000"), (), reconciled=True), now


class IndicatorTests(unittest.TestCase):
    def test_ema_and_rsi_sanity(self):
        flat = [10.0] * 60
        self.assertAlmostEqual(ema(flat, 20), 10.0)
        self.assertEqual(rsi(flat), 100.0)
        rising = list(range(1, 60))
        self.assertGreater(rsi([float(x) for x in rising]), 90)
        self.assertLess(rsi([float(60 - x) for x in rising]), 10)


class SignalTests(unittest.TestCase):
    def test_trend_long_and_short(self):
        up = trend_signal(bars_from_closes(uptrend()))
        self.assertIsInstance(up, Signal)
        self.assertEqual(up.direction, "long")
        self.assertLess(up.stop, up.entry)
        self.assertLess(up.entry, up.target)
        down = trend_signal(bars_from_closes(downtrend()))
        self.assertIsInstance(down, Signal)
        self.assertEqual(down.direction, "short")
        self.assertGreater(down.stop, down.entry)
        self.assertGreater(down.entry, down.target)

    def test_trend_skips_chop_and_chase(self):
        chop = [40 + (i % 2) * 0.5 for i in range(220)]
        self.assertIsInstance(trend_signal(bars_from_closes(chop)), Skip)
        closes = uptrend()
        closes[-1] = closes[-2] * 1.03  # 3% gap past prior high
        self.assertIn("chase", trend_signal(bars_from_closes(closes)).reason)

    def test_oversold_bounce_only_in_uptrend(self):
        closes = uptrend(rate=0.002)
        for i in range(1, 5):
            closes[-i] = closes[-5] * (0.99 ** (5 - i))
        # On a straight slide the "swing low" is today's low, cents under the close: inside noise, refused.
        self.assertIn("inside noise", oversold_bounce_signal(bars_from_closes(closes)).reason)
        sig = oversold_bounce_signal(bounce_bars())
        self.assertIsInstance(sig, Signal, getattr(sig, "reason", None))
        self.assertEqual(sig.direction, "long")
        self.assertLess(sig.stop, sig.entry)
        self.assertGreater(sig.target, sig.entry)
        dn = downtrend()
        self.assertIn("SMA200", oversold_bounce_signal(bars_from_closes(dn)).reason)

    def test_breakout_requires_tight_range_volume_and_no_chase(self):
        # A whipsaw where every day spans the whole range has ATR ~ range width: no stop can clear noise.
        whip = [100 + (0.5 if i % 2 else -0.5) for i in range(220)]
        vol = [1_000_000.0] * 219 + [2_000_000.0]
        whip[-1] = 101.3
        self.assertIn("inside noise", breakout_signal(bars_from_closes(whip, vol)).reason)
        sig = breakout_signal(breakout_bars())                    # range top 100.5 * 1.002; risk 0.40 > 0.5 ATR
        self.assertIsInstance(sig, Signal, getattr(sig, "reason", None))
        self.assertAlmostEqual(sig.stop, 100.5 * 1.002)
        self.assertIn("volume", breakout_signal(breakout_bars(last_volume=1_100_000.0)).reason)
        self.assertIn("chase", breakout_signal(breakout_bars(last_close=104.0)).reason)


class ContractTests(unittest.TestCase):
    def test_liquidity_gate(self):
        good = OptionQuote(AS_OF + timedelta(days=35), Decimal("40"), "call", Decimal("0.90"), Decimal("0.98"), 0.4, 600)
        self.assertTrue(is_liquid(good))
        self.assertFalse(is_liquid(OptionQuote(good.expiration, good.strike, "call", good.bid, good.ask, 0.4, 499)))
        self.assertFalse(is_liquid(OptionQuote(good.expiration, good.strike, "call", Decimal("0.80"), Decimal("1.00"), 0.4, 600)))
        self.assertFalse(is_liquid(OptionQuote(good.expiration, good.strike, "call", Decimal("0"), Decimal("0.05"), 0.4, 600)))

    def test_dte_window_and_delta_band(self):
        self.assertIsNone(select_long(chain(40, expiry=AS_OF + timedelta(days=20)), "call", AS_OF))
        self.assertIsNone(select_long(chain(40, expiry=AS_OF + timedelta(days=60)), "call", AS_OF))
        s = select_long(chain(40), "call", AS_OF)
        self.assertEqual(s.strategy, "long_call")
        self.assertTrue(0.35 <= abs(s.legs[0].delta) <= 0.45)
        p = select_long(chain(40, "put"), "put", AS_OF)
        self.assertEqual(p.strategy, "long_put")

    def test_debit_spread_quality_gate(self):
        s = select_debit_spread(chain(40), "call", AS_OF, Decimal("100"))
        self.assertIsNotNone(s)
        buy, sell = s.legs
        self.assertLess(buy.strike, sell.strike)
        self.assertGreaterEqual(s.max_profit, s.limit_debit)
        self.assertLessEqual(s.limit_debit * 100, 100)
        p = select_debit_spread(chain(40, "put"), "put", AS_OF, Decimal("100"))
        self.assertGreater(p.legs[0].strike, p.legs[1].strike)


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.state, self.now = fresh_state()

    def assert_passes_rules(self, idea):
        idea = json.loads(json.dumps(idea))  # must be pure JSON, like the real pipeline
        decision = evaluate(idea, self.state, now=self.now, trading_day=AS_OF, kill_switch=False)
        self.assertTrue(decision["approved"], decision["reason"])

    def test_every_playbook_idea_passes_rules_engine(self):
        snaps = [snapshot("IWM", bars_from_closes(uptrend()), chain(uptrend()[-1]) + chain(uptrend()[-1], "put"))]
        bounce = bounce_bars()
        snaps.append(snapshot("XLF", bounce, chain(bounce[-1].close)))
        snaps.append(snapshot("XLE", breakout_bars(), chain(101.1)))
        cfg = ScanConfig(universe=frozenset({"IWM", "XLF", "XLE"}), enabled_playbooks=ALL_ON, max_open_positions=5)
        result = scan(snaps, cfg, as_of=AS_OF)
        seen = {i["playbook"] for i in result.shadow}
        self.assertEqual(seen, set(PLAYBOOKS), result.skipped)
        self.assertTrue(result.shadow)
        for idea in result.shadow + result.proposals:
            self.assert_passes_rules(idea)

    def test_all_playbooks_disabled_by_default_shadow_only(self):
        snap = snapshot("IWM", bars_from_closes(uptrend()), chain(uptrend()[-1]))
        result = scan([snap], ScanConfig(), as_of=AS_OF)
        self.assertEqual(result.proposals, [])
        self.assertTrue(result.shadow)
        self.assertTrue(any("disabled" in s["reason"] for s in result.skipped))

    def test_unknown_playbook_rejected(self):
        with self.assertRaises(ValueError):
            scan([], ScanConfig(enabled_playbooks=frozenset({"credit_spread"})), as_of=AS_OF)

    def test_cross_cutting_filters(self):
        bars, quotes = bars_from_closes(uptrend()), chain(uptrend()[-1])
        cfg = ScanConfig(universe=frozenset({"IWM"}), enabled_playbooks=ALL_ON)
        reasons = lambda r: [s["reason"] for s in r.skipped]
        self.assertIn("outside liquid universe", reasons(scan([snapshot("ZZZ", bars, quotes)], cfg, as_of=AS_OF)))
        self.assertIn("one open idea per symbol", reasons(scan([snapshot("IWM", bars, quotes)], cfg, as_of=AS_OF, open_symbols=frozenset({"IWM"}))))
        self.assertIn("earnings date unknown; fail closed", reasons(scan([snapshot("IWM", bars, quotes, earnings=None)], cfg, as_of=AS_OF)))
        early = scan([snapshot("IWM", bars, quotes, earnings=AS_OF + timedelta(days=10))], cfg, as_of=AS_OF)
        self.assertEqual(early.proposals, [])
        self.assertTrue(any("earnings" in r for r in reasons(early)))
        stale = scan([snapshot("IWM", bars, quotes)], cfg, as_of=AS_OF + timedelta(days=1))
        self.assertIn("bars not current for session", reasons(stale))
        # Expensive chain: the naked long blows the $100 cap, but the spread's width still fits.
        pricey = scan([snapshot("IWM", bars, chain(uptrend()[-1], scale=3.0), iv_rank=10)], cfg, as_of=AS_OF)
        self.assertTrue(any(s["playbook"] == "trend_directional" and "premium cap" in s["reason"] for s in pricey.skipped))
        self.assertEqual([i["playbook"] for i in pricey.proposals], ["trend_debit_spread"])
        short_bars = bars[-50:]
        bad = scan([snapshot("IWM", short_bars, quotes)], cfg, as_of=AS_OF)
        self.assertTrue(any("bad market data" in r for r in reasons(bad)))

    def test_iv_rank_prefers_spread_when_elevated(self):
        bars, quotes = bars_from_closes(uptrend()), chain(uptrend()[-1])
        cfg = ScanConfig(universe=frozenset({"IWM"}), enabled_playbooks=ALL_ON)
        high = scan([snapshot("IWM", bars, quotes, iv_rank=80)], cfg, as_of=AS_OF)
        low = scan([snapshot("IWM", bars, quotes, iv_rank=10)], cfg, as_of=AS_OF)
        self.assertEqual(high.proposals[0]["playbook"], "trend_debit_spread")
        self.assertEqual(low.proposals[0]["playbook"], "trend_directional")
        self.assertEqual(len(high.proposals), 1)  # one idea per symbol

    def test_correlation_bucket_and_slots(self):
        bars, quotes = bars_from_closes(uptrend()), chain(uptrend()[-1])
        cfg = ScanConfig(universe=frozenset({"SPY", "QQQ", "XLF"}), enabled_playbooks=ALL_ON)
        snaps = [snapshot(s, bars, quotes) for s in ("SPY", "QQQ", "XLF")]
        result = scan(snaps, cfg, as_of=AS_OF)
        symbols = [i["symbol"] for i in result.proposals]
        self.assertEqual(len(symbols), 2)
        self.assertEqual(len({s for s in symbols if s in ("SPY", "QQQ")}), 1)
        self.assertIn("XLF", symbols)
        held = scan(snaps, cfg, as_of=AS_OF, open_symbols=frozenset({"IWM"}))
        self.assertEqual([i["symbol"] for i in held.proposals], ["XLF"])
        full = scan(snaps, cfg, as_of=AS_OF, open_symbols=frozenset({"AAPL", "MSFT"}))
        self.assertEqual(full.proposals, [])
        self.assertTrue(any("no free position slot" in s["reason"] for s in full.skipped))

    def test_idea_carries_playbook_metadata_and_is_json(self):
        snap = snapshot("IWM", bars_from_closes(uptrend()), chain(uptrend()[-1]), iv_rank=80)
        result = scan([snap], ScanConfig(universe=frozenset({"IWM"}), enabled_playbooks=ALL_ON), as_of=AS_OF)
        idea = result.proposals[0]
        json.dumps(idea)
        self.assertEqual(idea["strategy"], "call_debit_spread")
        self.assertEqual(idea["exit_plan"]["time_stop_dte"], 14)
        self.assertIn("EMA20", idea["exit_plan"]["underlying_stop_rule"])
        self.assertEqual(idea["quantity"], 1)
        self.assertEqual(idea["session"], AS_OF.isoformat())
        self.assertLessEqual(Decimal(idea["est_contract_cost"]) + Decimal(idea["estimated_fees"]), 100)


if __name__ == "__main__":
    unittest.main()
