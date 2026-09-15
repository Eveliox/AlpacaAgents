import copy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from alpaca_agents.gateway import validate_and_log
from alpaca_agents.rules import RiskState, evaluate

NOW = datetime(2026, 9, 15, 14, tzinfo=timezone.utc)
DAY = date(2026, 9, 15)
ROOT = Path(__file__).resolve().parents[1]


class RulesTests(unittest.TestCase):
    def setUp(self):
        self.idea = json.loads((ROOT / "examples/long_call.json").read_text())
        self.state = RiskState(DAY, NOW, 0, 0, Decimal("0"), Decimal("2000"), (), reconciled=True)

    def check(self, code="APPROVED", idea=None, state=None, **kwargs):
        result = evaluate(self.idea if idea is None else idea,
                          self.state if state is None else state,
                          now=NOW, trading_day=DAY, kill_switch=kwargs.get("kill_switch", False))
        self.assertEqual(result["reason"].split(":")[0], code)
        self.assertEqual(result["approved"], code == "APPROVED")
        return result

    def test_valid_and_deterministic_without_mutation(self):
        original = copy.deepcopy(self.idea)
        self.assertEqual(self.check(), self.check())
        self.assertEqual(self.idea, original)

    def test_idea_rejections(self):
        cases = [
            ({"strategy": "naked_call"}, "UNDEFINED_RISK"),
            ({"stop": None}, "MISSING_STOP_TARGET"),
            ({"target": None}, "MISSING_STOP_TARGET"),
            ({"quantity": 2}, "SIZE_LIMIT"),
            ({"quantity": True}, "SIZE_LIMIT"),
            ({"holding_style": "day"}, "SWING_ONLY"),
            ({"action": "close"}, "UNSUPPORTED_ACTION"),
            ({"target": 202, "reward_risk": 99}, "REWARD_RISK"),
            ({"reward_risk": 0.9}, "REWARD_RISK"),
            ({"stop": 205}, "INVALID_LEVELS"),
            ({"limit_debit": "4.38", "est_contract_cost": 438}, "RISK_LIMIT"),
            ({"limit_debit": 1, "est_contract_cost": 100}, "RISK_LIMIT"),
            ({"est_contract_cost": 1}, "COST_MISMATCH"),
            ({"entry_trigger": "NaN"}, "INVALID_INPUT"),
            ({"target": float("inf")}, "INVALID_INPUT"),
            ({"estimated_fees": -1}, "INVALID_COST"),
            ({"legs": []}, "INVALID_LEGS"),
        ]
        for change, code in cases:
            with self.subTest(change=change):
                self.check(code, {**self.idea, **change})
        self.check("INVALID_IDEA", idea=[])
        self.check("INVALID_INPUT", idea={k: v for k, v in self.idea.items() if k != "limit_debit"})

    def test_state_rejections(self):
        for change, code in [
            ({"open_positions": 2}, "POSITION_LIMIT"),
            ({"open_positions": 1, "pending_entries": 1}, "POSITION_LIMIT"),
            ({"daily_realized_loss": Decimal("40")}, "CIRCUIT_BREAKER"),
            ({"breaker_tripped": True}, "CIRCUIT_BREAKER"),
            ({"settled_cash": Decimal("90")}, "INSUFFICIENT_CASH"),
            ({"reconciled": False}, "STATE_UNAVAILABLE"),
            ({"observed_at": NOW - timedelta(seconds=61)}, "STALE_STATE"),
            ({"observed_at": NOW + timedelta(seconds=1)}, "STALE_STATE"),
            ({"trading_day": DAY - timedelta(days=1)}, "STALE_STATE"),
            ({"open_positions": -1}, "INVALID_STATE"),
            ({"daily_realized_loss": Decimal("NaN")}, "INVALID_INPUT"),
            ({"order_attempts": (NOW + timedelta(seconds=1),)}, "INVALID_STATE"),
            ({"order_attempts": (NOW,) * 5}, "RATE_LIMIT"),
        ]:
            with self.subTest(change=change):
                self.check(code, state=replace(self.state, **change))
        self.check(state=replace(self.state, order_attempts=(NOW - timedelta(hours=1),) * 5))
        self.check(state=replace(self.state, daily_realized_loss=Decimal("39.99")))

    def test_risk_boundary(self):
        self.check(idea={**self.idea, "limit_debit": ".99", "est_contract_cost": 99})
        self.check(idea={**self.idea, "limit_debit": 1, "est_contract_cost": 100, "estimated_fees": 0})

    def test_kill_switch_has_priority(self):
        self.check("KILL_SWITCH", idea={}, kill_switch=True)

    def test_contract_checks(self):
        for change, code in [
            ({"side": "sell"}, "INVALID_LEGS"),
            ({"expiration": DAY.isoformat()}, "INVALID_EXPIRATION"),
            ({"multiplier": 10}, "INVALID_LEGS"),
            ({"strike": 1.0001}, "INVALID_CONTRACT"),
            ({"expiration": "not-a-date"}, "INVALID_INPUT"),
        ]:
            idea = copy.deepcopy(self.idea)
            idea["legs"][0].update(change)
            self.check(code, idea)

    def test_long_put(self):
        self.idea.update(strategy="long_put", direction="short", stop=205, target=190)
        self.idea["legs"][0]["right"] = "put"
        self.check()

    def test_spreads(self):
        for right in ("call", "put"):
            idea = copy.deepcopy(self.idea)
            idea["strategy"] = f"{right}_debit_spread"
            idea["legs"][0]["right"] = right
            if right == "put":
                idea.update(direction="short", stop=205, target=190)
            idea["legs"].append({**idea["legs"][0], "side": "sell", "strike": 210 if right == "call" else 200})
            self.check(idea=idea)
            bad = copy.deepcopy(idea)
            bad["legs"][1]["expiration"] = "2026-11-20"
            self.check("INVALID_SPREAD", bad)
            bad = copy.deepcopy(idea)
            bad["legs"][1]["strike"] = 205
            self.check("INVALID_SPREAD", bad)
            bad = copy.deepcopy(idea)
            bad["legs"][1]["ratio"] = 2
            self.check("INVALID_LEGS", bad)

    def test_file_switch_and_audit(self):
        with tempfile.TemporaryDirectory() as temp:
            control, audit = Path(temp) / "control", Path(temp) / "audit.jsonl"
            def run(idea=None):
                return validate_and_log(self.idea if idea is None else idea, self.state, now=NOW,
                                        trading_day=DAY, control_file=control, audit_file=audit)
            self.assertFalse(run()["approved"])
            control.write_text("ARMED_PAPER")
            self.assertTrue(run()["approved"])
            self.assertFalse(run({**self.idea, "score": float("nan")})["approved"])
            control.write_text("STOP")
            self.assertFalse(run()["approved"])
            events = [json.loads(line) for line in audit.read_text().splitlines()]
            self.assertEqual(len(events), 8)
            self.assertEqual(events[3]["event"], "idea_approved")
            self.assertEqual(events[-1]["event"], "idea_rejected")
            # Audit errors must propagate, never return an approval.
            control.write_text("ARMED_PAPER")
            with self.assertRaises(OSError):
                validate_and_log(self.idea, self.state, now=NOW, trading_day=DAY,
                                 control_file=control, audit_file=Path(temp))


if __name__ == "__main__":
    unittest.main()
