"""Deterministic exit decisions for held long single-leg options. Pure; no I/O.

Inputs are the ledger's lot (contract, quantity, basis), the broker's option
mark, the last completed session's underlying close, and the entry idea's
levels and exit_plan. Output is an exit decision the journal's prepare_exit()
validates against inventory, or None with a note.

Rules, in priority order (first hit wins):
  underlying_stop   close beyond the idea's stop against the position
  premium_stop      mark*100 <= basis * (1 - premium_stop_pct/100)
  underlying_target close beyond the idea's target in favour of the position
  time_stop         DTE <= time_stop_dte, or NYSE sessions held >= time_stop_sessions

The exit order is a DAY LIMIT sell at 95% of the mark (floored at $0.01): a
deliberately marketable limit for paper. "Close back through EMA20" style
discretionary rules from the playbooks are NOT implemented here; those need the
indicator pipeline and are tracked in the README.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
import re

from alpaca_agents.calendar import sessions_between

OCC = re.compile(r"[A-Z]{1,6}[0-9]{6}[CP][0-9]{8}")
CENT = Decimal("0.01")


@dataclass(frozen=True)
class HeldOption:
    contract: str
    quantity: int
    basis: Decimal                 # remaining_basis for the lot, dollars, includes entry fees
    mark: Decimal | None           # broker current_price per share (may be missing)
    underlying_close: Decimal | None
    entry_day: date


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


def exit_limit(mark: Decimal) -> str:
    price = (mark * Decimal("0.95")).quantize(CENT, rounding=ROUND_DOWN)
    return format(max(price, CENT), ".2f")


def evaluate_exit(held: HeldOption, idea: dict, *, trading_day: date) -> tuple[dict | None, str]:
    """Return (decision, note). decision is None when no rule fires or inputs are unusable."""
    try:
        if not isinstance(held, HeldOption) or not OCC.fullmatch(held.contract):
            return None, "invalid position"
        if type(held.quantity) is not int or held.quantity < 1 or type(trading_day) is not date:
            return None, "invalid quantity or day"
        if type(held.entry_day) is not date or held.entry_day > trading_day:
            return None, "invalid entry day"
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
        close = None if held.underlying_close is None else _dec(held.underlying_close)

        reason, detail = None, None
        if close is not None:
            if (direction == "long" and close <= stop) or (direction == "short" and close >= stop):
                reason, detail = "underlying_stop", f"close {close} vs stop {stop}"
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
        if mark is None:
            return None, f"{reason} fired but no mark to price an exit; manual attention required"
        return {"action": "close", "contract": held.contract, "quantity": held.quantity,
                "limit_price": exit_limit(mark), "exit_reason": reason, "detail": detail,
                "trading_day": trading_day.isoformat()}, reason
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return None, "malformed idea or position"
