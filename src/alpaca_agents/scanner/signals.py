"""Playbook signal logic. Pure functions over bars; no data fetching, no LLM.

Parameters are the playbook's starting points, NOT proven edges. Backtest and
tune before enabling any playbook in ScanConfig.enabled_playbooks.
"""
from dataclasses import dataclass

from .indicators import atr, ema, ema_series, momentum, pivot_highs, pivot_lows, rsi, sma, validate_bars

CHASE_LIMIT = 0.015        # skip if price already ran >1.5% past the trigger
PULLBACK_BAND = 0.01       # "pullback toward EMA20": close within 1% above/below EMA20
RESISTANCE_LOOKBACK = 60
FALLBACK_REWARD_MULTIPLE = 1.5   # used only when no swing level exists in the trade direction
BOUNCE_RSI = 35.0
BOUNCE_SWING_LOW_LOOKBACK = 10
BREAKOUT_RANGE_SESSIONS = 10
BREAKOUT_RANGE_WIDTH = 0.05
BREAKOUT_VOLUME_MULTIPLE = 1.5
BREAKOUT_NEAR_HIGHS = 0.97      # range top within 3% of the 60-session high
# A stop inside normal daily noise is not a swing-trade stop: it exits on noise
# live and produces absurd R-multiples in replay (an 8-cent stop turns a 1%
# gap into -38R). Risk must be at least this many ATR14.
MIN_RISK_ATR = 0.5


@dataclass(frozen=True)
class Signal:
    playbook: str
    direction: str      # "long" or "short"
    entry: float
    stop: float
    target: float
    strength: float     # 0..1, feeds the placeholder score
    thesis: str


@dataclass(frozen=True)
class Skip:
    playbook: str
    reason: str


def _reward_risk(direction, entry, stop, target):
    risk = entry - stop if direction == "long" else stop - entry
    reward = target - entry if direction == "long" else entry - target
    if risk <= 0 or reward <= 0:
        return 0.0
    return reward / risk


def _stop_too_tight(bars, entry, stop):
    """Reason string when |entry - stop| is inside noise, else None."""
    floor = MIN_RISK_ATR * atr(bars, 14)
    risk = abs(entry - stop)
    if risk < floor:
        return f"stop {risk:.2f} from entry is inside noise (min {floor:.2f} = {MIN_RISK_ATR} x ATR14)"
    return None


def _nearest_level(levels, entry, direction):
    above = [lvl for lvl in levels if lvl > entry] if direction == "long" else [lvl for lvl in levels if lvl < entry]
    if not above:
        return None
    return min(above) if direction == "long" else max(above)


def trend_signal(bars):
    """Strategies 1 & 2 share this signal (EMA stack + 20-day momentum)."""
    name = "trend"
    validate_bars(bars)
    closes = [b.close for b in bars]
    close, prev = closes[-1], bars[-2]
    e20, e50, mom = ema(closes, 20), ema(closes, 50), momentum(closes, 20)
    if close > e20 > e50 and mom > 0:
        direction = "long"
    elif close < e20 < e50 and mom < 0:
        direction = "short"
    else:
        return Skip(name, "no EMA-stack trend with confirming momentum")

    if direction == "long":
        pullback = close <= e20 * (1 + PULLBACK_BAND)
        if not pullback and close > prev.high * (1 + CHASE_LIMIT):
            return Skip(name, f"already ran {(close / prev.high - 1):.1%} past prior high; do not chase")
        entry = close if pullback or close >= prev.high else prev.high
        stop = e20
        level = _nearest_level(pivot_highs(bars, RESISTANCE_LOOKBACK), entry, direction)
    else:
        pullback = close >= e20 * (1 - PULLBACK_BAND)
        if not pullback and close < prev.low * (1 - CHASE_LIMIT):
            return Skip(name, f"already ran {(1 - close / prev.low):.1%} past prior low; do not chase")
        entry = close if pullback or close <= prev.low else prev.low
        stop = e20
        level = _nearest_level(pivot_lows(bars, RESISTANCE_LOOKBACK), entry, direction)

    risk = abs(entry - stop)
    if risk <= 0:
        return Skip(name, "entry not beyond EMA20 stop")
    tight = _stop_too_tight(bars, entry, stop)
    if tight:
        return Skip(name, tight)
    if level is None:
        target = entry + FALLBACK_REWARD_MULTIPLE * risk if direction == "long" else entry - FALLBACK_REWARD_MULTIPLE * risk
        target_note = "no swing level in path; projected 1.5R"
    else:
        target = level
        target_note = "nearest swing level"
    rr = _reward_risk(direction, entry, stop, target)
    if rr < 1:
        return Skip(name, f"reward:risk {rr:.2f} below 1:1 to {target_note}")
    word = "uptrend" if direction == "long" else "downtrend"
    cmp = ">" if direction == "long" else "<"
    thesis = (f"{word}: close {close:.2f} {cmp} EMA20 {e20:.2f} {cmp} EMA50 {e50:.2f}; "
              f"mom20 {mom:+.1%}; {'pullback' if pullback else 'prior-bar break'} entry; target {target_note}")
    return Signal(name, direction, entry, stop, target, min(1.0, abs(mom) / 0.10), thesis)


def oversold_bounce_signal(bars):
    """Strategy 3: buy dips only inside a long-term uptrend."""
    name = "oversold_bounce"
    validate_bars(bars)
    closes = [b.close for b in bars]
    close = closes[-1]
    s200 = sma(closes, 200)
    if close <= s200:
        return Skip(name, "below SMA200; no counter-trend buys in downtrends")
    r = rsi(closes, 14)
    e50 = ema_series(closes, 50)
    three_down = closes[-1] < closes[-2] < closes[-3] < closes[-4]
    rising_e50 = e50[-1] > e50[-6]
    if not (r < BOUNCE_RSI or (three_down and rising_e50)):
        return Skip(name, f"not oversold (RSI {r:.1f})")
    entry = close
    stop = min(b.low for b in bars[-BOUNCE_SWING_LOW_LOOKBACK:])
    if stop >= entry:
        return Skip(name, "swing-low stop not below entry")
    tight = _stop_too_tight(bars, entry, stop)
    if tight:
        return Skip(name, tight)
    e20 = ema(closes, 20)
    candidates = [lvl for lvl in (e20,) if lvl > entry]
    swing = _nearest_level(pivot_highs(bars, RESISTANCE_LOOKBACK), entry, "long")
    if swing is not None:
        candidates.append(swing)
    if not candidates:
        return Skip(name, "no EMA20 or swing-high target above entry")
    target = min(candidates)
    rr = _reward_risk("long", entry, stop, target)
    if rr < 1:
        return Skip(name, f"reward:risk {rr:.2f} below 1:1")
    trigger = f"RSI14 {r:.1f}" if r < BOUNCE_RSI else "3 down closes into rising EMA50"
    strength = max(0.0, min(1.0, (BOUNCE_RSI - r) / BOUNCE_RSI)) if r < BOUNCE_RSI else 0.5
    thesis = f"oversold bounce: close {close:.2f} > SMA200 {s200:.2f}; {trigger}; target {'EMA20' if target == e20 else 'swing high'} {target:.2f}"
    return Signal(name, "long", entry, stop, target, strength, thesis)


def breakout_signal(bars):
    """Strategy 4: tight consolidation near highs, close above it on volume."""
    name = "breakout"
    validate_bars(bars)
    today = bars[-1]
    window = bars[-1 - BREAKOUT_RANGE_SESSIONS:-1]
    range_high = max(b.high for b in window)
    range_low = min(b.low for b in window)
    width = (range_high - range_low) / range_low
    if width >= BREAKOUT_RANGE_WIDTH:
        return Skip(name, f"range width {width:.1%} not tight")
    sixty_high = max(b.high for b in bars[-61:-1])
    if range_high < sixty_high * BREAKOUT_NEAR_HIGHS:
        return Skip(name, "consolidation not near highs")
    if today.close <= range_high:
        return Skip(name, "no close above range resistance")
    avg_volume = sum(b.volume for b in bars[-21:-1]) / 20
    if avg_volume <= 0 or today.volume <= BREAKOUT_VOLUME_MULTIPLE * avg_volume:
        return Skip(name, "breakout volume not above 1.5x average")
    if today.close > range_high * (1 + CHASE_LIMIT):
        return Skip(name, "already extended past breakout level; do not chase")
    entry, stop = today.close, range_high
    tight = _stop_too_tight(bars, entry, stop)
    if tight:
        return Skip(name, tight)
    target = range_high + (range_high - range_low)
    rr = _reward_risk("long", entry, stop, target)
    if rr < 1:
        return Skip(name, f"measured-move reward:risk {rr:.2f} below 1:1")
    thesis = (f"breakout: {BREAKOUT_RANGE_SESSIONS}-session range {range_low:.2f}-{range_high:.2f} ({width:.1%}); "
              f"close {today.close:.2f} above on {today.volume / avg_volume:.1f}x volume; measured move {target:.2f}")
    return Signal(name, "long", entry, stop, target, min(1.0, today.volume / avg_volume / 3), thesis)
