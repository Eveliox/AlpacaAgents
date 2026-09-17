"""Offline FIFO accounting for normalized standard LONG-option executions.

Not a broker importer or authorization source. One database per account. Unknown
history, spreads, shorts, exercises, assignments and corrections require a future
reconciler; never guess them into ordinary long-option fills.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, localcontext
import json
from pathlib import Path
import re
import sqlite3

from .ledger import LedgerError, _identifier, _money, _timestamp

SCALE = 1_000_000  # Integer microdollars; never use SQL floating-point arithmetic.


def _units(value: Decimal) -> int:
    with localcontext() as context:
        context.prec = 50
        return int(_money(value) * SCALE)


def _dollars(value: int) -> Decimal:
    with localcontext() as context:
        context.prec = 50
        return Decimal(value) / SCALE


@dataclass(frozen=True)
class OptionFill:
    execution_id: str
    position_id: str  # Stable, unique lifecycle ID, NOT just the option symbol.
    contract: str
    side: str  # buy_to_open or sell_to_close
    quantity: int
    premium: Decimal  # Total dollars for this execution, not per-share price.
    fees: Decimal  # Actual fees for this execution; unknown fees are unsupported.
    occurred_at: datetime
    trading_day: date  # Trusted exchange session date, not ingestion date.
    strategy: str = "long_option"
    multiplier: int = 100

    def canonical(self) -> dict:
        _identifier(self.execution_id)
        _identifier(self.position_id)
        if self.strategy != "long_option" or type(self.multiplier) is not int or self.multiplier != 100:
            raise LedgerError("Only standard single-leg long-option fills supported")
        if not isinstance(self.contract, str) or not re.fullmatch(r"[A-Z]{1,6}[0-9]{6}[CP][0-9]{8}", self.contract):
            raise LedgerError("Standard OCC contract symbol required")
        try:
            date(2000 + int(self.contract[-15:-13]), int(self.contract[-13:-11]), int(self.contract[-11:-9]))
        except ValueError:
            raise LedgerError("Invalid contract expiration") from None
        if int(self.contract[-8:]) == 0:
            raise LedgerError("Positive strike required")
        if self.side not in ("buy_to_open", "sell_to_close"):
            raise LedgerError("Unsupported fill side")
        if type(self.quantity) is not int or not 1 <= self.quantity <= 10000:
            raise LedgerError("Positive integral execution quantity required")
        if type(self.trading_day) is not date:
            raise LedgerError("Exchange session date required")
        premium, fees = _units(self.premium), _units(self.fees)
        if self.side == "buy_to_open" and premium == 0:
            raise LedgerError("Positive entry premium required")
        return {"execution_id": self.execution_id, "position_id": self.position_id,
                "contract": self.contract, "side": self.side, "quantity": self.quantity,
                "premium": premium, "fees": fees, "occurred_at": _timestamp(self.occurred_at),
                "trading_day": self.trading_day.isoformat()}


class FillLedger:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS option_fills (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, execution_id TEXT UNIQUE NOT NULL,
                position_id TEXT NOT NULL, payload TEXT NOT NULL,
                trading_day TEXT NOT NULL, pnl_units INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS option_lots (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, position_id TEXT NOT NULL,
                quantity INTEGER NOT NULL, basis_units INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS fill_breakers (
                trading_day TEXT PRIMARY KEY, tripped_at TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS option_fees (
                fee_id TEXT PRIMARY KEY, trading_day TEXT NOT NULL, amount_units INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS fill_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
                event TEXT NOT NULL, payload TEXT NOT NULL)""")

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
    def _totals(db, day: str):
        """(net pnl, loss counter). Separately-billed fees count fully toward the loss counter."""
        pnls = [r[0] for r in db.execute("SELECT pnl_units FROM option_fills WHERE trading_day=?", (day,))]
        fees = sum(r[0] for r in db.execute("SELECT amount_units FROM option_fees WHERE trading_day=?", (day,)))
        return sum(pnls) - fees, sum(-p for p in pnls if p < 0) + fees

    def _latch_if_needed(self, db, day: str, stamp: str) -> None:
        _, loss = self._totals(db, day)
        if loss >= 40 * SCALE:
            changed = db.execute("INSERT OR IGNORE INTO fill_breakers VALUES (?,?)", (day, stamp))
            if changed.rowcount:
                db.execute("INSERT INTO fill_events(timestamp,event,payload) VALUES (?, 'circuit_breaker_triggered', ?)",
                           (stamp, json.dumps({"trading_day": day, "realized_loss": str(_dollars(loss))})))

    def record_fee(self, fee_id: str, *, trading_day: date, amount: Decimal, recorded_at: datetime) -> bool:
        """Book a separately-billed fee (e.g. options regulatory fees) against a session."""
        _identifier(fee_id)
        if type(trading_day) is not date:
            raise LedgerError("Exchange session date required")
        units = _units(amount)
        stamp = _timestamp(recorded_at)
        with self._transaction() as db:
            existing = db.execute("SELECT trading_day,amount_units FROM option_fees WHERE fee_id=?", (fee_id,)).fetchone()
            if existing:
                if existing == (trading_day.isoformat(), units):
                    return False
                raise LedgerError("Conflicting fee replay; reconciliation required")
            db.execute("INSERT INTO option_fees VALUES (?,?,?)", (fee_id, trading_day.isoformat(), units))
            db.execute("INSERT INTO fill_events(timestamp,event,payload) VALUES (?, 'fee_accounted', ?)",
                       (stamp, json.dumps({"fee_id": fee_id, "trading_day": trading_day.isoformat(), "amount": str(_dollars(units))})))
            self._latch_if_needed(db, trading_day.isoformat(), stamp)
        return True

    def record(self, fill: OptionFill, *, recorded_at: datetime) -> bool:
        data = fill.canonical()
        stamp = _timestamp(recorded_at)
        if recorded_at < fill.occurred_at:
            raise LedgerError("Cannot record a future execution")
        encoded = json.dumps(data, sort_keys=True, separators=(",", ":"))
        with self._transaction() as db:
            existing = db.execute("SELECT payload FROM option_fills WHERE execution_id=?", (fill.execution_id,)).fetchone()
            if existing:
                if existing[0] == encoded:
                    return False
                raise LedgerError("Conflicting execution replay; reconciliation required")
            previous = db.execute("SELECT payload FROM option_fills WHERE position_id=? ORDER BY seq DESC LIMIT 1",
                                  (fill.position_id,)).fetchone()
            lots = db.execute("SELECT seq,quantity,basis_units FROM option_lots WHERE position_id=? AND quantity>0 ORDER BY seq",
                              (fill.position_id,)).fetchall()
            available = sum(qty for _, qty, _ in lots)
            if previous:
                last = json.loads(previous[0])
                if last["contract"] != fill.contract:
                    raise LedgerError("Position lifecycle contract mismatch")
                # Equal timestamps are allowed (partial fills of one order); the
                # importer must supply a deterministic (time, id) ordering.
                if fill.occurred_at < datetime.fromisoformat(last["occurred_at"]) or data["trading_day"] < last["trading_day"]:
                    raise LedgerError("Out-of-order execution; verified ordering required")
                if available == 0:
                    raise LedgerError("Closed lifecycle cannot be reused")
            pnl = 0
            if fill.side == "buy_to_open":
                db.execute("INSERT INTO option_lots(position_id,quantity,basis_units) VALUES (?,?,?)",
                           (fill.position_id, fill.quantity, data["premium"] + data["fees"]))
            else:
                if available < fill.quantity:
                    raise LedgerError("Unmatched close or oversell; complete opening history required")
                remaining, basis = fill.quantity, 0
                for seq, qty, cost in lots:
                    take = min(remaining, qty)
                    # Round basis upward by <1 microdollar for partial allocation;
                    # the remainder stays on the lot, so final totals stay exact.
                    allocated = (cost * take + qty - 1) // qty
                    basis += allocated
                    db.execute("UPDATE option_lots SET quantity=?,basis_units=? WHERE seq=?",
                               (qty - take, cost - allocated, seq))
                    remaining -= take
                    if remaining == 0:
                        break
                pnl = data["premium"] - data["fees"] - basis
            db.execute("INSERT INTO option_fills(execution_id,position_id,payload,trading_day,pnl_units) VALUES (?,?,?,?,?)",
                       (fill.execution_id, fill.position_id, encoded, data["trading_day"], pnl))
            event = {**data, "realized_pnl": str(_dollars(pnl))}
            db.execute("INSERT INTO fill_events(timestamp,event,payload) VALUES (?, 'fill_accounted', ?)",
                       (stamp, json.dumps(event, sort_keys=True)))
            self._latch_if_needed(db, data["trading_day"], stamp)
        return True

    def daily_summary(self, day: date) -> dict:
        if type(day) is not date:
            raise LedgerError("Exchange session date required")
        with self._transaction() as db:
            net, loss = self._totals(db, day.isoformat())
            latch = db.execute("SELECT 1 FROM fill_breakers WHERE trading_day=?", (day.isoformat(),)).fetchone()
            return {"trading_day": day, "realized_pnl": _dollars(net),
                    "realized_loss": _dollars(loss), "breaker_tripped": latch is not None,
                    "reconciled": False}  # Imported records do NOT prove complete broker history.

    def events(self, limit: int = 100) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise LedgerError("Event limit must be between 1 and 1000")
        with self._transaction() as db:
            rows = db.execute("SELECT seq,timestamp,event,payload FROM fill_events ORDER BY seq DESC LIMIT ?", (limit,))
            return [{"sequence": seq, "timestamp": stamp, "event": event, "payload": json.loads(payload)}
                    for seq, stamp, event, payload in rows]

    def inventory(self) -> list[dict]:
        """Remaining lots aggregated per lifecycle, with the lifecycle's contract."""
        with self._transaction() as db:
            rows = db.execute("""SELECT l.position_id, SUM(l.quantity), SUM(l.basis_units),
                                 (SELECT json_extract(payload,'$.contract') FROM option_fills f
                                  WHERE f.position_id = l.position_id ORDER BY seq LIMIT 1)
                                 FROM option_lots l WHERE l.quantity > 0 GROUP BY l.position_id ORDER BY MIN(l.seq)""")
            return [{"position_id": pid, "quantity": qty, "remaining_basis": _dollars(basis), "contract": contract}
                    for pid, qty, basis, contract in rows]

    def sell_proceeds_since(self, day: date) -> Decimal:
        """Gross sell premium booked on sessions >= day (for conservative unsettled-cash bounds)."""
        if type(day) is not date:
            raise LedgerError("Exchange session date required")
        with self._transaction() as db:
            rows = db.execute("SELECT payload FROM option_fills WHERE trading_day>=?", (day.isoformat(),)).fetchall()
        total = 0
        for (payload,) in rows:
            data = json.loads(payload)
            if data["side"] == "sell_to_close":
                total += data["premium"]
        return _dollars(total)

    def day_trades_since(self, day: date) -> int:
        """Lifecycles with a buy and a sell on the same session, that session >= day (PDT counting)."""
        if type(day) is not date:
            raise LedgerError("Exchange session date required")
        with self._transaction() as db:
            rows = db.execute("""SELECT COUNT(*) FROM (
                SELECT position_id, trading_day FROM option_fills WHERE trading_day >= ?
                GROUP BY position_id, trading_day
                HAVING SUM(json_extract(payload,'$.side')='buy_to_open') > 0
                   AND SUM(json_extract(payload,'$.side')='sell_to_close') > 0)""", (day.isoformat(),)).fetchone()
        return int(rows[0])

    def recorded_execution(self, execution_id: str) -> dict | None:
        """Canonical payload already booked under this execution id, or None."""
        _identifier(execution_id)
        with self._transaction() as db:
            row = db.execute("SELECT payload FROM option_fills WHERE execution_id=?", (execution_id,)).fetchone()
        return None if row is None else json.loads(row[0])

    def open_lifecycle(self, contract: str) -> str | None:
        """Lifecycle id currently holding this contract, if any (long-only)."""
        for row in self.inventory():
            if row["contract"] == contract:
                return row["position_id"]
        return None
