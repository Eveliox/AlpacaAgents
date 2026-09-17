"""Deterministic exit decisions for held long single-leg options. Pure; no I/O.

Inputs are the ledger's lot (contract, quantity, basis), the broker's option
mark, the last completed session's underlying close, and the entry idea's
levels and exit_plan. Output is an exit decision the journal's prepare_exit()
validates against inventory, or None with a note.

Rules, in priority order (first hit wins):
  underlying_stop   close beyond the idea's stop against the position
  underlying_rule   the playbook's discretionary rule, when bars are supplied:
                    "close back through EMA20 against position" -> close on the
                    wrong side of EMA20. The bounce/breakout rules are already
                    encoded as the idea's stop (swing low / range high).
  premium_stop      mark*100 <= basis * (1 - premium_stop_pct/100)
  underlying_target close beyond the idea's target in favour of the position
  time_stop         DTE <= time_stop_dte, or NYSE sessions held >= time_stop_sessions

The exit order is a DAY LIMIT sell priced at the fresh NBBO bid when one is
available (Alpaca paper fills a sell limit only when limit <= best bid), else
at 95% of the broker mark (floored at $0.01) as a fallback.

Same-session exits. On the session a position was opened, the underlying
rules have nothing new to say (their close is the signal session's, which by
construction is not through the stop), so only the PREMIUM STOP is evaluated
that day, and only as protection: never target or time exits. A same-session
exit is a day trade; a margin account under $25k may make at most 3 in any
5 business days before PDT restrictions apply, so the caller passes the count
already used in the rolling window and the rule refuses the 4th. A long
option's loss is bounded by its premium either way; this rule only decides
whether the -50% stop protects on day one (yes, within the budget) or the
position rides to the next session (when the budget is spent).
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
import re

from alpaca_agents.calendar import sessions_between
from alpaca_agents.scanner.indicators import ema

OCC = re.compile(r"[A-Z]{1,6}[0-9]{6}[CP][0-9]{8}")
CENT = Decimal("0.01")
PDT_DAY_TRADE_LIMIT = 3     # day trades allowed per rolling 5 business days under $25k equity
PDT_WINDOW_SESSIONS = 5


@dataclass(frozen=True)
class HeldOption:
    contract: str
    quantity: int
    basis: Decimal                 # remaining_basis for the lot, dollars, includes entry fees
    mark: Decimal | None           # broker current_price per share (may be missing)
    underlying_close: Decimal | None
    entry_day: date
    bars: tuple = ()               # ascending completed daily bars ending on the same session as underlying_close
    bid: Decimal | None = None     # fresh two-sided NBBO bid, if the controller could get one


def _dec(value) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError
    d = Decimal(str(value))
    if not d.is_finite():
        raise ValueError
    return d


def weekdays_between(start: date, end: date) -> int:
    """Scheduled NYSE sessions in (start, end]. Kept under its historical name."""
    return sessions_between(start, end)


def contract_expiry(contract: str) -> date:
    return date(2000 + int(contract[-15:-13]), int(contract[-13:-11]), int(contract[-11:-9]))


def exit_limit(mark: Decimal | None, bid: Decimal | None = None) -> str:
    """Bid if fresh and positive (marketable by definition); else 95% of mark; floored at $0.01."""
    if bid is not None and bid > 0:
        return format(bid.quantize(CENT, rounding=ROUND_DOWN), ".2f")
    price = (mark * Decimal("0.95")).quantize(CENT, rounding=ROUND_DOWN)
    return format(max(price, CENT), ".2f")


def evaluate_exit(held: HeldOption, idea: dict, *, trading_day: date, day_trades_used: int = 0) -> tuple[dict | None, str]:
    """Return (decision, note). decision is None when no rule fires or inputs are unusable.

    day_trades_used: same-session round trips already booked in the rolling
    PDT window (including today). Only consulted for a same-session exit.
    """
    try:
        if not isinstance(held, HeldOption) or not OCC.fullmatch(held.contract):
            return None, "invalid position"
        if type(held.quantity) is not int or held.quantity < 1 or type(trading_day) is not date:
            return None, "invalid quantity or day"
        if type(held.entry_day) is not date or held.entry_day > trading_day:
            return None, "invalid entry day"
        if type(day_trades_used) is not int or day_trades_used < 0:
            return None, "invalid day-trade count"
        same_session = held.entry_day == trading_day
        if not isinstance(idea, dict) or idea.get("strategy") not in ("long_call", "long_put"):
            return None, "exit rules cover long single-leg ideas only"
        plan = idea.get("exit_plan")
        if not isinstance(plan, dict):
            return None, "idea has no exit_plan"
        direction = idea.get("direction")
        if direction not in ("long", "short"):
            return None, "invalid direction"
        stop, target = _dec(idea["stop"]), _dec(idea["target"])
        basis = _dec(held.basis)
        if basis <= 0:
            return None, "invalid basis"
        expiry = contract_expiry(held.contract)
        dte = (expiry - trading_day).days
        mark = None if held.mark is None else _dec(held.mark)
        if mark is not None and mark < 0:
            return None, "negative mark"
        bid = None if held.bid is None else _dec(held.bid)
        if bid is not None and bid <= 0:
            bid = None
        close = None if held.underlying_close is None else _dec(held.underlying_close)

        reason, detail = None, None
        if same_session:
            # Only the premium stop has same-session information; it is protection, not profit-taking.
            pct = plan.get("premium_stop_pct")
            if mark is not None and type(pct) is int and 0 < pct < 100:
                floor = (basis * (100 - pct) / 100).quantize(CENT, rounding=ROUND_HALF_UP)
                if mark * 100 <= floor:
                    if day_trades_used >= PDT_DAY_TRADE_LIMIT:
                        return None, (f"premium_stop fired (mark {mark}*100 <= {floor}) but {day_trades_used} day trades already used "
                                      f"in {PDT_WINDOW_SESSIONS} sessions; holding to next session (loss bounded by premium)")
                    reason = "premium_stop"
                    detail = (f"mark {mark}*100 <= {floor} ({pct}% of basis {basis}); same-session protective exit, "
                              f"day trade {day_trades_used + 1} of {PDT_DAY_TRADE_LIMIT}")
            if reason is None:
                return None, f"opened this session; holding (premium stop only today, mark {mark})"
        if reason is None and close is not None:
            if (direction == "long" and close <= stop) or (direction == "short" and close >= stop):
                reason, detail = "underlying_stop", f"close {close} vs stop {stop}"
        if reason is None and close is not None and held.bars:
            rule = plan.get("underlying_stop_rule")
            if rule == "close back through EMA20 against position":
                closes = [b.close for b in held.bars]
                if len(closes) >= 20 and held.bars[-1].day <= trading_day:
                    e20 = Decimal(str(ema(closes, 20))).quantize(CENT, rounding=ROUND_HALF_UP)
                    if (direction == "long" and close < e20) or (direction == "short" and close > e20):
                        reason, detail = "underlying_rule", f"close {close} through EMA20 {e20} against {direction}"
        if reason is None and mark is not None:
            pct = plan.get("premium_stop_pct")
            if type(pct) is int and 0 < pct < 100:
                floor = (basis * (100 - pct) / 100).quantize(CENT, rounding=ROUND_HALF_UP)
                if mark * 100 <= floor:
                    reason, detail = "premium_stop", f"mark {mark}*100 <= {floor} ({pct}% of basis {basis})"
        if reason is None and close is not None:
            if (direction == "long" and close >= target) or (direction == "short" and close <= target):
                reason, detail = "underlying_target", f"close {close} vs target {target}"
        if reason is None:
            dte_limit = plan.get("time_stop_dte")
            if type(dte_limit) is int and dte <= dte_limit:
                reason, detail = "time_stop", f"{dte} DTE <= {dte_limit}"
            sessions_limit = plan.get("time_stop_sessions")
            if reason is None and type(sessions_limit) is int:
                held_sessions = weekdays_between(held.entry_day, trading_day)
                if held_sessions >= sessions_limit:
                    reason, detail = "time_stop", f"held {held_sessions} sessions >= {sessions_limit}"
        if reason is None:
            return None, f"hold: {dte} DTE, mark {mark}, close {close}"
        if mark is None and bid is None:
            return None, f"{reason} fired but no bid or mark to price an exit; manual attention required"
        return {"action": "close", "contract": held.contract, "quantity": held.quantity,
                "limit_price": exit_limit(mark, bid), "exit_reason": reason,
                "detail": detail + (f"; priced at bid {bid}" if bid is not None else "; priced at 95% of mark"),
                "trading_day": trading_day.isoformat()}, reason
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return None, "malformed idea or position"
