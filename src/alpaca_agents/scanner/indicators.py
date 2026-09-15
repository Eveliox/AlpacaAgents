"""Pure technical indicators over ascending daily bars.

Floats are acceptable here: these produce signals, not accounting. Money that
reaches the rules engine is converted to cent-quantized Decimals in scan.py.
"""
from dataclasses import dataclass
from datetime import date
import math

MIN_BARS = 200  # SMA200 for Strategy 3 and well-converged EMAs for the rest.


@dataclass(frozen=True)
class Bar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float


def validate_bars(bars) -> None:
    if len(bars) < MIN_BARS:
        raise ValueError(f"at least {MIN_BARS} daily bars required")
    for bar in bars:
        values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
        if type(bar.day) is not date or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values
        ):
            raise ValueError("bars need a date and finite numeric fields")
        if not 0 < bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high or bar.volume < 0:
            raise ValueError("inconsistent OHLC values")
    for prev, cur in zip(bars, bars[1:]):
        if cur.day <= prev.day:
            raise ValueError("bars must be strictly ascending by date")


def sma(values, period: int) -> float:
    if len(values) < period:
        raise ValueError("insufficient data for SMA")
    return sum(values[-period:]) / period


def ema_series(values, period: int) -> list:
    if len(values) < period:
        raise ValueError("insufficient data for EMA")
    weight = 2 / (period + 1)
    series = [None] * (period - 1)
    current = sum(values[:period]) / period
    series.append(current)
    for value in values[period:]:
        current = value * weight + current * (1 - weight)
        series.append(current)
    return series


def ema(values, period: int) -> float:
    return ema_series(values, period)[-1]


def rsi(closes, period: int = 14) -> float:
    """Wilder-smoothed RSI."""
    if len(closes) < period + 1:
        raise ValueError("insufficient data for RSI")
    changes = [b - a for a, b in zip(closes, closes[1:])]
    gains = [max(c, 0.0) for c in changes]
    losses = [max(-c, 0.0) for c in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def momentum(closes, lookback: int = 20) -> float:
    if len(closes) < lookback + 1:
        raise ValueError("insufficient data for momentum")
    return closes[-1] / closes[-1 - lookback] - 1.0


def pivot_highs(bars, lookback: int, wing: int = 2) -> list:
    """Highs greater than `wing` neighbours on both sides within the lookback."""
    start = max(wing, len(bars) - lookback)
    result = []
    for i in range(start, len(bars) - wing):
        high = bars[i].high
        if all(high > bars[j].high for j in range(i - wing, i + wing + 1) if j != i):
            result.append(high)
    return result


def pivot_lows(bars, lookback: int, wing: int = 2) -> list:
    start = max(wing, len(bars) - lookback)
    result = []
    for i in range(start, len(bars) - wing):
        low = bars[i].low
        if all(low < bars[j].low for j in range(i - wing, i + wing + 1) if j != i):
            result.append(low)
    return result
