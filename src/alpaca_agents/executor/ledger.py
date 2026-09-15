"""Persistent closed-trade accounting foundation; no broker access or authorization.

Inputs belong to a future trusted fill reconciler, NEVER to the scanner. This
module does not infer settled cash, group broker legs, or produce RiskState.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext
import json
from pathlib import Path
import sqlite3


class LedgerError(ValueError):
    pass


def _money(value: Decimal) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise LedgerError("Amounts must be finite nonnegative Decimals")
    # Bounds keep arithmetic exact under a controlled decimal precision.
    if value > Decimal("1000000000") or value.as_tuple().exponent < -6:
        raise LedgerError("Amount exceeds supported precision or range")
    return value


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise LedgerError("Timezone-aware timestamp required")
    return value.astimezone(timezone.utc).isoformat()


def _identifier(value: str):
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or not all(
        c.isascii() and (c.isalnum() or c in "-_.:") for c in value
    ):
        raise LedgerError("Stable ASCII identifier required")


@dataclass(frozen=True)
class ClosedTrade:
    """One fully closed position lifecycle, with aggregate DOLLAR amounts.

    entry_debit/exit_credit are total cash amounts, not per-share prices. fees
    includes actual entry AND exit fees. The reconciler must aggregate partial
    fills and all spread legs before supplying this record. trading_day is the
    exchange session date of the final close, supplied by a trusted calendar.
    Do not send executions as if each were a separate closed trade.
    """

    close_id: str
    position_id: str
    trading_day: date
    opened_at: datetime
    closed_at: datetime
    entry_debit: Decimal
    exit_credit: Decimal
    fees: Decimal
    reason: str

    def canonical(self) -> dict:
        _identifier(self.close_id)
        _identifier(self.position_id)
        if type(self.trading_day) is not date:
            raise LedgerError("Exchange trading day required")
        opened, closed = _timestamp(self.opened_at), _timestamp(self.closed_at)
        if self.closed_at < self.opened_at:
            raise LedgerError("Close precedes entry")
        if self.reason not in ("stop_hit", "target_hit", "manual_close", "expiration"):
            raise LedgerError("Unsupported close reason")
        amounts = [_money(value) for value in (self.entry_debit, self.exit_credit, self.fees)]
        if amounts[0] == 0:
            raise LedgerError("Positive entry debit required")
        return {
            "close_id": self.close_id, "position_id": self.position_id,
            "trading_day": self.trading_day.isoformat(), "opened_at": opened,
            "closed_at": closed, "entry_debit": format(amounts[0], "f"),
            "exit_credit": format(amounts[1], "f"), "fees": format(amounts[2], "f"),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class DailySummary:
    trading_day: date
    realized_pnl: Decimal
    realized_loss: Decimal
    breaker_tripped: bool
    closed_trades: int


class TradeLedger:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS closed_trades (
                close_id TEXT PRIMARY KEY, position_id TEXT UNIQUE NOT NULL,
                trading_day TEXT NOT NULL, payload TEXT NOT NULL, pnl TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS breakers (
                trading_day TEXT PRIMARY KEY, tripped_at TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS ledger_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL, event TEXT NOT NULL, payload TEXT NOT NULL
            )""")

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _summary(db, day: date) -> DailySummary:
        rows = db.execute("SELECT pnl FROM closed_trades WHERE trading_day=?", (day.isoformat(),)).fetchall()
        with localcontext() as context:
            context.prec = 50
            pnls = [Decimal(row[0]) for row in rows]
            net = sum(pnls, Decimal(0))
            losses = sum((-pnl for pnl in pnls if pnl < 0), Decimal(0))
        latched = db.execute("SELECT 1 FROM breakers WHERE trading_day=?", (day.isoformat(),)).fetchone() is not None
        return DailySummary(day, net, losses, latched, len(rows))

    def record_closed_trade(self, trade: ClosedTrade, *, recorded_at: datetime) -> bool:
        """Atomically record close + P&L + breaker + audit events.

        Returns False for an identical replay. Conflicting duplicates raise;
        corrections require a future explicit audited reconciliation workflow.
        There is deliberately no reset or deletion API for breaker latches.
        """
        payload = trade.canonical()
        stamp = _timestamp(recorded_at)
        if recorded_at < trade.closed_at:
            raise LedgerError("Cannot record a future close")
        # Normalize equivalent decimal encodings for idempotent replay.
        with localcontext() as context:
            context.prec = 50
            for key in ("entry_debit", "exit_credit", "fees"):
                payload[key] = format(Decimal(payload[key]).normalize(), "f")
            pnl = trade.exit_credit - trade.entry_debit - trade.fees
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self._transaction() as db:
            existing = db.execute("SELECT payload FROM closed_trades WHERE close_id=? OR position_id=?",
                                  (trade.close_id, trade.position_id)).fetchall()
            if existing:
                if len(existing) == 1 and existing[0][0] == encoded:
                    return False
                raise LedgerError("Conflicting closed trade; reconciliation required")
            db.execute("INSERT INTO closed_trades VALUES (?, ?, ?, ?, ?)",
                       (trade.close_id, trade.position_id, trade.trading_day.isoformat(), encoded, str(pnl)))
            event = {**payload, "realized_pnl": str(pnl)}
            db.execute("INSERT INTO ledger_events(timestamp,event,payload) VALUES (?, 'position_closed', ?)",
                       (stamp, json.dumps(event, sort_keys=True)))
            summary = self._summary(db, trade.trading_day)
            if summary.realized_loss >= Decimal("40") and not summary.breaker_tripped:
                db.execute("INSERT INTO breakers VALUES (?, ?)", (trade.trading_day.isoformat(), stamp))
                db.execute("INSERT INTO ledger_events(timestamp,event,payload) VALUES (?, 'circuit_breaker_triggered', ?)",
                           (stamp, json.dumps({"trading_day": trade.trading_day.isoformat(),
                                               "realized_loss": str(summary.realized_loss)})))
        return True

    def daily_summary(self, trading_day: date) -> DailySummary:
        if type(trading_day) is not date:
            raise LedgerError("Exchange trading day required")
        with self._transaction() as db:
            return self._summary(db, trading_day)

    def events(self) -> list[dict]:
        with self._transaction() as db:
            return [{"sequence": seq, "timestamp": stamp, "event": event, "payload": json.loads(payload)}
                    for seq, stamp, event, payload in db.execute(
                        "SELECT sequence,timestamp,event,payload FROM ledger_events ORDER BY sequence")]
