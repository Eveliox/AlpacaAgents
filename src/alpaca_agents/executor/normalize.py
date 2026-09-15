"""Alpaca account activities -> FillLedger. Fail-closed on anything unsupported.

Booked:   FILL on standard long-option contracts (buy opens/adds, sell closes),
          FEE (regulatory fees billed separately; counted fully as loss).
Ignored:  pure cash movements with no option P&L effect.
Blocked:  everything else (OPEXP/OPASN/OPEXC lifecycle events, equity fills from
          assignment, shorts, unknown types). A blocked FILL stops further fill
          processing so lifecycles are never misattributed.
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import re

from .eastern import eastern_date
from .fills import FillLedger, OptionFill
from .ledger import LedgerError

OCC = re.compile(r"[A-Z]{1,6}[0-9]{6}[CP][0-9]{8}")
IGNORED_TYPES = frozenset({"CSD", "CSW", "JNLC", "INT", "DIV"})


@dataclass(frozen=True)
class NormalizeResult:
    applied_fills: int
    applied_fees: int
    replayed: int
    ignored: int
    blocked: tuple      # {activity_id, activity_type, reason}
    complete: bool      # True only when nothing was blocked


def _decimal(value) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("bad number")
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("bad number")
    return result


def _int(value) -> int:
    result = _decimal(value)
    if result != result.to_integral_value() or result <= 0 or result > 10000:
        raise ValueError("bad quantity")
    return int(result)


def _when(value) -> datetime:
    if not isinstance(value, str):
        raise ValueError("bad time")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("naive time")
    return parsed.astimezone(timezone.utc)


def normalize_activities(records, ledger: FillLedger, *, account_id: str, recorded_at: datetime) -> NormalizeResult:
    fills, fees, blocked, ignored = [], [], [], 0
    for record in records:
        kind = record.get("activity_type") if isinstance(record, dict) else None
        rid = record.get("id") if isinstance(record, dict) else None
        if kind == "FILL":
            fills.append(record)
        elif kind == "FEE":
            fees.append(record)
        elif kind in IGNORED_TYPES:
            ignored += 1
        else:
            blocked.append({"activity_id": rid, "activity_type": kind, "reason": "UNSUPPORTED_ACTIVITY_TYPE"})

    applied = replayed = 0
    try:
        fills.sort(key=lambda r: (_when(r["transaction_time"]), str(r["id"])))
    except (KeyError, ValueError, TypeError):
        blocked.append({"activity_id": None, "activity_type": "FILL", "reason": "UNSORTABLE_FILLS"})
        fills = []

    halted = False
    for record in fills:
        rid = record.get("id")
        if halted:
            blocked.append({"activity_id": rid, "activity_type": "FILL", "reason": "NOT_APPLIED_AFTER_BLOCK"})
            continue
        try:
            contract = record["symbol"]
            if not isinstance(contract, str) or not OCC.fullmatch(contract):
                raise ValueError("NON_STANDARD_OPTION_FILL")
            if record.get("type") not in ("fill", "partial_fill"):
                raise ValueError("UNSUPPORTED_FILL_TYPE")
            side = record.get("side")
            qty = _int(record["qty"])
            price = _decimal(record["price"])
            if price < 0:
                raise ValueError("NEGATIVE_PRICE")
            when = _when(record["transaction_time"])
            # Overlapping import windows re-present booked activities. A replay
            # must be matched to its ORIGINAL lifecycle (which may be closed by
            # now), never re-derived from current inventory.
            previous = ledger.recorded_execution(str(rid))
            lifecycle = previous["position_id"] if previous else ledger.open_lifecycle(contract)
            if side == "buy":
                position_id = lifecycle or f"{account_id}:{rid}"
                ledger_side = "buy_to_open"
            elif side == "sell":
                if lifecycle is None:
                    raise ValueError("SELL_WITHOUT_INVENTORY")   # short or missing opening history
                position_id, ledger_side = lifecycle, "sell_to_close"
            else:
                raise ValueError("UNSUPPORTED_SIDE")
            fill = OptionFill(str(rid), position_id, contract, ledger_side, qty, price * 100 * qty,
                              Decimal(0), when, eastern_date(when))
            if ledger.record(fill, recorded_at=recorded_at):
                applied += 1
            else:
                replayed += 1
        except (KeyError, ValueError, TypeError, InvalidOperation) as exc:
            blocked.append({"activity_id": rid, "activity_type": "FILL", "reason": str(exc) or type(exc).__name__})
            halted = True
        except LedgerError as exc:
            blocked.append({"activity_id": rid, "activity_type": "FILL", "reason": f"LEDGER_REJECTED: {exc}"})
            halted = True

    applied_fees = 0
    for record in fees:
        rid = record.get("id")
        try:
            amount = -_decimal(record["net_amount"])
            if amount <= 0:
                raise ValueError("UNEXPECTED_FEE_CREDIT")
            day = date.fromisoformat(record["date"])
            if ledger.record_fee(str(rid), trading_day=day, amount=amount, recorded_at=recorded_at):
                applied_fees += 1
            else:
                replayed += 1
        except (KeyError, ValueError, TypeError, InvalidOperation) as exc:
            blocked.append({"activity_id": rid, "activity_type": "FEE", "reason": str(exc) or type(exc).__name__})
        except LedgerError as exc:
            blocked.append({"activity_id": rid, "activity_type": "FEE", "reason": f"LEDGER_REJECTED: {exc}"})

    return NormalizeResult(applied, applied_fees, replayed, ignored, tuple(blocked), not blocked)
