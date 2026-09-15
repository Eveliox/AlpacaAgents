"""Broker + ledger diagnostics. Authorization remains blocked pending evidence.

Pure `reconcile()` takes already-fetched data so it is fully testable; the
`build_risk_state()` wrapper does the read-only I/O. Every failed check is a
reason string; the rules engine rejects unreconciled state unconditionally.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re

from alpaca_agents.rules import RiskState
from .eastern import eastern_date
from .normalize import NormalizeResult, OCC, normalize_activities


@dataclass(frozen=True)
class ReconcileConfig:
    min_options_level: int = 2             # long calls/puts. Spreads typically need 3.
    history_lag_limit: timedelta = timedelta(minutes=15)
    clock_skew_limit: timedelta = timedelta(seconds=60)


@dataclass(frozen=True)
class Reconciliation:
    state: RiskState
    reasons: tuple
    details: dict


def _dec(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError
    d = Decimal(str(value))
    if not d.is_finite():
        raise ValueError
    return d


def _when(value) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError
    return parsed.astimezone(timezone.utc)


def trading_day_from_clock(clock: dict) -> date:
    """Current Eastern date; next_open must never reset today's loss latch early.

    This is a calendar date, not a verified exchange session. Closed markets are
    blocked separately until session-calendar integration exists.
    """
    stamp = _when(clock["timestamp"])
    if type(clock.get("is_open")) is not bool:
        raise ValueError
    return eastern_date(stamp)


def reconcile(*, account: dict, positions: list, open_orders: list, clock: dict, ledger_summary: dict,
              inventory: list, normalization: NormalizeResult | None, history_query: dict | None,
              order_attempts: tuple, reserved_cash: Decimal, pending_local: int, now: datetime,
              config: ReconcileConfig = ReconcileConfig()) -> Reconciliation:
    # Matching current positions cannot prove that fully closed losing trades,
    # late fees, external orders or unsettled proceeds are all accounted for.
    # No caller-supplied override can clear these unfinished prerequisites.
    reasons = ["SETTLEMENT_UNVERIFIED", "HISTORY_COVERAGE_AND_FEES_UNVERIFIED",
               "ORDER_PROVENANCE_UNVERIFIED", "CASH_ACCOUNT_ELIGIBILITY_UNVERIFIED"]
    details = {}
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("aware now required")
    now = now.astimezone(timezone.utc)

    # --- clock / trading day -------------------------------------------------
    trading_day = None
    try:
        skew = abs(now - _when(clock["timestamp"]))
        if skew > config.clock_skew_limit:
            reasons.append(f"BROKER_CLOCK_SKEW: {int(skew.total_seconds())}s")
        trading_day = trading_day_from_clock(clock)
        details["market_open"] = clock.get("is_open")
        if clock.get("is_open") is not True:
            reasons.append("MARKET_CLOSED")
    except (KeyError, ValueError, TypeError):
        reasons.append("CLOCK_UNAVAILABLE: cannot determine exchange trading day")
        trading_day = eastern_date(now)   # placeholder only; state is unreconciled anyway
    details["trading_day"] = trading_day.isoformat()

    # --- account -------------------------------------------------------------
    try:
        if account.get("status") != "ACTIVE":
            reasons.append(f"ACCOUNT_STATUS: {account.get('status')}")
        for flag in ("trading_blocked", "account_blocked", "trade_suspended_by_user"):
            if account.get(flag) is not False:
                reasons.append(f"ACCOUNT_FLAG: {flag}={account.get(flag)}")
        if account.get("pattern_day_trader") is not False:
            reasons.append("ACCOUNT_FLAG: pattern_day_trader")
        level = account.get("options_trading_level")
        if type(level) is not int or level < config.min_options_level:
            reasons.append(f"OPTIONS_LEVEL: {level!r} < {config.min_options_level}")
        details["options_trading_level"] = level
        multiplier = str(account.get("multiplier"))
        details["multiplier"] = multiplier
        if multiplier != "1":
            reasons.append(f"NOT_CASH_ACCOUNT: multiplier={multiplier}")
        candidates = []
        for key in ("cash", "non_marginable_buying_power", "options_buying_power"):
            if key in account:
                candidates.append(_dec(account[key]))
        if not candidates or "cash" not in account:
            raise ValueError
        settled = min(candidates) - _dec(reserved_cash)
        details["broker_cash_fields_min"] = str(min(candidates))
        details["reserved_cash"] = str(reserved_cash)
        if settled < 0:
            reasons.append("NEGATIVE_AVAILABLE_CASH after reservations")
            settled = Decimal(0)
    except (ValueError, TypeError, InvalidOperation, AttributeError):
        reasons.append("ACCOUNT_FIELDS_INVALID")
        settled = Decimal(0)

    # --- positions vs ledger -------------------------------------------------
    broker_positions = {}
    try:
        for p in positions:
            if p.get("asset_class") != "us_option":
                raise ValueError(f"NON_OPTION_POSITION: {p.get('asset_class')}")
            sym = p.get("symbol")
            if not isinstance(sym, str) or not OCC.fullmatch(sym):
                raise ValueError("NON_STANDARD_OPTION_POSITION")
            qty = _dec(p["qty"])
            if qty <= 0 or qty != qty.to_integral_value():
                raise ValueError(f"SHORT_OR_FRACTIONAL_POSITION: {sym}")
            if sym in broker_positions:
                raise ValueError(f"DUPLICATE_POSITION: {sym}")
            broker_positions[sym] = int(qty)
    except (ValueError, TypeError, InvalidOperation, KeyError, AttributeError) as exc:
        reasons.append(str(exc) or "POSITIONS_INVALID")
    ledger_positions = {}
    for row in inventory:
        ledger_positions[row["contract"]] = ledger_positions.get(row["contract"], 0) + row["quantity"]
    if broker_positions != ledger_positions:
        reasons.append(f"POSITION_MISMATCH: broker={broker_positions} ledger={ledger_positions}")
    details["open_positions"] = broker_positions

    # --- open orders ---------------------------------------------------------
    pending_broker = 0
    try:
        for o in open_orders:
            if o.get("legs"):
                raise ValueError("UNSUPPORTED_MULTILEG_OPEN_ORDER")
            if o.get("asset_class") != "us_option":
                raise ValueError(f"NON_OPTION_OPEN_ORDER: {o.get('asset_class')}")
            if o.get("side") == "buy":
                pending_broker += 1
            elif o.get("side") != "sell":
                raise ValueError("UNSUPPORTED_OPEN_ORDER_SIDE")
    except (ValueError, TypeError, AttributeError) as exc:
        reasons.append(str(exc) or "OPEN_ORDERS_INVALID")
    details["open_orders"] = len(open_orders)
    # Until IDs can be matched, assume disjoint broker/local reservations.
    pending = pending_broker + pending_local

    # --- history / ledger completeness --------------------------------------
    if normalization is None or not normalization.complete:
        reasons.append("HISTORY_INCOMPLETE: blocked activities or no normalization run")
        if normalization is not None:
            details["blocked_activities"] = list(normalization.blocked[:20])
    try:
        until = _when(history_query["until"])
        if until > now:
            reasons.append("HISTORY_FROM_FUTURE")
        if now - until > config.history_lag_limit:
            reasons.append(f"HISTORY_STALE: imported through {until.isoformat()}")
    except (KeyError, ValueError, TypeError):
        reasons.append("HISTORY_WINDOW_UNKNOWN")
    if ledger_summary.get("trading_day") != trading_day:
        reasons.append("LEDGER_DAY_MISMATCH")
    daily_loss = _dec(ledger_summary.get("realized_loss", "0"))
    breaker = ledger_summary.get("breaker_tripped") is True
    details["daily_realized_loss"] = str(daily_loss)
    details["breaker_tripped"] = breaker

    state = RiskState(trading_day=trading_day, observed_at=now, open_positions=len(broker_positions),
                      pending_entries=pending, daily_realized_loss=daily_loss, settled_cash=Decimal(0),
                      order_attempts=tuple(order_attempts), breaker_tripped=breaker, reconciled=not reasons)
    return Reconciliation(state, tuple(reasons), details)


def build_risk_state(client, ledger, store, *, now: datetime, order_attempts=(), reserved_cash=Decimal(0),
                     pending_local=0, config: ReconcileConfig = ReconcileConfig(), import_days: int = 7) -> Reconciliation:
    """Read-only I/O wrapper: import recent activities, normalize, fetch broker state, reconcile."""
    from .history import HistoryError, import_activities
    account = client.account()
    clock = client.clock()
    normalization, history_query = None, None
    try:
        report = import_activities(client, store, after=now - timedelta(days=import_days), until=now, now=now)
        history_query, records = store.exhausted_records(report["run_id"])
        normalization = normalize_activities(records, ledger, account_id=str(account.get("id")), recorded_at=now)
    except HistoryError:
        pass  # reflected as HISTORY_INCOMPLETE / HISTORY_WINDOW_UNKNOWN
    positions = client.positions()
    open_orders = client.open_orders()
    try:
        trading_day = trading_day_from_clock(clock)
    except (KeyError, ValueError, TypeError):
        trading_day = eastern_date(now)
    return reconcile(account=account, positions=positions, open_orders=open_orders, clock=clock,
                     ledger_summary=ledger.daily_summary(trading_day), inventory=ledger.inventory(),
                     normalization=normalization, history_query=history_query, order_attempts=order_attempts,
                     reserved_cash=reserved_cash, pending_local=pending_local, now=now, config=config)

