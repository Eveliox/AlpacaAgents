from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from alpaca_agents.executor.orders import AUTH_TTL, OrderJournal
from alpaca_agents.rules import RiskState

NOW = datetime(2026, 9, 15, 14, tzinfo=timezone.utc)
DAY = date(2026, 9, 15)
ROOT = Path(__file__).resolve().parents[1]


class OrderJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "orders.sqlite3"
        self.control = Path(self.temp.name) / "control"
        self.control.write_text("ARMED_PAPER")
        self.journal = OrderJournal(self.path, account_id="paper-account", control_file=self.control)
        self.idea = json.loads((ROOT / "examples/long_call.json").read_text())
        # Synthetic evidence ONLY. The real reconciler deliberately cannot yet
        # produce reconciled=True. Nothing in this test suite touches a broker.
        self.state = RiskState(DAY, NOW, 0, 0, Decimal(0), Decimal(2000), (), reconciled=True)

    def reserve(self, key="idea-1", idea=None, *, now=NOW, state=None):
        return self.journal.reserve(key, self.idea if idea is None else idea,
                                    state_provider=lambda: self.state if state is None else state,
                                    now=now, trading_day=DAY)

    def claim(self, aid, *, now=NOW, state=None, day=DAY):
        return self.journal.claim(aid, state_provider=lambda: self.state if state is None else state,
                                  now=now, trading_day=day)

    def other(self, symbol="SPY"):
        idea = deepcopy(self.idea)
        idea["symbol"] = symbol
        for leg in idea["legs"]:
            leg["symbol"] = symbol
        return idea

    def test_reserve_then_claim_prepares_correct_limit_order_once(self):
        reserved = self.reserve()
        self.assertTrue(reserved["approved"])
        claimed = self.claim(reserved["authorization_id"])
        self.assertTrue(claimed["approved"])
        self.assertFalse(claimed["submission_enabled"])
        body = claimed["prepared_order"]
        self.assertEqual(body["symbol"], "IWM261016C00205000")
        self.assertEqual(body["limit_price"], "0.90")
        self.assertEqual((body["side"], body["type"], body["time_in_force"], body["qty"], body["position_intent"]),
                         ("buy", "limit", "day", "1", "buy_to_open"))
        self.assertEqual(body["client_order_id"], "paper-" + reserved["authorization_id"])
        self.assertFalse(self.claim(reserved["authorization_id"])["approved"])
        self.assertFalse(self.reserve()["approved"])

    def test_claim_loads_immutable_snapshot_not_mutated_scanner_idea(self):
        reserved = self.reserve()
        self.idea["limit_debit"] = "10"
        result = self.claim(reserved["authorization_id"])
        self.assertEqual(result["prepared_order"]["limit_price"], "0.90")
        self.assertEqual(self.journal.events()[-1]["payload"]["idea"]["limit_debit"], "0.90")

    def test_bad_inputs_and_forged_approval_are_audited(self):
        for i, idea in enumerate(({}, {"approved": True, "idea": self.idea},
                                   {**self.idea, "stop": None}, {**self.idea, "score": float("nan")})):
            self.assertFalse(self.reserve(str(i), idea)["approved"])
        self.assertEqual(len(self.journal.events()), 4)
        self.assertTrue(all(e["event"] == "idea_rejected" for e in self.journal.events()))
        self.assertFalse(self.claim("forged-authorization")["approved"])

    def test_account_binding_survives_restart(self):
        reserved = self.reserve()
        self.journal = OrderJournal(self.path, account_id="paper-account", control_file=self.control)
        self.assertTrue(self.claim(reserved["authorization_id"])["approved"])
        with self.assertRaises(ValueError):
            OrderJournal(self.path, account_id="another-account", control_file=self.control)

    def test_cash_reservations_prevent_overbooking(self):
        state = replace(self.state, settled_cash=Decimal(150))
        self.assertTrue(self.reserve(state=state)["approved"])
        second = self.reserve("idea-2", self.other(), state=state)
        self.assertFalse(second["approved"])
        self.assertIn("INSUFFICIENT_CASH", second["reason"])

    def test_position_limit_with_concurrent_reservations(self):
        state = replace(self.state, open_positions=1)
        ideas = [self.idea, self.other()]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda i: self.reserve(f"idea-{i}", ideas[i], state=state), range(2)))
        self.assertEqual(sum(r["approved"] for r in results), 1)
        self.assertIn("POSITION_LIMIT", next(r["reason"] for r in results if not r["approved"]))

    def test_same_symbol_and_duplicate_key_blocked(self):
        self.assertTrue(self.reserve()["approved"])
        self.assertIn("SYMBOL_RESERVED", self.reserve("new-id")["reason"])
        self.assertIn("DUPLICATE_DECISION", self.reserve()["reason"])
        self.assertFalse(self.reserve("rejected", {**self.idea, "quantity": 5})["approved"])
        self.assertIn("DUPLICATE_DECISION", self.reserve("rejected", self.other())["reason"])

    def test_kill_switch_rechecked_on_claim_and_releases_unsent_reservation(self):
        reserved = self.reserve()
        self.control.write_text("STOP")
        result = self.claim(reserved["authorization_id"])
        self.assertIn("KILL_SWITCH", result["reason"])
        self.control.write_text("ARMED_PAPER")
        self.assertFalse(self.claim(reserved["authorization_id"])["approved"])
        self.assertTrue(self.reserve("new-id")["approved"])
        self.control.unlink()
        self.assertIn("KILL_SWITCH", self.reserve("no-control")["reason"])

    def test_switch_changed_during_state_read_still_blocks(self):
        def provider():
            self.control.write_text("STOP")
            return self.state
        result = self.journal.reserve("race", self.idea, state_provider=provider, now=NOW, trading_day=DAY)
        self.assertIn("KILL_SWITCH", result["reason"])

    def test_risk_state_rechecked_at_claim(self):
        for i, state in enumerate((replace(self.state, breaker_tripped=True),
                                    replace(self.state, reconciled=False),
                                    replace(self.state, settled_cash=Decimal(0)))):
            reserved = self.reserve(f"idea-{i}")
            self.assertTrue(reserved["approved"])
            self.assertFalse(self.claim(reserved["authorization_id"], state=state)["approved"])

    def test_expiry_boundary_and_day_change_reject(self):
        reserved = self.reserve()
        self.assertFalse(self.claim(reserved["authorization_id"], now=NOW + AUTH_TTL)["approved"])
        fresh = replace(self.state, observed_at=NOW + AUTH_TTL)
        reserved2 = self.reserve("idea-2", now=NOW + AUTH_TTL, state=fresh)
        self.assertTrue(reserved2["approved"])
        result = self.claim(reserved2["authorization_id"], now=NOW + AUTH_TTL, state=fresh, day=DAY + timedelta(days=1))
        self.assertIn("TIME_MISMATCH", result["reason"])

    def test_claimed_reservation_never_auto_expires(self):
        state = replace(self.state, open_positions=1)
        reserved = self.reserve(state=state)
        self.assertTrue(self.claim(reserved["authorization_id"], state=state)["approved"])
        later = NOW + timedelta(minutes=5)
        result = self.reserve("idea-2", self.other(), now=later, state=replace(state, observed_at=later))
        self.assertIn("POSITION_LIMIT", result["reason"])

    def test_rate_allowance_persists_after_expiry(self):
        for i in range(5):
            now = NOW + timedelta(seconds=31 * i)
            self.assertTrue(self.reserve(str(i), now=now, state=replace(self.state, observed_at=now))["approved"])
        later = NOW + timedelta(seconds=31 * 5)
        self.assertIn("RATE_LIMIT", self.reserve("sixth", now=later, state=replace(self.state, observed_at=later))["reason"])
        later = NOW + timedelta(hours=1)
        self.assertTrue(self.reserve("after-hour", now=later, state=replace(self.state, observed_at=later))["approved"])

    def test_claim_excludes_own_reservation_and_rate_allowance(self):
        for i in range(4):
            now = NOW + timedelta(seconds=31 * i)
            self.reserve(str(i), now=now, state=replace(self.state, observed_at=now))
        now = NOW + timedelta(seconds=31 * 4)
        state = replace(self.state, observed_at=now, settled_cash=Decimal(91), open_positions=1)
        fifth = self.reserve("fifth", now=now, state=state)
        self.assertTrue(fifth["approved"])
        self.assertTrue(self.claim(fifth["authorization_id"], now=now, state=state)["approved"])

    def test_concurrent_claims_consume_once(self):
        reserved = self.reserve()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.claim(reserved["authorization_id"]), range(2)))
        self.assertEqual(sum(r["approved"] for r in results), 1)

    def test_audit_failure_rolls_back_claim(self):
        reserved = self.reserve()
        db = sqlite3.connect(self.path)
        try:
            db.execute("""CREATE TRIGGER fail_audit BEFORE INSERT ON order_events
                          WHEN NEW.event='authorization_claimed'
                          BEGIN SELECT RAISE(ABORT, 'simulated audit failure'); END""")
            db.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                self.claim(reserved["authorization_id"])
            status = db.execute("SELECT status FROM order_intents WHERE authorization_id=?", (reserved["authorization_id"],)).fetchone()[0]
            self.assertEqual(status, "reserved")
            db.execute("DROP TRIGGER fail_audit")
            db.commit()
        finally:
            db.close()
        self.assertTrue(self.claim(reserved["authorization_id"])["approved"])

    def test_audit_failure_rolls_back_reservation(self):
        db = sqlite3.connect(self.path)
        try:
            db.execute("""CREATE TRIGGER fail_reserve BEFORE INSERT ON order_events
                          WHEN NEW.event='idea_approved'
                          BEGIN SELECT RAISE(ABORT, 'simulated audit failure'); END""")
            db.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                self.reserve()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0], 0)
            db.execute("DROP TRIGGER fail_reserve")
            db.commit()
        finally:
            db.close()
        self.assertTrue(self.reserve()["approved"])

    def test_state_can_age_out_before_authorization_expires(self):
        old_state = replace(self.state, observed_at=NOW - timedelta(seconds=50))
        reserved = self.reserve(state=old_state)
        self.assertTrue(reserved["approved"])
        result = self.claim(reserved["authorization_id"], now=NOW + timedelta(seconds=20), state=old_state)
        self.assertIn("STALE_STATE", result["reason"])

    def test_actual_unverified_state_cannot_reserve(self):
        result = self.reserve(state=replace(self.state, settled_cash=Decimal(0), reconciled=False))
        self.assertFalse(result["approved"])
        self.assertNotIn("authorization_id", result)
        self.assertIn("STATE_UNAVAILABLE", result["reason"])

    def test_provider_errors_are_sanitized_and_audited(self):
        def provider(): raise RuntimeError("fake-secret")
        result = self.journal.reserve("provider-error", self.idea, state_provider=provider, now=NOW, trading_day=DAY)
        self.assertIn("STATE_PROVIDER_ERROR", result["reason"])
        self.assertNotIn("fake-secret", json.dumps(self.journal.events()))

    def test_put_translation_and_spreads_remain_unsupported(self):
        put = deepcopy(self.idea)
        put.update(strategy="long_put", direction="short", stop=205, target=190)
        put["legs"][0]["right"] = "put"
        reserved = self.reserve(idea=put)
        self.assertEqual(self.claim(reserved["authorization_id"])["prepared_order"]["symbol"], "IWM261016P00205000")
        spread = self.other()
        spread["strategy"] = "call_debit_spread"
        spread["legs"].append({**spread["legs"][0], "strike": 210, "side": "sell"})
        result = self.reserve("spread", spread)
        self.assertIn("UNSUPPORTED_EXECUTION_STRUCTURE", result["reason"])


if __name__ == "__main__":
    unittest.main()
