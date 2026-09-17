from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from alpaca_agents.dashboard import AGENTS, _age, _t, build, collect, next_action, render
from alpaca_agents.executor.fills import FillLedger, OptionFill
from alpaca_agents.executor.orders import OrderJournal
from alpaca_agents.notify import Notifier
from alpaca_agents.rules import RiskState
from tests.test_controller import ControllerTests

NOW = datetime(2026, 9, 15, 14, tzinfo=timezone.utc)


class DashboardFromLifecycleTests(ControllerTests):
    """Reuse the controller fixture so the dashboard reads the real schemas."""

    def test_renders_after_full_lifecycle(self):
        self.test_full_lifecycle_entry_fill_hold_premium_stop_exit()
        # Lay the files out as runtime/ expects.
        rt = self.root / "runtime"
        rt.mkdir()
        shutil.copy(self.root / "o.sqlite3", rt / "orders.sqlite3")
        shutil.copy(self.root / "f.sqlite3", rt / "fills.sqlite3")
        shutil.copy(self.root / "t.sqlite3", rt / "api-requests.sqlite3")
        shutil.copy(self.root / "c.jsonl", rt / "cycles.jsonl")
        shutil.copy(self.root / "n.jsonl", rt / "notifications.jsonl")
        shutil.copy(self.control, rt / "trading-control")
        (rt / "playbooks").mkdir()
        (rt / "playbooks" / "trend_directional.approved").write_text("APPROVED")
        (rt / "shadow-scan.json").write_text(json.dumps({"generated_at": "x", "shadow": [self.idea, self.idea]}))
        now = datetime.now(timezone.utc)
        data = collect(rt, now=now)
        self.assertEqual(data["daily_loss"], Decimal("52"))
        self.assertTrue(data["latched"])
        self.assertEqual(data["inventory"], [])
        self.assertEqual(len(data["closed"]), 1)
        self.assertEqual(data["shadow_counts"]["trend_directional"], 2)
        self.assertTrue(data["approvals"]["trend_directional"])
        self.assertFalse(data["approvals"]["oversold_bounce"])
        self.assertGreaterEqual(len(data["cycles"]), 6)
        out = build(rt, rt / "dashboard.html", now=now)
        page = out.read_text(encoding="utf-8")
        for needle in ("ARMED_PAPER", "$52.00 / $40.00", "latched", "IWM261016C00205000", "resolved:filled",
                       "Exit premium_stop", "POST", "/v2/orders", "APPROVED", "shadow only", "reconciled"):
            self.assertIn(needle, page, needle)
        self.assertEqual(page.count("<script"), 2)  # static code + escaped JSON context only
        self.assertNotIn("test-secret", page)


class DashboardAgentTests(unittest.TestCase):
    def test_named_roles_have_accessible_offline_filters_and_owned_panels(self):
        from html.parser import HTMLParser

        class Page(HTMLParser):
            def __init__(self):
                super().__init__()
                self.inputs, self.labels, self.sections = [], {}, []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "input":
                    self.inputs.append(attrs)
                elif tag == "label":
                    self.labels[attrs["for"]] = attrs
                elif tag == "section":
                    self.sections.append(attrs)

        with tempfile.TemporaryDirectory() as tmp:
            page = render(collect(Path(tmp), now=NOW))
        parsed = Page()
        parsed.feed(page)
        self.assertEqual(len(parsed.inputs), 5)
        self.assertEqual([r["id"] for r in parsed.inputs if "checked" in r], ["agent-all"])
        for item in parsed.inputs:
            self.assertEqual((item["type"], item["name"]), ("radio", "agent"))
            self.assertIn(item["id"], parsed.labels)
        self.assertEqual(len([s for s in parsed.sections if "data-agent" not in s]), 1)  # global status stays visible
        for key, name, role, _ in AGENTS:
            self.assertIn(name, page)
            self.assertIn(role.replace("&", "&amp;"), page)
            self.assertGreaterEqual(sum(s.get("data-agent") == key for s in parsed.sections), 2)
            self.assertIn(f'#agent-{key}:checked ~ main [data-agent]:not([data-agent="{key}"]){{display:none}}', page)
        for agent, title in (("star", "Playbooks"), ("moon", "Risk checks"), ("houston", "Order intents"),
                             ("astra", "Notifications")):
            self.assertIn(f'<span class="agent-owner">{agent.title()}</span><h2>{title}</h2>', page)
        self.assertIn("No controller reconciliation snapshot yet", page)
        self.assertEqual(page.count("<script"), 2)
        self.assertIn('form-action &#x27;none&#x27;', page)
        self.assertIn('id="chat-form"', page)

    def test_risk_blockers_are_escaped_in_moon_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = collect(Path(tmp), now=NOW)
            data["cycles"] = [{"stages": {"reconcile": {"ok": False, "reasons": ["<unsafe>"]}}}]
            page = render(data)
            self.assertIn("Latest reconciliation blockers", page)
            self.assertIn("&lt;unsafe&gt;", page)
            self.assertNotIn("<unsafe>", page)


class DashboardWorkspaceTests(unittest.TestCase):
    def data(self, root):
        return collect(root, now=NOW)

    def test_missing_ledgers_are_unknown_not_zero_or_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = render(self.data(Path(tmp)))
        self.assertIn("Check missing or unreadable records", page)
        self.assertIn("<strong>Unknown</strong>", page)
        self.assertNotIn("$0.00 / $40.00", page)
        self.assertIn("No controller check yet", page)
        self.assertIn("Controller running status is not verified", page)

    def test_corrupt_files_do_not_crash_or_silently_report_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "fills.sqlite3").write_text("broken database")
            (root / "cycles.jsonl").write_text('null\n[]\n{"unfinished"\n')
            (root / "shadow-scan.json").write_text('[]')
            (root / "playbooks").mkdir()
            (root / "playbooks/trend_directional.approved").write_text("APPROVED", encoding="utf-16")
            data = self.data(root)
            self.assertFalse(data["ledger_known"])
            self.assertFalse(data["approvals"]["trend_directional"])
            page = render(data)
        self.assertIn("could not read journal", page)
        self.assertIn("malformed record skipped", page)
        self.assertIn("shadow-scan.json: report unreadable", page)

    def test_snapshot_age_handles_stale_unknown_and_future(self):
        self.assertEqual(_age(NOW.isoformat(), NOW)[1], "ok")
        for stamp in (None, "invalid", "2026-09-15T14:00:00"):
            self.assertEqual(_age(stamp, NOW), ("Unknown age", "warn"))
        self.assertEqual(_age((NOW - timedelta(seconds=61)).isoformat(), NOW)[0], "Stale snapshot")
        self.assertEqual(_age((NOW + timedelta(seconds=1)).isoformat(), NOW)[1], "bad")
        self.assertEqual(_t("2026-09-16T02:00:00Z", NOW.date()), "2026-09-16 02:00:00 UTC")

    def test_next_action_priority_and_safe_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self.data(Path(tmp))
        data.update(issues=[], inventory=[], cycles=[])
        rec = {"ok": True}
        self.assertIn("first controller snapshot", next_action(data, {})[0])
        data["shadow"] = {"errors": [{"symbol": "QQQ", "reason": "status=403"}]}
        self.assertIn("Options data needs attention", next_action(data, rec)[0])
        self.assertIn("Stocks Advanced alone", next_action(data, rec)[1])
        # Risk incidents take priority over data subscriptions.
        blocked = {"ok": False, "reasons": ["POSITION_MISMATCH"]}
        self.assertIn("broker/risk blocker", next_action(data, blocked)[0])
        data["inventory"] = [{"contract": "test"}]
        self.assertIn("not being managed", next_action(data, blocked)[0])
        data.update(inventory=[], shadow={})
        self.assertIn("Market closed", next_action(data, {"reasons": ["MARKET_CLOSED"]})[0])
        self.assertIn("Refresh the controller snapshot", next_action(data, rec)[0])
        data["cycles"] = [{"started_at": NOW.isoformat()}]
        self.assertIn("Research mode", next_action(data, rec)[0])
        data["control"] = "EXITS_ONLY"
        self.assertIn("Monitor existing positions", next_action(data, rec)[0])
        self.assertNotIn("--submit", next_action(data, rec)[2])
        data["latched"] = True
        self.assertIn("Pause new entries", next_action(data, rec)[0])

    def test_backtests_are_research_not_option_profit_or_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = {"level": "underlying", "options_pnl_modelled": False, "symbol": "QQQ",
                      "first_bar": "2023-01-03", "last_bar": "2026-09-16",
                      "summary": {"trend": {"resolved": 37, "expectancy_r": 0.2527, "median_r": -0.42, "profit_factor": 2.6,
                                            "max_loss_r": -8.1756, "no_trade": 1, "sample_sufficient": True}}}
            (root / "bt-QQQ.json").write_text(json.dumps(report))
            (root / "bt-bad.json").write_text('{"summary":{}}')
            data = self.data(root)
            self.assertEqual(len(data["research"]), 1)
            page = render(data)
        for text in ("0.25", "-0.42", "2.60", "-8.2", "bt-QQQ.json", "Median R", "Profit factor", "Worst R",
                     "options P&amp;L is NOT modeled", "not statistical proof", "a few trades carry the result",
                     "No result here approves a playbook", "unsupported research report"):
            self.assertIn(text, page)
        self.assertNotIn("$25", page)
        self.assertFalse(any(data["approvals"].values()))

    def test_runtime_content_is_escaped_in_new_panels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hostile = '<img src=x onerror="alert(1)">'
            (root / "shadow-scan.json").write_text(json.dumps({"generated_at": hostile,
                "errors": [{"symbol": hostile, "reason": hostile}], "shadow": [{"symbol": hostile, "thesis": hostile}]}))
            (root / "bt-test.json").write_text(json.dumps({"level": "underlying", "options_pnl_modelled": False,
                "symbol": hostile, "summary": {hostile: {"expectancy_r": hostile}}}))
            page = render(self.data(root))
        self.assertNotIn(hostile, page)
        self.assertIn("&lt;img", page)
        self.assertEqual(page.count("<script"), 2)

    def test_build_only_writes_output_and_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "trading-control").write_text("DISABLED")
            (root / "bt-test.json").write_text('{}')
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            out = root / "dashboard.html"
            build(root, out, now=NOW)
            page = out.read_bytes()
            build(root, out, now=NOW)
            self.assertEqual(page, out.read_bytes())
            after = {p.name: p.read_bytes() for p in root.iterdir() if p != out}
            self.assertEqual(before, after)
            self.assertIn(b"<details>", page)


class DashboardEscapingTests(unittest.TestCase):
    def test_hostile_content_is_escaped_and_missing_files_are_fine(self):
        with tempfile.TemporaryDirectory() as tmp:
            rt = Path(tmp)
            page = render(collect(rt, now=NOW))
            self.assertIn("DISABLED", page)
            self.assertIn("never", page)
            hostile = "<script>alert(1)</script>"
            Notifier(rt / "notifications.jsonl").send("incident", hostile, now=NOW)
            (rt / "trading-control").write_text(hostile)
            journal = OrderJournal(rt / "orders.sqlite3", account_id="acct", control_file=rt / "trading-control")
            journal.reserve("k", {"symbol": hostile}, state_provider=lambda: RiskState(NOW.date(), NOW, 0, 0, Decimal(0), Decimal(1), (), reconciled=True),
                            now=NOW, trading_day=NOW.date())
            ledger = FillLedger(rt / "fills.sqlite3")
            ledger.record(OptionFill("e1", "p1", "IWM261016C00205000", "buy_to_open", 1, Decimal("90"), Decimal("0.65"), NOW, NOW.date()), recorded_at=NOW)
            page = render(collect(rt, now=NOW))
            self.assertNotIn(hostile, page)
            self.assertIn("&lt;script&gt;", page)
            self.assertIn("$90.65", page)
            self.assertIn("1 / 2", page)


if __name__ == "__main__":
    unittest.main()
