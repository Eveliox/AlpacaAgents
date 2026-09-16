from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from alpaca_agents.dashboard import AGENTS, build, collect, render
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
        self.assertNotIn("<script", page)
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
        self.assertNotIn("<script", page)
        self.assertNotIn("<form", page)

    def test_risk_blockers_are_escaped_in_moon_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = collect(Path(tmp), now=NOW)
            data["cycles"] = [{"stages": {"reconcile": {"ok": False, "reasons": ["<unsafe>"]}}}]
            page = render(data)
            self.assertIn("Latest reconciliation blockers", page)
            self.assertIn("&lt;unsafe&gt;", page)
            self.assertNotIn("<unsafe>", page)


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
