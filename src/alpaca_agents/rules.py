"""Pure, deterministic entry validation. No I/O, broker, or LLM dependencies."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import re
from typing import Any


@dataclass(frozen=True)
class RiskState:
    """Trusted executor snapshot, never supplied by the scanner.

    Counters include pending reservations. settled_cash is net of reserved
    premium/fees. daily_realized_loss is cumulative losses (wins do not offset).
    The executor must persist breaker_tripped through the trading day, including
    restarts, and roll it over only at a verified exchange trading-day boundary.
    """

    trading_day: date
    observed_at: datetime
    open_positions: int
    pending_entries: int
    daily_realized_loss: Decimal
    settled_cash: Decimal
    order_attempts: tuple[datetime, ...]
    breaker_tripped: bool = False
    reconciled: bool = False


def _number(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError("expected a number")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid number") from exc
    if not result.is_finite():
        raise ValueError("non-finite number")
    return result


def _positive(value: Any) -> Decimal:
    result = _number(value)
    if result <= 0:
        raise ValueError("expected a positive number")
    return result


def _aware(value: datetime) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def evaluate(idea: Any, state: RiskState, *, now: datetime,
             trading_day: date, kill_switch: bool) -> dict:
    """Return the handover's decision envelope. Only opening trades supported.

    now and trading_day come from the trusted controller/exchange calendar.
    Decisions are NOT executable authorization tokens. A future executor must
    atomically reserve capacity and revalidate before submission.
    """
    def decision(approved: bool, reason: str) -> dict:
        return {"approved": approved, "reason": reason, "idea": idea}

    def reject(reason: str) -> dict:
        return decision(False, reason)

    if kill_switch is not False:
        return reject("KILL_SWITCH: trading disabled")
    try:
        if not isinstance(state, RiskState) or state.reconciled is not True:
            return reject("STATE_UNAVAILABLE: reconciled executor snapshot required")
        if (not _aware(now) or not _aware(state.observed_at)
                or state.trading_day != trading_day
                or not timedelta(0) <= now - state.observed_at <= timedelta(seconds=60)):
            return reject("STALE_STATE: fresh snapshot for this trading day required")
        if (type(state.breaker_tripped) is not bool
                or any(type(n) is not int or n < 0 for n in (state.open_positions, state.pending_entries))
                or _number(state.daily_realized_loss) < 0
                or _number(state.settled_cash) < 0
                or not isinstance(state.order_attempts, tuple)
                or any(not _aware(t) or t > now for t in state.order_attempts)):
            return reject("INVALID_STATE: invalid risk counters")
        if state.breaker_tripped or _number(state.daily_realized_loss) >= 40:
            return reject("CIRCUIT_BREAKER: daily loss limit reached or latched")
        if state.open_positions + state.pending_entries >= 2:
            return reject("POSITION_LIMIT: two open or pending positions")
        if sum(now - timedelta(hours=1) < t <= now for t in state.order_attempts) >= 5:
            return reject("RATE_LIMIT: five order attempts in rolling hour")
        if not isinstance(idea, dict):
            return reject("INVALID_IDEA: expected a JSON object")
        if idea.get("action") != "open":
            return reject("UNSUPPORTED_ACTION: only swing entries supported; exits require a separate validated workflow")
        if idea.get("holding_style") != "swing":
            return reject("SWING_ONLY: holding_style must be swing")
        if idea.get("strategy") not in ("long_call", "long_put", "call_debit_spread", "put_debit_spread"):
            return reject("UNDEFINED_RISK: strategy not permitted")
        if type(idea.get("quantity")) is not int or idea["quantity"] != 1:
            return reject("SIZE_LIMIT: exactly one contract or one spread unit allowed")
        if idea.get("stop") is None or idea.get("target") is None:
            return reject("MISSING_STOP_TARGET: both stop and target required")
        symbol = idea.get("symbol")
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z]{1,6}", symbol):
            return reject("INVALID_SYMBOL: supported underlying ticker required")
        entry, stop, target = (_positive(idea[k]) for k in ("entry_trigger", "stop", "target"))
        direction = idea.get("direction")
        if not ((direction == "long" and stop < entry < target)
                or (direction == "short" and target < entry < stop)):
            return reject("INVALID_LEVELS: stop and target must bracket entry in bias direction")
        # Never trust the scanner's claimed reward:risk ratio.
        if abs(target - entry) < abs(entry - stop) or _positive(idea["reward_risk"]) < 1:
            return reject("REWARD_RISK: minimum 1:1 required")
        score = _number(idea["score"])
        if not 0 <= score <= 100 or not isinstance(idea.get("thesis"), str) or not idea["thesis"].strip():
            return reject("INVALID_METADATA: score 0..100 and nonempty thesis required")
        strategy = idea["strategy"]
        right = "call" if strategy in ("long_call", "call_debit_spread") else "put"
        if direction != ("long" if right == "call" else "short"):
            return reject("BIAS_MISMATCH: strategy must match directional bias")
        spread = strategy.endswith("spread")
        legs = idea.get("legs")
        if not isinstance(legs, list) or len(legs) != (2 if spread else 1):
            return reject("INVALID_LEGS: explicit one-leg long or two-leg debit spread required")
        strikes, expirations, sides = [], [], []
        for leg in legs:
            if not isinstance(leg, dict):
                return reject("INVALID_LEGS: each leg must be an object")
            if (leg.get("symbol") != symbol or leg.get("right") != right
                    or type(leg.get("ratio")) is not int or leg["ratio"] != 1
                    or type(leg.get("multiplier")) is not int or leg["multiplier"] != 100):
                return reject("INVALID_LEGS: standard same-underlying 1:1 options only")
            strike = _positive(leg["strike"])
            if strike * 1000 != (strike * 1000).to_integral_value() or strike >= 100000:
                return reject("INVALID_CONTRACT: strike not encodable as standard OCC symbol")
            expiry = date.fromisoformat(leg["expiration"])
            if expiry <= trading_day:
                return reject("INVALID_EXPIRATION: expiry must be after this trading day")
            strikes.append(strike)
            expirations.append(expiry)
            sides.append(leg.get("side"))
        if sides != (["buy", "sell"] if spread else ["buy"]):
            return reject("INVALID_LEGS: buy leg first, optional covered sell leg second")
        if spread and (expirations[0] != expirations[1]
                       or not (strikes[0] < strikes[1] if right == "call" else strikes[0] > strikes[1])):
            return reject("INVALID_SPREAD: same-expiry vertical debit spread required")
        # Limit debit is dollars per share; estimated cost is dollars per position.
        debit = _positive(idea["limit_debit"])
        fees = _number(idea["estimated_fees"])
        if fees < 0 or debit * 100 != (debit * 100).to_integral_value():
            return reject("INVALID_COST: nonnegative fees and cent-denominated debit required")
        premium = debit * 100 * idea["quantity"]
        if spread and debit >= abs(strikes[0] - strikes[1]):
            return reject("INVALID_SPREAD: debit must be below spread width")
        if _positive(idea["est_contract_cost"]) != premium:
            return reject("COST_MISMATCH: estimated premium must equal limit debit times multiplier times quantity")
        max_loss = premium + fees
        if max_loss > 100:
            return reject("RISK_LIMIT: maximum loss including fees exceeds $100")
        if max_loss > _number(state.settled_cash):
            return reject("INSUFFICIENT_CASH: settled unreserved cash required")
        return decision(True, "APPROVED: all entry checks passed")
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return reject("INVALID_INPUT: missing, malformed, or non-finite field")
