"""Broker + ledger + journal -> RiskState. reconciled=True only when every check passes.

Pure `reconcile()` takes already-fetched data so it is fully testable; the
`build_risk_state()` wrapper does the read-only I/O. Every failed check is a
reason string; the rules engine rejects unreconciled state unconditionally.

The four verifications that used to be hard-blocked are now real checks:
- provenance: broker open orders <-> journal intents matched on client_order_id
- coverage:   contiguous exhausted import windows from account creation to now
- settlement: conservative unsettled-proceeds bound computed from the ledger
- cash acct:  multiplier == "1", no override
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from alpaca_agents.calendar import is_session
from alpaca_agents.rules import RiskState
from .eastern import eastern_date
from .normalize import NormalizeResult, OCC, normalize_activities

CLIENT_ID_PREFIX = "paper-"


@dataclass(frozen=True)
class ReconcileConfig:
    min_options_level: int = 2             # long calls/puts. Spreads typically need 3.
    history_lag_limit: timedelta = timedelta(minutes=15)
    clock_skew_limit: timedelta = timedelta(seconds=60)
    unsettled_calendar_days: int = 4       # T+1 plus weekend/holiday slack; deliberately over-conservative


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
    """Current Eastern date. Never next_open: rolling forward early would reset the loss latch."""
    stamp = _when(clock["timestamp"])
    if type(clock.get("is_open")) is not bool:
        raise ValueError
    return eastern_date(stamp)


def reconcile(*, account: dict, positions: list, open_orders: list, clock: dict, ledger_summary: dict,
              inventory: list, normalization: NormalizeResult | None, coverage_through: datetime | None,
              unsettled_proceeds: Decimal, live_intents: list, intent_orders: dict,
              order_attempts: tuple, now: datetime, config: ReconcileConfig = ReconcileConfig()) -> Reconciliation:
    """
    live_intents:  journal rows with status reserved|claimed (authorization_id, client_order_id, status, kind)
    intent_orders: {client_order_id: broker order dict | None} for every CLAIMED intent

    details["resolvable"] lists claimed intents whose broker record is terminal;
    the controller must journal.resolve() them and reconcile again.
    """
    reasons, details = [], {}
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("aware now required")
    now = now.astimezone(timezone.utc)

    # --- clock / trading day -------------------------------------------------
    try:
        skew = abs(now - _when(clock["timestamp"]))
        if skew > config.clock_skew_limit:
            reasons.append(f"BROKER_CLOCK_SKEW: {int(skew.total_seconds())}s")
        trading_day = trading_day_from_clock(clock)
        details["market_open"] = clock.get("is_open")
        if clock.get("is_open") is not True:
            reasons.append("MARKET_CLOSED")
        if not is_session(trading_day):
            reasons.append(f"NOT_A_SESSION: {trading_day.isoformat()} is not a scheduled NYSE session")
    except (KeyError, ValueError, TypeError):
        reasons.append("CLOCK_UNAVAILABLE: cannot determine exchange trading day")
        trading_day = eastern_date(now)
    details["trading_day"] = trading_day.isoformat()

    # --- account -------------------------------------------------------------
    broker_cash = Decimal(0)
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
        candidates = [_dec(account[k]) for k in ("cash", "non_marginable_buying_power", "options_buying_power") if k in account]
        if not candidates or "cash" not in account:
            raise ValueError
        broker_cash = min(candidates)
        details["broker_cash_fields_min"] = str(broker_cash)
        created = _when(account["created_at"])
    except (ValueError, TypeError, InvalidOperation, AttributeError, KeyError):
        reasons.append("ACCOUNT_FIELDS_INVALID")
        created = None

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

    # --- order provenance: broker open orders <-> journal intents -------------
    claimed = {i["client_order_id"]: i for i in live_intents if i["status"] == "claimed"}
    reserved = [i for i in live_intents if i["status"] == "reserved"]
    pending, resolvable = 0, []
    try:
        seen = set()
        for o in open_orders:
            if o.get("legs"):
                raise ValueError("UNSUPPORTED_MULTILEG_OPEN_ORDER")
            if o.get("asset_class") != "us_option":
                raise ValueError(f"NON_OPTION_OPEN_ORDER: {o.get('asset_class')}")
            cid = o.get("client_order_id")
            if cid not in claimed:
                raise ValueError(f"UNKNOWN_OPEN_ORDER: client_order_id not issued by this journal")
            if cid in seen:
                raise ValueError("DUPLICATE_OPEN_ORDER")
            seen.add(cid)
            if o.get("side") == "buy":
                pending += 1
            elif o.get("side") != "sell":
                raise ValueError("UNSUPPORTED_OPEN_ORDER_SIDE")
        for cid, intent in claimed.items():
            record = intent_orders.get(cid)
            if record is None:
                # A claimed body with no broker record is an incident: it may have
                # been sent and lost. It keeps its slot until resolved manually.
                reasons.append(f"CLAIMED_INTENT_WITHOUT_BROKER_RECORD: {intent['authorization_id']}")
                pending += 1
            elif cid not in seen:
                status = record.get("status")
                if status in ("new", "accepted", "pending_new", "partially_filled", "held", "accepted_for_bidding"):
                    reasons.append(f"OPEN_ORDER_NOT_LISTED: {cid}")
                    pending += 1
                else:
                    # Terminal at the broker but still claimed locally: the caller
                    # must run journal.resolve() and re-reconcile.
                    reasons.append(f"INTENT_UNRESOLVED: {cid} broker_status={status}")
                    resolvable.append({"authorization_id": intent["authorization_id"], "broker_status": status,
                                       "broker_order_id": record.get("id")})
    except (ValueError, TypeError, AttributeError) as exc:
        reasons.append(str(exc) or "OPEN_ORDERS_INVALID")
    details["open_orders"] = len(open_orders)
    details["resolvable"] = resolvable
    details["reserved_intents"] = len(reserved)   # the journal adds these itself at reserve/claim time

    # --- history coverage ----------------------------------------------------
    if normalization is None or not normalization.complete:
        reasons.append("HISTORY_INCOMPLETE: blocked activities or no normalization run")
        if normalization is not None:
            details["blocked_activities"] = list(normalization.blocked[:20])
    if created is None:
        reasons.append("HISTORY_COVERAGE_UNANCHORED: account created_at unavailable")
    elif coverage_through is None:
        reasons.append("HISTORY_COVERAGE_GAP: no contiguous import from account creation")
    else:
        if coverage_through > now:
            reasons.append("HISTORY_FROM_FUTURE")
        elif now - coverage_through > config.history_lag_limit:
            reasons.append(f"HISTORY_STALE: covered through {coverage_through.isoformat()}")
        details["coverage_through"] = coverage_through.isoformat()
    if ledger_summary.get("trading_day") != trading_day:
        reasons.append("LEDGER_DAY_MISMATCH")
    daily_loss = _dec(ledger_summary.get("realized_loss", "0"))
    breaker = ledger_summary.get("breaker_tripped") is True
    details["daily_realized_loss"] = str(daily_loss)
    details["breaker_tripped"] = breaker

    # --- settlement (conservative, ledger-derived) ---------------------------
    try:
        unsettled = _dec(unsettled_proceeds)
        if unsettled < 0:
            raise ValueError
    except (ValueError, TypeError, InvalidOperation):
        reasons.append("UNSETTLED_PROCEEDS_INVALID")
        unsettled = broker_cash
    settled = broker_cash - unsettled
    details["unsettled_proceeds_bound"] = str(unsettled)
    if settled < 0:
        settled = Decimal(0)
    details["settled_cash_bound"] = str(settled)

    state = RiskState(trading_day=trading_day, observed_at=now, open_positions=len(broker_positions),
                      pending_entries=pending, daily_realized_loss=daily_loss, settled_cash=settled,
                      order_attempts=tuple(order_attempts), breaker_tripped=breaker, reconciled=not reasons)
    return Reconciliation(state, tuple(reasons), details)


def build_risk_state(client, ledger, store, journal=None, *, now: datetime, order_attempts=(),
                     live_intents: list | None = None, config: ReconcileConfig = ReconcileConfig()) -> Reconciliation:
    """Read-only I/O wrapper. Imports from the last covered instant (or account creation).

    Pass live_intents (and journal=None) when calling from inside the journal's
    own transaction; opening the journal again there would deadlock.
    """
    from .history import HistoryError, import_activities
    account = client.account()
    clock = client.clock()
    normalization, coverage = None, None
    try:
        created = _when(account["created_at"])
        through = store.covered_through(created)
        # Overlap the previous window so exclusive after/until bounds cannot drop an activity.
        after = created if through is None else max(created, through - timedelta(minutes=5))
        report = import_activities(client, store, after=after, until=now, now=now, max_pages=1000)
        _, records = store.exhausted_records(report["run_id"])
        normalization = normalize_activities(records, ledger, account_id=str(account.get("id")), recorded_at=now)
        coverage = store.covered_through(created)
    except (HistoryError, KeyError, ValueError, TypeError):
        pass  # reflected as HISTORY_* reasons
    positions = client.positions()
    open_orders = client.open_orders()
    if live_intents is not None:
        live = list(live_intents)
    else:
        live = journal.live_intents() if journal is not None else []
    intent_orders = {i["client_order_id"]: client.order_by_client_id(i["client_order_id"])
                     for i in live if i["status"] == "claimed"}
    try:
        trading_day = trading_day_from_clock(clock)
    except (KeyError, ValueError, TypeError):
        trading_day = eastern_date(now)
    unsettled = ledger.sell_proceeds_since(trading_day - timedelta(days=config.unsettled_calendar_days))
    return reconcile(account=account, positions=positions, open_orders=open_orders, clock=clock,
                     ledger_summary=ledger.daily_summary(trading_day), inventory=ledger.inventory(),
                     normalization=normalization, coverage_through=coverage, unsettled_proceeds=unsettled,
                     live_intents=live, intent_orders=intent_orders, order_attempts=order_attempts,
                     now=now, config=config)
