"""Walk-forward replay of signals.py over historical daily bars.

Honesty rules baked in:
- Signals see only bars up to and including the signal session (no look-ahead).
- Fills happen no earlier than the next session's open.
- Stop checks are close-based (matching the playbook's "closes back through"
  wording), so a gap through the stop exits at the worse close, not the stop.
- Same-session stop and target => the stop is assumed to have hit first.
- A fill already through the stop or at/beyond the target is NO TRADE: a
  rational executor would not enter, so it is counted separately and excluded
  from the statistics rather than booked as a synthetic -1R or a "win".
- `exit_reason` (stop / target / timeout) is independent of `outcome`, which is
  the SIGN of the R-multiple. A trend position closed by its rising EMA20 in
  profit is a win that exited on the stop rule; the old code called it a loss.
- Results are R-multiples on the UNDERLYING. A 1R underlying win does not mean
  the option made 1R; option outcomes need point-in-time option quotes.
- Beware the mean: gaps through stops are open-ended in R. Read median_r and
  profit_factor next to expectancy_r, and max_loss_r for what one gap costs.
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
    outcome: str            # win | loss | flat (sign of R) | no_trade | no_fill | unresolved
    r_multiple: float | None
    sessions_held: int | None
    exit_reason: str | None = None   # stop | target | timeout | fill_beyond_stop | fill_beyond_target


def _outcome(r: float) -> str:
    return "win" if r > 0 else "loss" if r < 0 else "flat"


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
    if risk <= 0:  # gapped through the stop before we were in: not a trade
        return TradeOutcome(**base, fill_day=bars[fill_idx].day, fill_price=fill, exit_day=bars[fill_idx].day,
                            exit_price=None, outcome="no_trade", r_multiple=None, sessions_held=0,
                            exit_reason="fill_beyond_stop")
    if (fill >= target) if long else (fill <= target):  # nothing left to capture
        return TradeOutcome(**base, fill_day=bars[fill_idx].day, fill_price=fill, exit_day=bars[fill_idx].day,
                            exit_price=None, outcome="no_trade", r_multiple=None, sessions_held=0,
                            exit_reason="fill_beyond_target")

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
            exit_price, why = b.close, "stop"
        elif targeted:
            exit_price, why = target, "target"
        else:
            continue
        r = ((exit_price - fill) if long else (fill - exit_price)) / risk
        return TradeOutcome(**base, fill_day=bars[fill_idx].day, fill_price=fill, exit_day=b.day,
                            exit_price=exit_price, outcome=_outcome(r), r_multiple=r, sessions_held=j - fill_idx,
                            exit_reason=why)

    if fill_idx + HOLD_LIMIT[playbook] > len(bars):
        return TradeOutcome(**base, fill_day=bars[fill_idx].day, fill_price=fill, exit_day=None, exit_price=None,
                            outcome="unresolved", r_multiple=None, sessions_held=None)
    b = bars[last - 1]
    r = ((b.close - fill) if long else (fill - b.close)) / risk
    return TradeOutcome(**base, fill_day=bars[fill_idx].day, fill_price=fill, exit_day=b.day,
                        exit_price=b.close, outcome=_outcome(r), r_multiple=r, sessions_held=last - 1 - fill_idx,
                        exit_reason="timeout")


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
            if result.exit_day is not None:   # includes no_trade fills: the session was consumed
                busy_until[playbook] = next(k for k in range(i + 1, len(bars)) if bars[k].day == result.exit_day)
            elif result.outcome == "unresolved":
                busy_until[playbook] = len(bars)
            else:  # no_fill: pending trigger never hit; free after the fill window
                busy_until[playbook] = i + MAX_FILL_SESSIONS
    return outcomes


def summarize(outcomes) -> dict:
    """Per-playbook underlying-level statistics. Expectancy is in R, not dollars.

    win/loss are by SIGN of R. exits (stop/target/timeout) say why a trade
    ended. no_trade fills are excluded from every statistic and counted.
    """
    report = {}
    for playbook in sorted({o.playbook for o in outcomes}):
        mine = [o for o in outcomes if o.playbook == playbook]
        resolved = [o for o in mine if o.outcome in ("win", "loss", "flat")]
        wins = [o for o in resolved if o.outcome == "win"]
        losses = [o for o in resolved if o.outcome == "loss"]
        rs = [o.r_multiple for o in resolved]
        gross_win = sum(r for r in rs if r > 0)
        gross_loss = -sum(r for r in rs if r < 0)
        entry = {
            "signals": len(mine),
            "no_fill": sum(o.outcome == "no_fill" for o in mine),
            "no_trade": sum(o.outcome == "no_trade" for o in mine),
            "no_trade_reasons": {why: sum(o.exit_reason == why for o in mine if o.outcome == "no_trade")
                                 for why in ("fill_beyond_stop", "fill_beyond_target")},
            "unresolved": sum(o.outcome == "unresolved" for o in mine),
            "resolved": len(resolved),
            "wins": len(wins), "losses": len(losses), "flat": len(resolved) - len(wins) - len(losses),
            "exits": {why: sum(o.exit_reason == why for o in resolved) for why in ("stop", "target", "timeout")},
            "win_rate": round(len(wins) / len(resolved), 4) if resolved else None,
            "target_hit_rate": round(sum(o.exit_reason == "target" for o in resolved) / len(resolved), 4) if resolved else None,
            "expectancy_r": round(mean(rs), 4) if rs else None,
            "median_r": round(median(rs), 4) if rs else None,
            "profit_factor": (round(gross_win / gross_loss, 4) if gross_loss > 0 else None) if rs else None,
            "avg_win_r": round(mean(o.r_multiple for o in wins), 4) if wins else None,
            "avg_loss_r": round(mean(o.r_multiple for o in losses), 4) if losses else None,
            "max_win_r": round(max(rs), 4) if rs else None,
            "max_loss_r": round(min(rs), 4) if rs else None,
            "median_sessions_held": median(o.sessions_held for o in resolved) if resolved else None,
            "sample_sufficient": len(resolved) >= MIN_SAMPLE,
            "negative_expectancy": (mean(rs) < 0) if rs else None,
        }
        if playbook == "breakout":
            fast_fail = [o for o in losses if o.exit_reason == "stop" and o.sessions_held is not None
                         and o.sessions_held <= FAILED_BREAKOUT_SESSIONS]
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
