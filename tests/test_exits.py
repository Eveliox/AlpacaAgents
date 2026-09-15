from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from alpaca_agents.executor.exits import HeldOption, evaluate_exit, exit_limit, weekdays_between
from alpaca_agents.executor.orders import OrderJournal
from alpaca_agents.gateway import control_mode, trading_disabled

NOW = datetime(2026, 9, 15, 14, tzinfo=timezone.utc)
DAY = date(2026, 9, 15)
ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "IWM261016C00205000"   # expires 2026-10-16: 31 DTE on DAY


def idea(**over):
    base = json.loads((ROOT / "examples/long_call.json").read_text())
    base["exit_plan"] = {"premium_stop_pct": 50, "time_stop_dte": 21, "underlying_stop_rule": "n/a"}
    base.update(over)
    return base


def held(**over):
    base = dict(contract=CONTRACT, quantity=1, basis=Decimal("90.65"), mark=Decimal("0.80"),
                underlying_close=Decimal("203.00"), entry_day=date(2026, 9, 10))
    base.update(over)
    return HeldOption(**base)


class ExitRuleTests(unittest.TestCase):
    def test_hold_when_nothing_fires(self):
        decision, note = evaluate_exit(held(), idea(), trading_day=DAY)
        self.assertIsNone(decision)
        self.assertTrue(note.startswith("hold"))

    def test_rule_priority_and_payloads(self):
        i = idea()  # long, stop 199, target 210 per examples/long_call.json
        stop_close = Decimal(i["stop"])
        cases = [
            ("underlying_stop", held(underlying_close=stop_close, mark=Decimal("0.10"))),      # beats premium stop
            ("premium_stop", held(mark=Decimal("0.45"))),                                       # 45 <= 45.33 (50% of 90.65)
            ("underlying_target", held(underlying_close=Decimal(i["target"]))),
            ("time_stop", held(entry_day=date(2026, 8, 1))),  # DTE 31 > 21; sessions rule absent -> not yet
        ]
        for expected, position in cases[:3]:
            with self.subTest(rule=expected):
                decision, note = evaluate_exit(position, i, trading_day=DAY)
                self.assertEqual(note, expected)
                self.assertEqual(decision["exit_reason"], expected)
                self.assertEqual((decision["action"], decision["contract"], decision["quantity"]), ("close", CONTRACT, 1))
                self.assertEqual(decision["limit_price"], exit_limit(position.mark))
        decision, note = evaluate_exit(cases[3][1], i, trading_day=DAY)
        self.assertIsNone(decision)

    def test_time_stops(self):
        decision, _ = evaluate_exit(held(), idea(), trading_day=date(2026, 9, 25))   # 21 DTE
        self.assertEqual(decision["exit_reason"], "time_stop")
        plan = {"premium_stop_pct": 50, "time_stop_dte": 5, "time_stop_sessions": 3, "underlying_stop_rule": "n/a"}
        decision, _ = evaluate_exit(held(entry_day=date(2026, 9, 10)), idea(exit_plan=plan), trading_day=DAY)  # Thu->Tue = 3 weekdays
        self.assertEqual(decision["exit_reason"], "time_stop")
        self.assertIn("sessions", decision["detail"])

    def test_premium_stop_boundary_uses_basis_with_fees(self):
        # basis 90.65 -> floor 45.33; mark 0.4534 not encodable in cents; test 0.46 vs 0.45
        self.assertIsNone(evaluate_exit(held(mark=Decimal("0.46")), idea(), trading_day=DAY)[0])
        self.assertIsNotNone(evaluate_exit(held(mark=Decimal("0.45")), idea(), trading_day=DAY)[0])

    def test_short_direction_mirrors(self):
        put = idea(strategy="long_put", direction="short", stop="210", target="195", entry_trigger="203")
        self.assertEqual(evaluate_exit(held(underlying_close=Decimal("210")), put, trading_day=DAY)[1], "underlying_stop")
        self.assertEqual(evaluate_exit(held(underlying_close=Decimal("194")), put, trading_day=DAY)[1], "underlying_target")

    def test_missing_inputs_degrade_safely(self):
        # No close: underlying rules skipped, premium/time still work.
        self.assertEqual(evaluate_exit(held(underlying_close=None, mark=Decimal("0.30")), idea(), trading_day=DAY)[1], "premium_stop")
        # No mark: rule may fire but nothing can be priced -> manual note, no decision.
        decision, note = evaluate_exit(held(mark=None, underlying_close=Decimal("100")), idea(), trading_day=DAY)
        self.assertIsNone(decision)
        self.assertIn("manual", note)
        for bad in (idea(strategy="call_debit_spread"), idea(exit_plan=None), idea(direction="up"), {}):
            self.assertIsNone(evaluate_exit(held(), bad, trading_day=DAY)[0])
        self.assertIsNone(evaluate_exit(held(entry_day=DAY.replace(day=20)), idea(), trading_day=DAY)[0])
        self.assertIsNone(evaluate_exit(held(basis=Decimal("0")), idea(), trading_day=DAY)[0])

    def test_helpers(self):
        self.assertEqual(exit_limit(Decimal("0.80")), "0.76")
        self.assertEqual(exit_limit(Decimal("0.01")), "0.01")
        self.assertEqual(exit_limit(Decimal("0.005")), "0.01")
        self.assertEqual(weekdays_between(date(2026, 9, 11), date(2026, 9, 14)), 1)   # Fri -> Mon
        self.assertEqual(weekdays_between(date(2026, 9, 1), date(2026, 9, 15)), 10)
        self.assertEqual(weekdays_between(DAY, DAY), 0)


class ControlModeTests(unittest.TestCase):
    def test_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "control"
            self.assertEqual(control_mode(f), "DISABLED")
            for content, mode, disabled in (("ARMED_PAPER", "ARMED_PAPER", False), ("EXITS_ONLY", "EXITS_ONLY", True),
                                            ("DISABLED", "DISABLED", True), ("armed_paper", "DISABLED", True), ("", "DISABLED", True)):
                f.write_text(content)
                self.assertEqual(control_mode(f), mode)
                self.assertEqual(trading_disabled(f), disabled)


class PrepareExitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.control = root / "control"
        self.control.write_text("ARMED_PAPER")
        self.journal = OrderJournal(root / "o.sqlite3", account_id="acct", control_file=self.control)
        self.inventory = [{"position_id": "p1", "quantity": 1, "remaining_basis": Decimal("90.65"), "contract": CONTRACT}]
        self.decision = {"action": "close", "contract": CONTRACT, "quantity": 1, "limit_price": "0.76",
                         "exit_reason": "premium_stop", "detail": "test"}

    def prepare(self, key="x1", decision=None, inventory=None):
        return self.journal.prepare_exit(key, self.decision if decision is None else decision,
                                         inventory=self.inventory if inventory is None else inventory, now=NOW, trading_day=DAY)

    def test_exit_is_claimed_immediately_with_sell_body(self):
        result = self.prepare()
        self.assertTrue(result["approved"], result["reason"])
        body = result["prepared_order"]
        self.assertEqual((body["symbol"], body["qty"], body["side"], body["position_intent"], body["limit_price"]),
                         (CONTRACT, "1", "sell", "sell_to_close", "0.76"))
        self.assertEqual(body["client_order_id"], "paper-" + result["authorization_id"])
        live = self.journal.live_intents()[0]
        self.assertEqual((live["kind"], live["status"], live["contract"], live["symbol"]), ("exit", "claimed", CONTRACT, "IWM"))
        self.assertEqual(self.journal.sendable_body(result["authorization_id"]), body)
        self.assertEqual(self.journal.events()[0]["event"], "exit_approved")

    def test_one_live_exit_per_contract_and_single_use_keys(self):
        self.assertTrue(self.prepare("a")["approved"])
        self.assertTrue(self.prepare("b")["reason"].startswith("EXIT_PENDING"))
        self.assertTrue(self.prepare("a")["reason"].startswith("DUPLICATE_DECISION"))

    def test_validation_rejections(self):
        cases = {
            "NOT_HELD": dict(decision={**self.decision, "quantity": 2}),
            "INVALID_EXIT: OCC": dict(decision={**self.decision, "contract": "IWM"}),
            "INVALID_EXIT: unknown exit": dict(decision={**self.decision, "exit_reason": "vibes"}),
            "INVALID_EXIT: cent": dict(decision={**self.decision, "limit_price": "0.7"}),
            "INVALID_EXIT: positive": dict(decision={**self.decision, "quantity": 0}),
            "INVALID_EXIT: action": dict(decision={**self.decision, "action": "open"}),
            "INVALID_EXIT": dict(decision="nope"),
        }
        for i, (code, kwargs) in enumerate(cases.items()):
            with self.subTest(code=code):
                result = self.prepare(f"k{i}", **kwargs)
                self.assertFalse(result["approved"])
                self.assertTrue(result["reason"].startswith(code), result["reason"])
        self.assertTrue(self.prepare("empty", inventory=[])["reason"].startswith("NOT_HELD"))
        self.assertEqual(self.journal.live_intents(), [])

    def test_exits_only_mode_allows_exits_but_not_entries(self):
        self.control.write_text("EXITS_ONLY")
        self.assertTrue(self.prepare()["approved"])
        self.control.write_text("DISABLED")
        self.assertTrue(self.prepare("k2")["reason"].startswith("KILL_SWITCH"))
        self.control.unlink()
        self.assertTrue(self.prepare("k3")["reason"].startswith("KILL_SWITCH"))

    def test_exit_does_not_consume_entry_capacity(self):
        from alpaca_agents.rules import RiskState
        self.assertTrue(self.prepare()["approved"])
        state = RiskState(DAY, NOW, 1, 0, Decimal(0), Decimal(2000), (), reconciled=True)
        entry = json.loads((ROOT / "examples/long_call.json").read_text())
        entry["symbol"] = "SPY"
        for leg in entry["legs"]:
            leg["symbol"] = "SPY"
        reserved = self.journal.reserve("e1", entry, state_provider=lambda: state, now=NOW, trading_day=DAY)
        self.assertTrue(reserved["approved"], reserved["reason"])


if __name__ == "__main__":
    unittest.main()
