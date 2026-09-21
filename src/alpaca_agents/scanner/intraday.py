"""Pure underlying-only scalp hypotheses. NEVER produces an executable option idea.

Parameters are research starting points, not established edges or broker rules.
Completed one-minute bars only; no interpolation, partial bars or forward inputs.
"""
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import math

from alpaca_agents.calendar import is_session
from alpaca_agents.executor.eastern import to_eastern
from .indicators import atr, ema


@dataclass(frozen=True)
class MinuteBar:
    at: datetime  # interval START
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float  # provider's volume-weighted price within the interval


def research_window(now):
    """Scheduled hours only; not a broker-open/halts assertion. Quotes still gate execution."""
    local = to_eastern(now)
    minute = local.hour * 60 + local.minute
    return is_session(local.date()) and (590 <= minute < 690 or 810 <= minute < 930)


def completed_bars(payload, *, symbol, now):
    """Strict normalization: require the full regular-session sequence for session VWAP."""
    local = to_eastern(now)
    if (not isinstance(payload, dict) or payload.get('ticker') != symbol
            or payload.get('adjusted') is not True or payload.get('next_url')
            or not isinstance(payload.get('results'), list) or len(payload['results']) > 1500):
        raise ValueError('Incomplete or mismatched intraday response')
    opening = datetime.combine(local.date(), time(9, 30), local.tzinfo)
    closing = opening + timedelta(hours=6, minutes=30)
    bars, previous = [], None
    for row in payload['results']:
        if not isinstance(row, dict) or type(row.get('t')) is not int or row['t'] % 60000:
            raise ValueError('Invalid minute timestamp')
        at = datetime.fromtimestamp(row['t'] / 1000, timezone.utc)
        if at > now or (previous is not None and at <= previous):
            raise ValueError('Future, duplicate or unordered minute bars')
        previous = at
        if not opening <= at < closing or at + timedelta(minutes=1) > now:
            continue  # explicitly exclude extended hours and the forming candle
        values = [row.get(k) for k in ('o', 'h', 'l', 'c', 'v', 'vw')]
        if any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 1e12 for v in values):
            raise ValueError('Missing or nonfinite intraday fields')
        o, h, l, c, v, vw = values
        if not (0 < l <= min(o, c) <= max(o, c) <= h and v > 0 and l <= vw <= h):
            raise ValueError('Invalid OHLCV/VWAP')
        if at != (bars[-1].at + timedelta(minutes=1) if bars else opening):
            raise ValueError('Incomplete regular session; VWAP is unknown')
        bars.append(MinuteBar(at, *values))
    if len(bars) < 22:
        raise ValueError('Need 22 completed regular-session minutes')
    age = now - (bars[-1].at + timedelta(minutes=1))
    if not timedelta(0) <= age <= timedelta(seconds=90):
        raise ValueError('Stale minute bars')
    return tuple(bars)


def evaluate(payload, *, symbol, now):
    """Returns research levels/checks, never a risk approval, probability or order."""
    if not research_window(now):
        return {'status': 'skipped', 'reason': 'Outside research windows: 09:50-11:30 / 13:30-15:30 ET'}
    bars = completed_bars(payload, symbol=symbol, now=now)
    closes = [b.close for b in bars]
    last, prev = bars[-1], bars[-2]
    volume = sum(b.volume for b in bars)
    vwap = sum(b.vwap * b.volume for b in bars) / volume
    prior_vwap = sum(b.vwap * b.volume for b in bars[:-1]) / (volume - last.volume)
    fast, slow = ema(closes, 9), ema(closes, 21)
    average_volume = sum(b.volume for b in bars[-21:-1]) / 20
    relative_volume = last.volume / average_volume
    average_dollars = sum(b.close * b.volume for b in bars[-21:-1]) / 20
    volatility = atr(bars, 14)
    checks = {'bar_at': last.at.isoformat(), 'vwap': round(vwap, 4), 'ema9': round(fast, 4),
              'ema21': round(slow, 4), 'atr14': round(volatility, 4),
              'relative_volume_20m': round(relative_volume, 3)}
    def skip(reason):
        return {'status': 'skipped', 'reason': reason, **checks}
    if last.close < 5 or average_dollars < 1_000_000:
        return skip('Underlying liquidity floor: price >= $5 and average minute dollar volume >= $1m')
    if relative_volume < 1.5:
        return skip('Confirmation volume below 1.5x preceding 20 completed minutes')
    long = last.close > fast > slow and last.close > vwap
    short = last.close < fast < slow and last.close < vwap
    if not (long or short):
        return skip('No EMA9/21 trend aligned with session VWAP')
    opening_high, opening_low = max(b.high for b in bars[:15]), min(b.low for b in bars[:15])
    breakout = (prev.close <= opening_high < last.close if long else prev.close >= opening_low > last.close)
    reclaim = (prev.close <= prior_vwap and last.close > vwap if long else prev.close >= prior_vwap and last.close < vwap)
    if not (breakout or reclaim):
        return skip('No fresh 15-minute opening-range break or VWAP reclaim/loss')
    entry = last.close
    stop = min(b.low for b in bars[-5:]) if long else max(b.high for b in bars[-5:])
    risk = abs(entry - stop)
    if volatility <= 0 or not .5 * volatility <= risk <= 1.5 * volatility:
        return skip('Structural stop outside 0.5-1.5 ATR14; noise or extended entry')
    target = entry + (1.5 * risk if long else -1.5 * risk)
    if target <= 0:
        return skip('Invalid projected target')
    return {'status': 'setup', 'reason': 'Underlying hypothesis only; no option selected or order authorized',
            'technique': 'opening_range_15m' if breakout else 'vwap_reclaim',
            'direction': 'long' if long else 'short', 'entry': round(entry, 4), 'stop': round(stop, 4),
            'target': round(target, 4), 'target_basis': 'Projected 1.5 underlying R, not option return',
            **checks}
