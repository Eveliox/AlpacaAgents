"""Walk-forward replay of signals.py over historical daily bars.

Honesty rules baked in:
- Signals see only bars up to and including the signal session (no look-ahead).
- Fills happen no earlier than the next session's open.
- Stop checks are close-based (matching the playbook's "closes back through"
  wording), so a gap through the stop exits at the worse close, not the stop.
- Same-session stop and target => counted as a loss.
- Results are R-multiples on the UNDERLYING. A 1R underlying win does not mean
  the option made 1R; option outcomes need point-in-time option quotes.
"""
from dataclasses import dataclass, asdict
from datetime import date
from statistics import mean, median

from alpaca_agents.scanner.indicators import MIN_BARS, ema, validate_bars
from alpaca_agents.scanner.signals import Signal, breakout_signal, oversold_bounce_signal, trend_signal

SIGNALS = {"trend": trend_signal, "oversold_bounce": oversold_bounce_signal, "breakout": breakout_signal}
MAX_FILL_SESSIONS = 3
# Approximations of the playbook's time stops in trading sessions (theta-driven
# DTE stops on a ~38 DTE entry): trend/breakout ~15 sessions, bounce 10 sessions.
HOLD_LIMIT = {"trend": 15, "oversold_bounce": 10, "breakout": 15}
LOOKBACK = 400          # bars fed to a signal; > MIN_BARS so EMAs are converged
FAILED_BREAKOUT_SESSIONS = 3
MIN_SAMPLE = 30         # playbook: 30+ triggered ideas before judging a strategy


@dataclass(frozen=True)
class TradeOutcome:
    playbook: str
    symbol: str
    signal_day: date
    direction: str
    entry_plan: float
    stop_plan: float
    target: float
    fill_day: date | None
    fill_price: float | None
    exit_day: date | None
    exit_price: float | None
    outcome: str            # win | loss | timeout | no_fill | unresolved
    r_multiple: float | None
    sessions_held: int | None


def _simulate(bars, start, signal: Signal, playbook: str, symbol: str) -> TradeOutcome:
    long = signal.direction == "long"
    entry, stop, target = signal.entry, signal.stop, signal.target
    close_entry = entry == bars[start - 1].close
    base = dict(playbook=playbook, symbol=symbol, signal_day=bars[start - 1].day, direction=signal.direction,
                entry_plan=entry, stop_plan=stop, target=target)

    fill_idx = fill = None
    for j in range(start, min(start + MAX_FILL_SESSIONS, len(bars))):
        b = bars[j]
        if close_entry:
            fill_idx, fill = j, b.open                         # earliest realistic fill
        elif long and b.high >= entry:
            fill_idx, fill = j, max(b.open, entry)             # gap above trigger fills worse
        elif not long and b.low <= entry:
            fill_idx, fill = j, min(b.open, entry)
        if fill_idx is not None:
            break
    if fill_idx is None:
        if start + MAX_FILL_SESSIONS > len(bars):
            return TradeOutcome(**base, fill_day=None, fill_price=None, exit_day=None, exit_price=None,
                                outcome="unresolved", r_multiple=None, sessions_held=None)
        return TradeOutcome(**base, fill_day=None, fill_price=None, exit_day=None, exit_price=None,
                            outcome="no_fill", r_multiple=None, sessions_held=None)

    risk = (fill - stop) if long else (stop - fill)
    if risk <= 0:  # gapped through the stop before we were even in
        return TradeOutcome(**base, fill_day=bars[fill_idx].day, fill_price=fill, exit_day=bars[fill_idx].day,
                            exit_price=bars[fill_idx].close, outcome="loss",
                            r_multiple=-1.0, sessions_held=0)

    last = min(fill_idx + HOLD_LIMIT[playbook], len(bars))
    for j in range(fill_idx, last):
        b = bars[j]
        if playbook == "trend":   # dynamic: "closes back through EMA20"
            closes = [x.close for x in bars[max(0, j - LOOKBACK + 1):j + 1]]
            stop_level = ema(closes, 20)
        else:
            stop_level = stop
        stopped = b.close <= stop_level if long else b.close >= stop_level
        targeted = b.high >= target if long else b.low <= target
        if stopped:
            exit_price, outcome = b.close, "loss"
        elif targeted:
            exit_price, outcome = target, "win"
        else:
            continue
        r = ((exit_price - fill) if long else (fill - exit_price)) / risk
        return TradeOutcome(**base, fill_day=bars[fill_idx].day, fill_price=fill, exit_day=b.day,
                            exit_price=exit_price, outcome=outcome, r_multiple=r, sessions_held=j - fill_idx)

    if fill_idx + HOLD_LIMIT[playbook] > len(bars):
        return TradeOutcome(**base, fill_day=bars[fill_idx].day, fill_price=fill, exit_day=None, exit_price=None,
                            outcome="unresolved", r_multiple=None, sessions_held=None)
    b = bars[last - 1]
    r = ((b.close - fill) if long else (fill - b.close)) / risk
    return TradeOutcome(**base, fill_day=bars[fill_idx].day, fill_price=fill, exit_day=b.day,
                        exit_price=b.close, outcome="timeout", r_multiple=r, sessions_held=last - 1 - fill_idx)


def replay(bars, symbol: str, *, playbooks=tuple(SIGNALS)) -> list:
    """One trade at a time per playbook per symbol (mirrors 'one open idea per symbol')."""
    bars = tuple(bars)
    validate_bars(bars)
    unknown = set(playbooks) - set(SIGNALS)
    if unknown:
        raise ValueError(f"unknown playbooks: {sorted(unknown)}")
    outcomes = []
    busy_until = {p: -1 for p in playbooks}      # index of the last session a trade occupies
    for i in range(MIN_BARS - 1, len(bars) - 1):
        window = bars[max(0, i - LOOKBACK + 1):i + 1]
        for playbook in playbooks:
            if i <= busy_until[playbook]:
                continue
            signal = SIGNALS[playbook](window)
            if not isinstance(signal, Signal):
                continue
            result = _simulate(bars, i + 1, signal, playbook, symbol)
            outcomes.append(result)
            if result.exit_day is not None:
                busy_until[playbook] = next(k for k in range(i + 1, len(bars)) if bars[k].day == result.exit_day)
            elif result.outcome == "unresolved":
                busy_until[playbook] = len(bars)
            else:  # no_fill: pending trigger never hit; free after the fill window
                busy_until[playbook] = i + MAX_FILL_SESSIONS
    return outcomes


def summarize(outcomes) -> dict:
    """Per-playbook underlying-level statistics. Expectancy is in R, not dollars."""
    report = {}
    for playbook in sorted({o.playbook for o in outcomes}):
        mine = [o for o in outcomes if o.playbook == playbook]
        resolved = [o for o in mine if o.outcome in ("win", "loss", "timeout")]
        wins = [o for o in resolved if o.outcome == "win"]
        losses = [o for o in resolved if o.outcome == "loss"]
        timeouts = [o for o in resolved if o.outcome == "timeout"]
        rs = [o.r_multiple for o in resolved]
        entry = {
            "signals": len(mine),
            "no_fill": sum(o.outcome == "no_fill" for o in mine),
            "unresolved": sum(o.outcome == "unresolved" for o in mine),
            "resolved": len(resolved),
            "wins": len(wins), "losses": len(losses), "timeouts": len(timeouts),
            "win_rate": round(len(wins) / len(resolved), 4) if resolved else None,
            "expectancy_r": round(mean(rs), 4) if rs else None,
            "avg_win_r": round(mean(o.r_multiple for o in wins), 4) if wins else None,
            "avg_loss_r": round(mean(o.r_multiple for o in losses), 4) if losses else None,
            "median_sessions_held": median(o.sessions_held for o in resolved) if resolved else None,
            "sample_sufficient": len(resolved) >= MIN_SAMPLE,
            "negative_expectancy": (mean(rs) < 0) if rs else None,
        }
        if playbook == "breakout":
            fast_fail = [o for o in losses if o.sessions_held is not None and o.sessions_held <= FAILED_BREAKOUT_SESSIONS]
            entry["failed_breakout_rate"] = round(len(fast_fail) / len(resolved), 4) if resolved else None
        report[playbook] = entry
    return report


def outcome_dicts(outcomes) -> list:
    rows = []
    for o in outcomes:
        d = asdict(o)
        for k in ("signal_day", "fill_day", "exit_day"):
            d[k] = d[k].isoformat() if d[k] else None
        rows.append(d)
    return rows
