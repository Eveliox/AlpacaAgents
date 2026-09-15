"""Option structure selection with the playbook's liquidity and delta gates."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

MIN_OPEN_INTEREST = 500
MAX_SPREAD_OF_MID = Decimal("0.10")
DTE_MIN, DTE_MAX = 30, 45
LONG_DELTA = (0.35, 0.45, 0.40)      # min, max, ideal
SPREAD_LONG_DELTA = (0.35, 0.45, 0.40)
SPREAD_SHORT_DELTA = (0.20, 0.30, 0.25)


@dataclass(frozen=True)
class OptionQuote:
    expiration: date
    strike: Decimal
    right: str          # "call" or "put"
    bid: Decimal
    ask: Decimal
    delta: float        # signed; puts negative
    open_interest: int


@dataclass(frozen=True)
class Structure:
    strategy: str       # rules-engine structure name
    legs: tuple         # (buy,) or (buy, sell)
    limit_debit: Decimal
    expiration: date
    max_profit: Decimal | None  # spreads only


def is_liquid(q: OptionQuote) -> bool:
    if q.open_interest < MIN_OPEN_INTEREST or q.bid <= 0 or q.ask < q.bid:
        return False
    mid = (q.bid + q.ask) / 2
    return (q.ask - q.bid) <= mid * MAX_SPREAD_OF_MID


def _eligible(chain, right, as_of):
    return [q for q in chain if q.right == right and DTE_MIN <= (q.expiration - as_of).days <= DTE_MAX and is_liquid(q)]


def _closest(quotes, band):
    low, high, ideal = band
    inside = [q for q in quotes if low <= abs(q.delta) <= high]
    if not inside:
        return None
    return min(inside, key=lambda q: (abs(abs(q.delta) - ideal), q.expiration, q.strike))


def select_long(chain, right, as_of) -> Structure | None:
    quote = _closest(_eligible(chain, right, as_of), LONG_DELTA)
    if quote is None:
        return None
    name = "long_call" if right == "call" else "long_put"
    return Structure(name, (quote,), quote.ask, quote.expiration, None)


def select_debit_spread(chain, right, as_of, max_debit: Decimal) -> Structure | None:
    """Buy ~0.40 delta, sell ~0.25 delta, same expiry; max profit must be >= debit."""
    eligible = _eligible(chain, right, as_of)
    best = None
    for expiration in sorted({q.expiration for q in eligible}):
        same = [q for q in eligible if q.expiration == expiration]
        buy = _closest(same, SPREAD_LONG_DELTA)
        if buy is None:
            continue
        further = [q for q in same if (q.strike > buy.strike if right == "call" else q.strike < buy.strike)]
        sell = _closest(further, SPREAD_SHORT_DELTA)
        if sell is None:
            continue
        debit = buy.ask - sell.bid           # conservative: pay the ask, receive the bid
        width = abs(buy.strike - sell.strike)
        if debit <= 0 or debit >= width:
            continue
        max_profit = width - debit
        if max_profit < debit or debit * 100 > max_debit:
            continue
        fit = abs(abs(buy.delta) - SPREAD_LONG_DELTA[2]) + abs(abs(sell.delta) - SPREAD_SHORT_DELTA[2])
        if best is None or fit < best[0]:
            best = (fit, Structure("call_debit_spread" if right == "call" else "put_debit_spread",
                                   (buy, sell), debit, expiration, max_profit))
    return best[1] if best else None
