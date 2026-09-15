"""Full offline cycle: reconcile -> resolve -> exits -> entries against a stateful fake broker."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError

from alpaca_agents.controller import CycleConfig, RuntimeLock, approved_playbooks, loop, previous_weekday, run_cycle
from alpaca_agents.executor.client import ExecutorError
from alpaca_agents.executor.client import Credentials, PaperClient, TraceStore
from alpaca_agents.executor.fills import FillLedger
from alpaca_agents.executor.history import ActivityStore
from alpaca_agents.executor.orders import OrderJournal
from alpaca_agents.notify import Notifier

ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 9, 15)
T0 = datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc)
CONTRACT = "IWM261016C00205000"


class Response(BytesIO):
    def __init__(self, payload, code=200):
        super().__init__(json.dumps(payload).encode())
        self.code, self.headers = code, {"X-Request-ID": "rid"}


class FakeBroker:
    """Minimal stateful paper broker: orders, positions, activities, clock."""

    def __init__(self, clock):
        self.clock = clock
        self.orders, self.positions, self.activities = {}, {}, []
        self.calls, self.reject_next = [], None
        self.marks = {}

    def now(self):
        return self.clock()

    def fill(self, client_order_id, price):
        order = next(o for o in self.orders.values() if o["client_order_id"] == client_order_id)
        order["status"], order["filled_avg_price"] = "filled", price
        qty = int(order["qty"])
        if order["side"] == "buy":
            self.positions[order["symbol"]] = self.positions.get(order["symbol"], 0) + qty
        else:
            self.positions[order["symbol"]] -= qty
            if self.positions[order["symbol"]] == 0:
                del self.positions[order["symbol"]]
        self.activities.append({"id": f"act-{len(self.activities)+1}", "activity_type": "FILL", "type": "fill",
                                "transaction_time": self.now().isoformat(), "price": price, "qty": str(qty),
                                "side": order["side"], "symbol": order["symbol"], "order_id": order["id"],
                                "cum_qty": str(qty), "leaves_qty": "0"})

    def open(self, request, timeout):
        url = request.full_url
        path = url.split("paper-api.alpaca.markets")[1]
        route, _, query = path.partition("?")
        self.calls.append((request.get_method(), route))
        if request.get_method() == "POST":
            if self.reject_next:
                code, self.reject_next = self.reject_next, None
                raise HTTPError(url, code, "rejected", {"X-Request-ID": "rid-rej"}, BytesIO(b"{}"))
            body = json.loads(request.data)
            oid = f"bo-{len(self.orders)+1}"
            self.orders[oid] = {**body, "id": oid, "status": "accepted", "asset_class": "us_option"}
            return Response(self.orders[oid])
        if route == "/v2/account":
            return Response({"id": "acct", "status": "ACTIVE", "trading_blocked": False, "account_blocked": False,
                             "trade_suspended_by_user": False, "pattern_day_trader": False, "options_trading_level": 2,
                             "multiplier": "1", "cash": "2000.00", "non_marginable_buying_power": "2000.00",
                             "options_buying_power": "2000.00", "created_at": "2026-09-01T00:00:00Z"})
        if route == "/v2/clock":
            return Response({"timestamp": self.now().isoformat(), "is_open": True})
        if route == "/v2/positions":
            return Response([{"asset_class": "us_option", "symbol": s, "qty": str(q), "current_price": str(self.marks.get(s, "0.90"))}
                             for s, q in self.positions.items()])
        if route == "/v2/orders":
            return Response([o for o in self.orders.values() if o["status"] in ("accepted", "new")])
        if route == "/v2/orders:by_client_order_id":
            cid = query.split("client_order_id=")[1]
            for o in self.orders.values():
                if o["client_order_id"] == cid:
                    return Response(o)
            raise HTTPError(url, 404, "nf", {"X-Request-ID": "rid"}, BytesIO(b"{}"))
        if route == "/v2/account/activities":
            params = dict(p.split("=") for p in query.split("&"))
            after = datetime.fromisoformat(params["after"].replace("%3A", ":").replace("Z", "+00:00"))
            until = datetime.fromisoformat(params["until"].replace("%3A", ":").replace("Z", "+00:00"))
            if "page_token" in params:
                return Response([])
            return Response([a for a in self.activities if after < datetime.fromisoformat(a["transaction_time"]) < until])
        raise AssertionError(route)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.t = T0
        self.broker = FakeBroker(lambda: self.t)
        self.client = PaperClient(Credentials("k", "s"), TraceStore(self.root / "t.sqlite3"), opener=self.broker)
        self.control = self.root / "control"
        self.control.write_text("ARMED_PAPER")
        self.journal = OrderJournal(self.root / "o.sqlite3", account_id="acct", control_file=self.control)
        self.ledger = FillLedger(self.root / "f.sqlite3")
        self.store = ActivityStore(self.root / "a.sqlite3")
        self.notifier = Notifier(self.root / "n.jsonl")
        self.idea = json.loads((ROOT / "examples/long_call.json").read_text())
        self.idea["playbook"] = "trend_directional"
        self.idea["exit_plan"] = {"premium_stop_pct": 50, "time_stop_dte": 21, "underlying_stop_rule": "n/a"}
        self.ideas = {"proposals": [self.idea], "shadow": [self.idea], "skipped": []}
        self.closes = {"IWM": Decimal("203")}

    def cycle(self, *, submit=True, enabled=frozenset({"trend_directional"}), ideas=None):
        # A real controller uses wall-clock 'now' for reserve/claim; the fake broker
        # clock must agree within 60s, so pin the fake clock to real time.
        self.t = datetime.now(timezone.utc)
        return run_cycle(client=self.client, ledger=self.ledger, store=self.store, journal=self.journal,
                         notifier=self.notifier, ideas_provider=lambda **kw: self.ideas if ideas is None else ideas,
                         closes_provider=lambda symbols: self.closes, now=self.t,
                         config=CycleConfig(submit=submit, enabled_playbooks=enabled), report_path=self.root / "c.jsonl")

    def posts(self):
        return [c for c in self.broker.calls if c[0] == "POST"]

    def test_dry_run_reserves_but_never_claims_or_posts(self):
        report = self.cycle(submit=False)
        self.assertTrue(report["stages"]["reconcile"]["ok"], report["stages"]["reconcile"]["reasons"])
        entry = report["stages"]["entries"]["entries"][0]
        self.assertTrue(entry["reserved"])
        self.assertFalse(entry["claimed"])
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.journal.live_intents()[0]["status"], "reserved")

    def test_disabled_playbook_is_shadow_only(self):
        report = self.cycle(enabled=frozenset())
        self.assertEqual(report["stages"]["scan"], {"shadow": 1, "proposals": 0, "skipped": 0,
                                                    "refused": [{"symbol": "IWM", "reason": "playbook not enabled"}]})
        self.assertEqual(report["stages"]["entries"]["entries"], [])
        self.assertEqual(self.posts(), [])

    def test_full_lifecycle_entry_fill_hold_premium_stop_exit(self):
        # Cycle 1: entry reserved, claimed, POSTed.
        report = self.cycle()
        entry = report["stages"]["entries"]["entries"][0]
        self.assertEqual(entry["submission"]["outcome"], "submitted", entry)
        self.assertEqual(len(self.posts()), 1)
        sent = json.loads(self.broker.calls and self.broker.orders["bo-1"] and json.dumps(self.broker.orders["bo-1"]))
        self.assertEqual((sent["symbol"], sent["side"], sent["limit_price"], sent["qty"]), (CONTRACT, "buy", "0.90", "1"))
        intent = self.journal.live_intents()[0]
        self.assertEqual((intent["status"], intent["broker_order_id"]), ("claimed", "bo-1"))

        # Cycle 2: order still open at broker -> matched, pending=1, no second entry (symbol reserved).
        report = self.cycle()
        rec = report["stages"]["reconcile"]
        self.assertTrue(rec["ok"], rec["reasons"])
        self.assertEqual(rec["pending"], 1)
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(report["stages"]["entries"]["entries"], [])
        self.assertEqual(report["stages"]["scan"]["refused"][0]["reason"], "underlying already held or pending")

        # Broker fills; cycle 3 resolves the intent, imports the fill, holds the position.
        self.t = datetime.now(timezone.utc) - timedelta(seconds=1)   # fill lands inside the next import window
        self.broker.fill(intent["client_order_id"], "0.90")
        self.broker.marks[CONTRACT] = "0.85"
        report = self.cycle()
        rec = report["stages"]["reconcile"]
        self.assertTrue(rec["ok"], rec["reasons"])
        self.assertEqual([r["broker_status"] for r in rec["resolved"]], ["filled"])
        self.assertEqual((rec["open_positions"], rec["pending"]), (1, 0))
        self.assertEqual(self.ledger.inventory()[0]["contract"], CONTRACT)
        self.assertEqual(self.journal.live_intents(), [])
        self.assertTrue(report["stages"]["exits"][0]["note"].startswith("hold"))
        self.assertEqual(len(self.posts()), 1)
        # The fake provider ignores open_symbols; the controller must refuse a second IWM itself.
        self.assertEqual(report["stages"]["entries"]["entries"], [])
        self.assertEqual(report["stages"]["scan"]["refused"], [{"symbol": "IWM", "reason": "underlying already held or pending"}])

        # Mark collapses: cycle 4 fires the premium stop and POSTs a sell.
        self.broker.marks[CONTRACT] = "0.40"
        report = self.cycle()
        exit_record = report["stages"]["exits"][0]
        self.assertEqual(exit_record["note"], "premium_stop")
        self.assertTrue(exit_record["prepared"], exit_record)
        self.assertEqual(exit_record["submission"]["outcome"], "submitted")
        sell = self.broker.orders["bo-2"]
        self.assertEqual((sell["symbol"], sell["side"], sell["position_intent"], sell["limit_price"]), (CONTRACT, "sell", "sell_to_close", "0.38"))
        self.assertEqual(len(self.posts()), 2)

        # Cycle 5: sell open at broker -> matched exit, no duplicate exit, still reconciled.
        report = self.cycle()
        self.assertTrue(report["stages"]["reconcile"]["ok"], report["stages"]["reconcile"]["reasons"])
        self.assertTrue(report["stages"]["exits"][0].get("reason", "").startswith(("EXIT_PENDING", "DUPLICATE_DECISION")))
        self.assertEqual(len(self.posts()), 2)

        # Sell fills: cycle 6 books the loss (90 - 38 = 52 >= 40 breaker) and halts entries.
        self.t = datetime.now(timezone.utc) - timedelta(seconds=1)   # fill lands inside the next import window
        self.broker.fill(sell["client_order_id"], "0.38")
        report = self.cycle()
        rec = report["stages"]["reconcile"]
        self.assertTrue(rec["ok"], rec["reasons"])
        self.assertEqual(rec["open_positions"], 0)
        self.assertEqual(rec["daily_loss"], "52")
        self.assertTrue(rec["breaker"])
        self.assertEqual(self.ledger.inventory(), [])
        entry = report["stages"]["entries"]["entries"][0]
        self.assertFalse(entry["reserved"])
        self.assertTrue(entry["reason"].startswith("CIRCUIT_BREAKER"))
        self.assertEqual(len(self.posts()), 2)
        notes = [json.loads(l)["title"] for l in (self.root / "n.jsonl").read_text().splitlines()]
        self.assertIn("Entry trend_directional IWM", notes)
        self.assertIn(f"Exit premium_stop {CONTRACT}", notes)

    def test_broker_rejection_releases_slot_and_next_cycle_is_clean(self):
        self.broker.reject_next = 422
        report = self.cycle()
        self.assertEqual(report["stages"]["entries"]["entries"][0]["submission"]["outcome"], "unplaced")
        self.assertEqual(self.journal.live_intents(), [])
        report = self.cycle()
        self.assertTrue(report["stages"]["reconcile"]["ok"], report["stages"]["reconcile"]["reasons"])

    def test_unknown_outcome_becomes_incident_and_halts(self):
        self.broker.reject_next = 500
        report = self.cycle()
        self.assertEqual(report["stages"]["entries"]["entries"][0]["submission"]["outcome"], "unknown")
        self.assertEqual(self.journal.live_intents()[0]["status"], "claimed")
        report = self.cycle()
        self.assertFalse(report["stages"]["reconcile"]["ok"])
        self.assertTrue(any(r.startswith("CLAIMED_INTENT_WITHOUT_BROKER_RECORD") for r in report["stages"]["reconcile"]["reasons"]))
        self.assertEqual(report["stages"]["halt"], {"reason": "unreconciled"})
        levels = [json.loads(l)["level"] for l in (self.root / "n.jsonl").read_text().splitlines()]
        self.assertIn("incident", levels)
        self.assertEqual(len(self.posts()), 1)

    def test_exits_only_mode_blocks_entries(self):
        self.control.write_text("EXITS_ONLY")
        report = self.cycle()
        self.assertEqual(report["stages"]["entries"], {"skipped": "control mode EXITS_ONLY"})
        self.assertEqual(self.posts(), [])
        self.control.unlink()
        report = self.cycle()
        self.assertEqual(report["stages"]["entries"], {"skipped": "control mode DISABLED"})

    def test_provider_failure_is_contained(self):
        def boom(**kw):
            raise RuntimeError("secret-url")
        report = self.cycle(ideas=None)
        self.assertTrue(report["stages"]["reconcile"]["ok"])
        report = run_cycle(client=self.client, ledger=self.ledger, store=self.store, journal=self.journal,
                           notifier=self.notifier, ideas_provider=boom, closes_provider=lambda s: {},
                           now=datetime.now(timezone.utc), config=CycleConfig(submit=True, enabled_playbooks=frozenset({"trend_directional"})),
                           report_path=self.root / "c.jsonl")
        self.assertEqual(report["stages"]["entries"], {"error": "RuntimeError"})
        self.assertNotIn("secret-url", (self.root / "n.jsonl").read_text())
        self.assertNotIn("secret-url", (self.root / "c.jsonl").read_text())

    def test_playbook_approval_markers(self):
        approvals = self.root / "playbooks"
        approvals.mkdir()
        enabled, refused = approved_playbooks(["trend_directional", "bogus", "oversold_bounce"], approvals)
        self.assertEqual(enabled, frozenset())
        self.assertEqual([r["playbook"] for r in refused], ["trend_directional", "bogus", "oversold_bounce"])
        (approvals / "trend_directional.approved").write_text("APPROVED\n")
        (approvals / "oversold_bounce.approved").write_text("approved")
        enabled, refused = approved_playbooks(["trend_directional", "oversold_bounce"], approvals)
        self.assertEqual(enabled, frozenset({"trend_directional"}))
        self.assertEqual(refused[0]["playbook"], "oversold_bounce")

    def test_loop_stops_on_market_close_or_incident(self):
        scripted = [
            {"stages": {"reconcile": {"ok": True, "reasons": []}}, "finished_at": "t1"},
            {"stages": {"reconcile": {"ok": False, "reasons": ["MARKET_CLOSED"]}}, "finished_at": "t2"},
        ]
        slept, logs = [], []
        code = loop(lambda now: scripted.pop(0), every=60, log=logs.append, sleep=slept.append)
        self.assertEqual((code, slept), (0, [60]))
        self.assertTrue(logs[-1].startswith("market closed"))
        scripted = [{"stages": {"reconcile": {"ok": False, "reasons": ["CLAIMED_INTENT_WITHOUT_BROKER_RECORD: x"]}}}]
        self.assertEqual(loop(lambda now: scripted.pop(0), every=60, log=logs.append, sleep=slept.append), 3)
        self.assertEqual(slept, [60])
        scripted = [{"stages": {"reconcile": {"ok": False, "error": "Paper API transport_error"}}}]
        self.assertEqual(loop(lambda now: scripted.pop(0), every=60, log=logs.append, sleep=slept.append), 3)
        # A transient warning (e.g. clock skew) keeps looping.
        scripted = [{"stages": {"reconcile": {"ok": False, "reasons": ["BROKER_CLOCK_SKEW: 70s"]}}},
                    {"stages": {"reconcile": {"ok": False, "reasons": ["MARKET_CLOSED", "NOT_A_SESSION: x"]}}}]
        self.assertEqual(loop(lambda now: scripted.pop(0), every=90, log=logs.append, sleep=slept.append), 0)
        self.assertEqual(slept, [60, 90])

    def test_runtime_lock_is_exclusive_and_stale_locks_refuse(self):
        with RuntimeLock(self.root):
            self.assertTrue((self.root / "controller.lock").exists())
            with self.assertRaises(ExecutorError):
                with RuntimeLock(self.root):
                    pass
        self.assertFalse((self.root / "controller.lock").exists())
        (self.root / "controller.lock").write_text("stale")
        with self.assertRaises(ExecutorError):
            with RuntimeLock(self.root):
                pass

    def test_previous_weekday(self):
        self.assertEqual(previous_weekday(date(2026, 9, 14)), date(2026, 9, 11))   # Mon -> Fri
        self.assertEqual(previous_weekday(date(2026, 9, 15)), date(2026, 9, 14))
        self.assertEqual(previous_weekday(date(2026, 9, 8)), date(2026, 9, 4))    # Labor Day skipped


if __name__ == "__main__":
    unittest.main()
