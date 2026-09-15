"""Normalize provider data into Layer 1's input contract, never into an order."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re

from alpaca_agents.scanner.contracts import OptionQuote, is_liquid
from alpaca_agents.scanner.indicators import Bar, validate_bars
from alpaca_agents.scanner.scan import INDEX_ETFS, SymbolSnapshot
from .client import MarketDataError, symbol_checked

MAX_QUOTE_AGE = timedelta(seconds=120)
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class _QuoteRejected(ValueError):
    pass


@dataclass(frozen=True)
class SnapshotResult:
    snapshot: SymbolSnapshot
    diagnostics: tuple[dict, ...]
    observed_at: datetime
    oldest_quote_at: datetime


def _decimal(value) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError("invalid numeric field")
    result = Decimal(str(value))
    if not result.is_finite() or abs(result) > 1_000_000_000:
        raise ValueError("invalid numeric range")
    return result


def _time(value: int, units_per_second: int) -> datetime:
    if type(value) is not int or value <= 0:
        raise ValueError("invalid provider timestamp")
    return EPOCH + timedelta(microseconds=value * 1_000_000 // units_per_second)


def bars_from_payload(payload: dict, *, symbol: str) -> list:
    """Parse one bounded daily-aggregate response into Bars (no length/session checks)."""
    if payload.get("ticker") != symbol or payload.get("adjusted") is not True or payload.get("next_url"):
        raise ValueError("wrong symbol, unadjusted or incomplete bars")
    rows = payload["results"]
    if not isinstance(rows, list):
        raise ValueError("missing bars")
    bars = []
    for row in rows:
        # US daily windows start at ET midnight (04:00/05:00 UTC), on the
        # same calendar date. This is not a generic global-exchange adapter.
        day = _time(row["t"], 1_000).date()  # Provider aggregate timestamps are milliseconds.
        values = [float(_decimal(row[k])) for k in ("o", "h", "l", "c", "v")]
        bars.append(Bar(day, *values))
    return bars


def normalize_bars(payload: dict, *, symbol: str, completed_session: date) -> tuple[Bar, ...]:
    try:
        bars = bars_from_payload(payload, symbol=symbol)
        validate_bars(bars)
        if bars[-1].day != completed_session:
            raise ValueError("last bar not requested completed session")
        return tuple(bars)
    except (ValueError, KeyError, TypeError, AttributeError, InvalidOperation, OverflowError):
        raise MarketDataError("Invalid/incomplete daily bars for requested completed session") from None


def normalize_chain(pages, *, symbol: str, now: datetime) -> tuple[tuple[OptionQuote, ...], tuple[dict, ...], datetime]:
    quotes, issues, stamps = [], [], []
    for page in pages:
        for row in page["results"]:
            ticker = None
            try:
                details = row["details"]
                ticker = details["ticker"]
                right = details["contract_type"]
                expiration = date.fromisoformat(details["expiration_date"])
                strike = _decimal(details["strike_price"])
                multiplier = details["shares_per_contract"]
                if (right not in ("call", "put") or multiplier != 100 or isinstance(multiplier, bool)
                        or details.get("additional_underlyings") or not 0 < strike < 100000
                        or strike * 1000 != (strike * 1000).to_integral_value()):
                    raise _QuoteRejected("UNSUPPORTED_CONTRACT")
                expected = f"O:{symbol}{expiration:%y%m%d}{'C' if right == 'call' else 'P'}{int(strike * 1000):08d}"
                if ticker != expected or row["underlying_asset"]["ticker"] != symbol:
                    raise _QuoteRejected("CONTRACT_IDENTITY_MISMATCH")
                if not 30 <= (expiration - now.date()).days <= 45:
                    raise _QuoteRejected("DTE_OUTSIDE_WINDOW")
                last_quote = row["last_quote"]
                if last_quote["timeframe"] != "REAL-TIME":
                    raise _QuoteRejected("REALTIME_QUOTE_REQUIRED")
                quote_at = _time(last_quote["last_updated"], 1_000_000_000)  # Nanoseconds.
                if not timedelta(0) <= now - quote_at <= MAX_QUOTE_AGE:
                    raise _QuoteRejected("STALE_OR_FUTURE_QUOTE")
                if any(type(last_quote[k]) is not int or last_quote[k] <= 0 for k in ("bid_size", "ask_size")):
                    raise _QuoteRejected("TWO_SIDED_SIZE_REQUIRED")
                quote = OptionQuote(expiration, strike, right, _decimal(last_quote["bid"]),
                                    _decimal(last_quote["ask"]), float(_decimal(row["greeks"]["delta"])), row["open_interest"])
                if not is_liquid(quote):
                    raise _QuoteRejected("LIQUIDITY_DELTA_OR_PRICE_GATE")
                quotes.append(quote)
                stamps.append(quote_at)
                continue
            except _QuoteRejected as exc:
                reason = str(exc)  # Only our own fixed rejection codes.
            except (ValueError, KeyError, TypeError, AttributeError, InvalidOperation, OverflowError):
                reason = "MISSING_OR_MALFORMED_QUOTE_FIELDS"
            safe_ticker = ticker if isinstance(ticker, str) and re.fullmatch(r"O:[A-Z]{1,6}\d{6}[CP]\d{8}", ticker) else None
            issues.append({"symbol": symbol, "contract": safe_ticker, "reason": reason})
    if not quotes:
        error = MarketDataError(f"No usable realtime option quotes; rejected_contracts={len(issues)}")
        error.diagnostics = tuple(issues)
        raise error
    return tuple(quotes), tuple(issues), min(stamps)


def load_snapshot(client, *, symbol: str, completed_session: date, now: datetime,
                  next_earnings: date | None = None, clock=None) -> SnapshotResult:
    symbol_checked(symbol)
    if (not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
            or type(completed_session) is not date):
        raise MarketDataError("Aware observation timestamp and completed session date required")
    now = now.astimezone(timezone.utc)
    # Conservative manual-session boundary until an exchange-calendar controller
    # exists. Never load today's partial daily bar with a current option quote.
    if not 1 <= (now.date() - completed_session).days <= 4:
        raise MarketDataError("Completed session must precede today by 1-4 calendar days")
    etf = symbol in INDEX_ETFS
    if etf and next_earnings is not None:
        raise MarketDataError("Supported index ETFs use explicit inapplicable earnings status")
    if not etf and (type(next_earnings) is not date or next_earnings <= now.date()):
        raise MarketDataError("Verified future earnings date required for individual stocks")
    bars = normalize_bars(client.daily_bars(symbol, start=completed_session - timedelta(days=450), end=completed_session),
                          symbol=symbol, completed_session=completed_session)
    pages = client.option_chain(symbol, expiration_min=now.date() + timedelta(days=30),
                                expiration_max=now.date() + timedelta(days=45))
    if clock is not None:
        finished = clock()
        if (not isinstance(finished, datetime) or finished.tzinfo is None or finished.utcoffset() is None
                or finished < now or finished.astimezone(timezone.utc).date() != now.date()):
            raise MarketDataError("Clock changed during snapshot retrieval")
        now = finished.astimezone(timezone.utc)
    chain, issues, oldest = normalize_chain(pages, symbol=symbol, now=now)
    snapshot = SymbolSnapshot(symbol, bars, chain, next_earnings, iv_rank=None,
                              earnings_not_applicable=etf, valuation_day=now.date())
    return SnapshotResult(snapshot, issues, now, oldest)
