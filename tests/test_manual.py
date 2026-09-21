"""Owner-drafted idea: joins the front of Layer 1, never a shortcut past Moon or the journal."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from alpaca_agents.controller import approved_playbooks
from alpaca_agents.manual import MAX_AGE, consume, load_draft, main
from alpaca_agents.rules import RiskState, evaluate
from alpaca_agents.scanner.contracts import OptionQuote
from alpaca_agents.scanner.scan import MANUAL_PLAYBOOK, NO_EARNINGS_ETFS, ScanConfig, SymbolSnapshot, build_manual_idea, earnings_exempt
from tests import test_controller
from tests.test_scanner import bars_from_closes

NOW = datetime(2026, 9, 21, 15, tzinfo=timezone.utc)
SESSION = date(2026, 9, 18)
EXPIRY = date(2026, 10, 23)


def quote(ask="0.66", delta=0.36, right="call", oi=2700, strike="82.5"):
    ask = Decimal(ask)
    return OptionQuote(EXPIRY, Decimal(strike), right, ask - Decimal("0.01"), ask, delta if right == "call" else -delta, oi)


def snapshot(symbol="TLT", chain=None):
    closes = [80 + 0.02 * i + (0.3 if i % 2 else -0.3) for i in range(220)]   # gentle drift, real daily range for ATR
    return SymbolSnapshot(symbol, bars_from_closes(closes, last=SESSION), chain if chain is not None else (quote(), quote(right="put")),
                          None, iv_rank=None, earnings_not_applicable=True, valuation_day=NOW.date())


class BuildManualIdeaTests(unittest.TestCase):
    def test_builds_scanner_shaped_idea_that_moon_approves(self):
        idea = build_manual_idea(snapshot(), "call", ScanConfig(universe=frozenset({"TLT"})), as_of=SESSION, contract_day=NOW.date(), note="demo")
        self.assertIsInstance(idea, dict, idea)
        self.assertEqual((idea["playbook"], idea["strategy"], idea["direction"], idea["quantity"]), (MANUAL_PLAYBOOK, "long_call", "long", 1))
        self.assertEqual(idea["limit_debit"], "0.66")
        self.assertEqual(idea["est_contract_cost"], "66.00")
        self.assertIn("MANUAL (no signal): demo", idea["thesis"])
        self.assertEqual(idea["exit_plan"]["premium_stop_pct"], 50)
        stop, entry, target = (Decimal(idea[k]) for k in ("stop", "entry_trigger", "target"))
        self.assertTrue(stop < entry < target)
        self.assertEqual(Decimal(idea["reward_risk"]), Decimal("1.50"))
        state = RiskState(NOW.date(), NOW, 0, 0, Decimal(0), Decimal(2000), (), reconciled=True)
        verdict = evaluate(idea, state, now=NOW, trading_day=NOW.date(), kill_switch=False)
        self.assertTrue(verdict["approved"], verdict["reason"])
        put = build_manual_idea(snapshot(), "put", ScanConfig(universe=frozenset({"TLT"})), as_of=SESSION, contract_day=NOW.date(), note="demo")
        self.assertEqual((put["strategy"], put["direction"]), ("long_put", "short"))
        self.assertTrue(Decimal(put["target"]) < Decimal(put["entry_trigger"]) < Decimal(put["stop"]))
        self.assertTrue(evaluate(put, state, now=NOW, trading_day=NOW.date(), kill_switch=False)["approved"])

    def test_cap_liquidity_and_inputs_are_enforced_at_draft_time(self):
        cfg = ScanConfig(universe=frozenset({"TLT"}))
        self.assertIn("exceeds 100 premium cap", build_manual_idea(snapshot(chain=(quote(ask="1.20"),)), "call", cfg, as_of=SESSION, contract_day=NOW.date(), note="x"))
        self.assertIn("no liquid", build_manual_idea(snapshot(chain=(quote(delta=0.10),)), "call", cfg, as_of=SESSION, contract_day=NOW.date(), note="x"))
        self.assertIn("no liquid", build_manual_idea(snapshot(chain=(quote(oi=10),)), "call", cfg, as_of=SESSION, contract_day=NOW.date(), note="x"))
        self.assertIn("note", build_manual_idea(snapshot(), "call", cfg, as_of=SESSION, contract_day=NOW.date(), note="  "))
        self.assertIn("right", build_manual_idea(snapshot(), "straddle", cfg, as_of=SESSION, contract_day=NOW.date(), note="x"))
        self.assertIn("not current", build_manual_idea(snapshot(), "call", cfg, as_of=SESSION - timedelta(days=1), contract_day=NOW.date(), note="x"))

    def test_no_earnings_allowlist_is_explicit(self):
        self.assertTrue(earnings_exempt("TLT") and earnings_exempt("QQQ"))
        self.assertFalse(earnings_exempt("AAPL") or earnings_exempt("TQQQ") or earnings_exempt("SQQQ"))
        self.assertTrue(all(s.isupper() and s.isalpha() for s in NO_EARNINGS_ETFS))


class DraftFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "manual-idea.json"

    def save(self, **over):
        data = {"schema_version": 1, "id": "a" * 32, "drafted_at": NOW.isoformat(), "idea": {"playbook": MANUAL_PLAYBOOK, "symbol": "TLT"}}
        data.update(over)
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def test_fresh_draft_loads_and_every_defect_is_refused(self):
        self.save()
        idea, ident = load_draft(self.path, now=NOW + timedelta(minutes=5))
        self.assertEqual((idea["symbol"], ident), ("TLT", "a" * 32))
        self.assertIsNone(load_draft(self.path, now=NOW + MAX_AGE + timedelta(seconds=1))[0])
        self.assertIn("expired", load_draft(self.path, now=NOW + MAX_AGE + timedelta(seconds=1))[1])
        self.assertIn("future", load_draft(self.path, now=NOW - timedelta(seconds=5))[1])
        for over in ({"schema_version": 2}, {"id": "short"}, {"idea": {"playbook": "trend_directional"}}, {"idea": "x"},
                     {"drafted_at": "2026-09-21T15:00:00"}, {"drafted_at": "yesterday"}):
            self.save(**over)
            self.assertIsNone(load_draft(self.path, now=NOW)[0], over)
        self.path.write_bytes(b'{"schema_version": 1, "schema_version": 1}')
        self.assertIsNone(load_draft(self.path, now=NOW)[0])
        self.path.unlink()
        self.assertEqual(load_draft(self.path, now=NOW), (None, "no draft"))

    def test_consume_retires_the_file_once(self):
        self.save()
        used = consume(self.path, "a" * 32)
        self.assertTrue(used.name.startswith("manual-idea.used-"))
        self.assertFalse(self.path.exists())
        self.assertEqual(load_draft(self.path, now=NOW), (None, "no draft"))
        self.assertIsNone(consume(self.path, "a" * 32))

    def test_manual_needs_its_own_approval_marker(self):
        approvals = Path(self.tmp.name) / "playbooks"
        approvals.mkdir()
        enabled, refused = approved_playbooks([MANUAL_PLAYBOOK], approvals)
        self.assertEqual(enabled, frozenset())
        self.assertIn("missing approval marker", refused[0]["reason"])
        (approvals / "manual.approved").write_text("APPROVED", encoding="utf-8")
        enabled, refused = approved_playbooks([MANUAL_PLAYBOOK, "bogus"], approvals)
        self.assertEqual(enabled, frozenset({MANUAL_PLAYBOOK}))
        self.assertEqual(refused, [{"playbook": "bogus", "reason": "unknown playbook"}])

    def test_cli_refuses_unverified_stock_before_any_network_call(self):
        rt = Path(self.tmp.name)
        with patch("alpaca_agents.marketdata.client.MarketDataClient") as client, \
             patch("alpaca_agents.marketdata.client.DataCredentials.from_environment"):
            self.assertEqual(main(["draft", "AAPL", "call", "--note", "demo", "--runtime", str(rt)]), 1)
            client.assert_not_called()
        self.assertFalse(self.path.exists())
        self.assertEqual(main(["show", "--runtime", str(rt)]), 1)


class ManualLifecycleTests(unittest.TestCase):
    """The manual idea rides the real cycle against the stateful fake broker."""
    setUp, cycle, posts, backdate_entries = (test_controller.ControllerTests.setUp, test_controller.ControllerTests.cycle,
                                             test_controller.ControllerTests.posts, test_controller.ControllerTests.backdate_entries)

    def manual_ideas(self):
        idea = build_manual_idea(snapshot("IWM", chain=(quote(ask="0.90", strike="205", right="call"),)), "call",
                                 ScanConfig(universe=frozenset({"IWM"})), as_of=SESSION, contract_day=NOW.date(), note="demo")
        self.assertIsInstance(idea, dict, idea)
        idea["legs"][0]["expiration"] = "2026-10-16"   # match the fake broker's contract
        return {"proposals": [idea], "shadow": [], "skipped": []}

    def test_manual_idea_is_refused_without_enablement_and_submitted_with_it(self):
        report = self.cycle(ideas=self.manual_ideas(), enabled=frozenset({"trend_directional"}))
        self.assertEqual(report["stages"]["scan"]["refused"], [{"symbol": "IWM", "reason": "playbook not enabled"}])
        self.assertEqual(self.posts(), [])
        report = self.cycle(ideas=self.manual_ideas(), enabled=frozenset({MANUAL_PLAYBOOK}))
        entry = report["stages"]["entries"]["entries"][0]
        self.assertEqual((entry["playbook"], entry["reserved"], entry["claimed"]), (MANUAL_PLAYBOOK, True, True), entry)
        self.assertEqual(entry["submission"]["outcome"], "submitted")
        self.assertEqual(len(self.posts()), 1)
        intent = self.journal.live_intents()[0]
        self.assertEqual(intent["status"], "claimed")
        # Fill, hold, then the premium stop takes it out through the ordinary exit path.
        self.t = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.broker.fill(intent["client_order_id"], "0.90")
        self.broker.marks["IWM261016C00205000"] = "0.85"
        report = self.cycle(ideas=self.manual_ideas(), enabled=frozenset({MANUAL_PLAYBOOK}))
        self.assertEqual(report["stages"]["reconcile"]["open_positions"], 1)
        self.assertEqual(report["stages"]["scan"]["refused"][0]["reason"], "underlying already held or pending")
        self.backdate_entries()
        self.broker.marks["IWM261016C00205000"] = "0.40"
        report = self.cycle(ideas={"proposals": [], "shadow": [], "skipped": []}, enabled=frozenset({MANUAL_PLAYBOOK}))
        exit_record = report["stages"]["exits"][0]
        self.assertEqual((exit_record["note"], exit_record["prepared"]), ("premium_stop", True), exit_record)
        self.assertEqual(exit_record["submission"]["outcome"], "submitted")
        self.assertEqual(len(self.posts()), 2)

    def test_dry_run_never_claims_a_manual_idea(self):
        report = self.cycle(ideas=self.manual_ideas(), enabled=frozenset({MANUAL_PLAYBOOK}), submit=False)
        entry = report["stages"]["entries"]["entries"][0]
        self.assertEqual((entry["reserved"], entry["claimed"]), (True, False))
        self.assertEqual(self.posts(), [])
