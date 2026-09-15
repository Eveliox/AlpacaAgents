"""OFFLINE paper order-intent journal. No HTTP client, credentials or submission.

An executor-owned controller supplies baseline RiskState via state_provider;
scanner input is ONLY the unvalidated idea. Baseline cash/pending/attempt counts
exclude this journal's local reservations. DB access must be executor-only.

reserved -> claimed is single-use and commits BEFORE releasing a prepared body.
A claimed reservation never auto-expires: a future process crash/send timeout
could mean a broker fill. Reconciliation, not retry, must resolve that state.
"""
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from alpaca_agents.gateway import trading_disabled
from alpaca_agents.rules import RiskState, evaluate
from .ledger import _identifier

AUTH_TTL = timedelta(seconds=30)


def _stamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Aware timestamp required")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _json_idea(idea):
    try:
        return json.loads(json.dumps(idea, allow_nan=False))
    except (TypeError, ValueError, OverflowError):
        return None


class OrderJournal:
    def __init__(self, path: Path, *, account_id: str, control_file: Path):
        _identifier(account_id)
        self.path, self.control_file = path, control_file
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS order_scope (singleton INTEGER PRIMARY KEY CHECK(singleton=1), account_id TEXT NOT NULL)")
            previous = db.execute("SELECT account_id FROM order_scope WHERE singleton=1").fetchone()
            if previous and previous[0] != account_id:
                raise ValueError("Order journal belongs to another paper account")
            db.execute("INSERT OR IGNORE INTO order_scope VALUES (1,?)", (account_id,))
            db.execute("""CREATE TABLE IF NOT EXISTS order_intents (
                authorization_id TEXT PRIMARY KEY, decision_key TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL, expires_at TEXT NOT NULL, trading_day TEXT NOT NULL,
                status TEXT NOT NULL, authorized INTEGER NOT NULL,
                symbol TEXT, cost TEXT NOT NULL, idea TEXT NOT NULL,
                client_order_id TEXT UNIQUE NOT NULL, reason TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS order_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
                event TEXT NOT NULL, payload TEXT NOT NULL)""")

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _event(db, stamp, event, payload):
        db.execute("INSERT INTO order_events(timestamp,event,payload) VALUES (?,?,?)",
                   (stamp, event, json.dumps(payload, allow_nan=False, sort_keys=True)))

    def _expire(self, db, stamp):
        rows = db.execute("SELECT authorization_id FROM order_intents WHERE status='reserved' AND expires_at<=?", (stamp,)).fetchall()
        for row in rows:
            db.execute("UPDATE order_intents SET status='expired',reason='AUTHORIZATION_EXPIRED' WHERE authorization_id=?", (row[0],))
            self._event(db, stamp, "authorization_expired", {"authorization_id": row[0]})

    def _validate(self, db, idea, state_provider, *, now, day, excluding=None):
        # Re-read the independently controlled switch on BOTH reserve and claim.
        disabled = trading_disabled(self.control_file)
        if disabled:
            return {"approved": False, "reason": "KILL_SWITCH: trading disabled", "idea": idea}
        try:
            baseline = state_provider()  # Trusted controller only, inside DB lock.
            if not isinstance(baseline, RiskState):
                raise ValueError
            rows = db.execute("SELECT authorization_id,symbol,cost,status,created_at,authorized FROM order_intents").fetchall()
            others = [r for r in rows if r["authorization_id"] != excluding]
            active = [r for r in others if r["status"] in ("reserved", "claimed")]
            with localcontext() as ctx:
                ctx.prec = 50
                cash = Decimal(str(baseline.settled_cash)) - sum((Decimal(r["cost"]) for r in active), Decimal(0))
            attempts = tuple(datetime.fromisoformat(r["created_at"]) for r in others if r["authorized"])
            state = replace(baseline, pending_entries=baseline.pending_entries + len(active),
                            settled_cash=max(Decimal(0), cash), order_attempts=baseline.order_attempts + attempts)
        except Exception:
            return {"approved": False, "reason": "STATE_PROVIDER_ERROR: trusted baseline unavailable", "idea": idea}
        result = evaluate(idea, state, now=now, trading_day=day, kill_switch=False)
        # The paper intent translator supports long single-leg orders only.
        # Additional restrictions never turn a rules rejection into approval.
        if result["approved"] and idea["strategy"] not in ("long_call", "long_put"):
            result = {"approved": False, "reason": "UNSUPPORTED_EXECUTION_STRUCTURE: single-leg longs only", "idea": idea}
        if result["approved"] and any(r["symbol"] == idea["symbol"] for r in active):
            result = {"approved": False, "reason": "SYMBOL_RESERVED: one pending intent per underlying", "idea": idea}
        # Catch a switch changed while the trusted state provider was running.
        if trading_disabled(self.control_file):
            result = {"approved": False, "reason": "KILL_SWITCH: trading disabled", "idea": idea}
        return result

    def reserve(self, decision_key: str, idea, *, state_provider, now: datetime, trading_day: date) -> dict:
        """Validate and atomically reserve cash, one slot and one rate allowance.

        decision_key is a stable scanner/controller correlation ID, NOT authority.
        Every key is single-use, including rejected keys. Retry requires a new
        decision and full validation. All decisions are audited transactionally.
        """
        _identifier(decision_key)
        stamp = _stamp(now)
        if type(trading_day) is not date:
            raise ValueError("Trading day required")
        idea = _json_idea(idea)
        with self._transaction() as db:
            self._expire(db, stamp)
            if db.execute("SELECT 1 FROM order_intents WHERE decision_key=?", (decision_key,)).fetchone():
                result = {"approved": False, "reason": "DUPLICATE_DECISION: key already evaluated", "idea": idea}
            else:
                result = self._validate(db, idea, state_provider, now=now, day=trading_day)
                authorization_id = uuid4().hex
                cost = Decimal(0)
                if result["approved"]:
                    with localcontext() as ctx:
                        ctx.prec = 50
                        cost = Decimal(str(idea["limit_debit"])) * 100 + Decimal(str(idea["estimated_fees"]))
                db.execute("INSERT INTO order_intents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (authorization_id, decision_key, stamp, _stamp(now + AUTH_TTL), trading_day.isoformat(),
                            "reserved" if result["approved"] else "rejected", int(result["approved"]),
                            idea.get("symbol") if result["approved"] else None, str(cost),
                            json.dumps(idea, allow_nan=False, sort_keys=True), "paper-" + authorization_id, result["reason"]))
                if result["approved"]:
                    result["authorization_id"] = authorization_id
                    result["expires_at"] = _stamp(now + AUTH_TTL)
            self._event(db, stamp, "idea_approved" if result["approved"] else "idea_rejected",
                        {"decision_key": decision_key, **result})
        return result  # COMMIT has succeeded before an approval leaves this method.

    @staticmethod
    def _body(row) -> dict:
        idea = json.loads(row["idea"])
        leg = idea["legs"][0]
        expiry = date.fromisoformat(leg["expiration"])
        with localcontext() as ctx:
            ctx.prec = 50
            strike = int(Decimal(str(leg["strike"])) * 1000)
            limit = format(Decimal(str(idea["limit_debit"])), ".2f")
        contract = f"{idea['symbol']}{expiry:%y%m%d}{'C' if leg['right'] == 'call' else 'P'}{strike:08d}"
        return {"symbol": contract, "qty": "1", "side": "buy", "type": "limit",
                "time_in_force": "day", "limit_price": limit, "position_intent": "buy_to_open",
                "client_order_id": row["client_order_id"]}

    def claim(self, authorization_id: str, *, state_provider, now: datetime, trading_day: date) -> dict:
        """Consume once, returning an OFFLINE prepared body, never submitting it.

        No caller-provided idea/body is accepted. Reload and revalidate the stored
        idea, excluding its own reservation but including every other one.
        Claimed reservations are unresolved until a future broker reconciler
        explicitly releases them. Expiry alone can never release a claimed slot.
        """
        _identifier(authorization_id)
        stamp = _stamp(now)
        if type(trading_day) is not date:
            raise ValueError("Trading day required")
        with self._transaction() as db:
            self._expire(db, stamp)
            row = db.execute("SELECT * FROM order_intents WHERE authorization_id=?", (authorization_id,)).fetchone()
            if row is None or row["status"] != "reserved":
                result = {"approved": False, "reason": "AUTHORIZATION_UNAVAILABLE: unknown, expired or already consumed", "idea": None}
            elif row["trading_day"] != trading_day.isoformat() or stamp < row["created_at"]:
                result = {"approved": False, "reason": "AUTHORIZATION_TIME_MISMATCH", "idea": json.loads(row["idea"])}
                db.execute("UPDATE order_intents SET status='revoked',reason=? WHERE authorization_id=?", (result["reason"], authorization_id))
            else:
                idea = json.loads(row["idea"])
                result = self._validate(db, idea, state_provider, now=now, day=trading_day, excluding=authorization_id)
                if result["approved"]:
                    result["prepared_order"] = self._body(row)
                    result["submission_enabled"] = False
                db.execute("UPDATE order_intents SET status=?,reason=? WHERE authorization_id=?",
                           ("claimed" if result["approved"] else "revoked", result["reason"], authorization_id))
            self._event(db, stamp, "authorization_claimed" if result["approved"] else "authorization_rejected",
                        {"authorization_id": authorization_id, **result})
        return result

    def live_intents(self) -> list[dict]:
        """Reserved and claimed intents (both still hold cash/slot reservations)."""
        with self._transaction() as db:
            rows = db.execute("""SELECT authorization_id, client_order_id, symbol, cost, status, created_at
                                 FROM order_intents WHERE status IN ('reserved','claimed') ORDER BY created_at""").fetchall()
        return [dict(r) for r in rows]

    TERMINAL = frozenset({"filled", "canceled", "expired", "rejected", "replaced", "done_for_day"})

    def resolve(self, authorization_id: str, *, broker_status: str, broker_order_id: str, now: datetime) -> bool:
        """claimed -> resolved:<status>, only from a broker-observed terminal status.

        This is the ONLY way a claimed reservation releases its slot. It must be
        driven by a broker order record matched on client_order_id, never by a
        timeout, a caller assertion, or the absence of a record.
        """
        _identifier(authorization_id)
        _identifier(broker_order_id)
        if broker_status not in self.TERMINAL:
            raise ValueError("Only terminal broker statuses can resolve an intent")
        stamp = _stamp(now)
        with self._transaction() as db:
            row = db.execute("SELECT status FROM order_intents WHERE authorization_id=?", (authorization_id,)).fetchone()
            if row is None or row["status"] != "claimed":
                return False
            db.execute("UPDATE order_intents SET status=?,reason=? WHERE authorization_id=?",
                       (f"resolved:{broker_status}", f"BROKER_STATUS: {broker_status}", authorization_id))
            self._event(db, stamp, "intent_resolved", {"authorization_id": authorization_id,
                                                      "broker_status": broker_status, "broker_order_id": broker_order_id})
        return True

    def events(self, limit=100) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Event limit must be 1..1000")
        with self._transaction() as db:
            rows = db.execute("SELECT sequence,timestamp,event,payload FROM order_events ORDER BY sequence DESC LIMIT ?", (limit,))
            return [{"sequence": r[0], "timestamp": r[1], "event": r[2], "payload": json.loads(r[3])} for r in rows]
