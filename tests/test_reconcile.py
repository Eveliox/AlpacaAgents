from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest

from alpaca_agents.executor.client import Credentials, PaperClient, TraceStore
from alpaca_agents.executor.eastern import eastern_date, eastern_offset, to_eastern
from alpaca_agents.executor.fills import FillLedger
from alpaca_agents.executor.history import ActivityStore
from alpaca_agents.executor.normalize import normalize_activities
from alpaca_agents.executor.reconcile import ReconcileConfig, build_risk_state, reconcile, trading_day_from_clock
from alpaca_agents.rules import evaluate

NOW = datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc)      # 10:00 ET, market open
DAY = date(2026, 9, 15)
CONTRACT = "IWM261016C00205000"


def clock(is_open=True, stamp=NOW):
    return {"timestamp": stamp.isoformat(), "is_open": is_open,
            "next_open": "2026-09-16T09:30:00-04:00", "next_close": "2026-09-15T16:00:00-04:00"}


def account(**over):
    base = {"id": "acct-uuid", "status": "ACTIVE", "trading_blocked": False, "account_blocked": False,
            "trade_suspended_by_user": False, "pattern_day_trader": False, "options_trading_level": 2,
            "multiplier": "1", "cash": "1900.00", "non_marginable_buying_power": "1850.00",
            "options_buying_power": "1850.00"}
    return {**base, **over}


def fill(rid, side, qty="1", price="0.90", when=NOW - timedelta(hours=1), symbol=CONTRACT, kind="fill"):
    return {"id": rid, "activity_type": "FILL", "transaction_time": when.isoformat(), "type": kind,
            "price": price, "qty": qty, "side": side, "symbol": symbol, "order_id": "o1", "cum_qty": qty, "leaves_qty": "0"}


def fee(rid, amount="-0.04", day=DAY):
    return {"id": rid, "activity_type": "FEE", "date": day.isoformat(), "net_amount": amount, "description": "OCC"}


class EasternTests(unittest.TestCase):
    def test_dst_boundaries_and_dates(self):
        self.assertEqual(eastern_offset(datetime(2026, 3, 8, 6, 59, tzinfo=timezone.utc)), timedelta(hours=-5))
        self.assertEqual(eastern_offset(datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc)), timedelta(hours=-4))
        self.assertEqual(eastern_offset(datetime(2026, 11, 1, 5, 59, tzinfo=timezone.utc)), timedelta(hours=-4))
        self.assertEqual(eastern_offset(datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)), timedelta(hours=-5))
        # 23:30 ET on the 14th is 03:30Z on the 15th: session date must be the 14th.
        self.assertEqual(eastern_date(datetime(2026, 9, 15, 3, 30, tzinfo=timezone.utc)), date(2026, 9, 14))
        with self.assertRaises(ValueError):
            to_eastern(datetime(2026, 9, 15, 3, 30))
        with self.assertRaises(ValueError):
            to_eastern(datetime(2006, 6, 1, tzinfo=timezone.utc))


class NormalizeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = FillLedger(Path(self.temp.name) / "fills.sqlite3")

    def run_norm(self, records):
        return normalize_activities(records, self.ledger, account_id="acct", recorded_at=NOW)

    def test_buy_add_partial_close_fee_and_idempotent_replay(self):
        records = [fill("f1", "buy", price="0.90"), fill("f2", "buy", when=NOW - timedelta(minutes=50), price="1.00"),
                   fill("f3", "sell", when=NOW - timedelta(minutes=40), price="0.50", kind="partial_fill"),
                   fee("fee1"), {"id": "c1", "activity_type": "CSD", "net_amount": "1000"}]
        first = self.run_norm(records)
        self.assertTrue(first.complete)
        self.assertEqual((first.applied_fills, first.applied_fees, first.ignored), (3, 1, 1))
        inv = self.ledger.inventory()
        self.assertEqual(len(inv), 1)
        self.assertEqual((inv[0]["contract"], inv[0]["quantity"]), (CONTRACT, 1))
        summary = self.ledger.daily_summary(DAY)
        self.assertEqual(summary["realized_loss"], Decimal("40.04"))     # (50-90) + 0.04 fee
        self.assertTrue(summary["breaker_tripped"])
        again = self.run_norm(records)
        self.assertEqual((again.applied_fills, again.applied_fees, again.replayed), (0, 0, 4))
        self.assertTrue(again.complete)

    def test_out_of_order_input_is_sorted_by_time_then_id(self):
        records = [fill("f2", "sell", when=NOW - timedelta(minutes=30)), fill("f1", "buy")]
        self.assertTrue(self.run_norm(records).complete)
        self.assertEqual(self.ledger.inventory(), [])

    def test_sell_without_inventory_blocks_and_halts(self):
        result = self.run_norm([fill("s1", "sell"), fill("b1", "buy", when=NOW - timedelta(minutes=30))])
        self.assertFalse(result.complete)
        self.assertEqual([b["reason"] for b in result.blocked], ["SELL_WITHOUT_INVENTORY", "NOT_APPLIED_AFTER_BLOCK"])
        self.assertEqual(self.ledger.inventory(), [])

    def test_unsupported_types_and_equity_fills_block(self):
        for record in ({"id": "x1", "activity_type": "OPASN", "symbol": CONTRACT},
                       {"id": "x2", "activity_type": "OPEXP"},
                       fill("e1", "buy", symbol="IWM"),
                       fee("f-credit", amount="0.04"),
                       {"id": "u1", "activity_type": "SOMETHING_NEW"}):
            with self.subTest(record=record.get("id")):
                self.assertFalse(self.run_norm([record]).complete)

    def test_new_lifecycle_after_full_close(self):
        self.run_norm([fill("b1", "buy"), fill("s1", "sell", when=NOW - timedelta(minutes=30))])
        result = self.run_norm([fill("b2", "buy", when=NOW - timedelta(minutes=20))])
        self.assertTrue(result.complete)
        inv = self.ledger.inventory()
        self.assertEqual(inv[0]["position_id"], "acct:b2")

    def test_fill_session_uses_eastern_date(self):
        late = datetime(2026, 9, 15, 3, 30, tzinfo=timezone.utc)   # 23:30 ET on the 14th
        self.run_norm([fill("b1", "buy", when=late, price="1.00"), fill("s1", "sell", when=late + timedelta(minutes=1), price="0.10")])
        self.assertEqual(self.ledger.daily_summary(date(2026, 9, 14))["realized_loss"], Decimal("90"))
        self.assertEqual(self.ledger.daily_summary(DAY)["realized_loss"], Decimal("0"))


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = FillLedger(Path(self.temp.name) / "fills.sqlite3")
        self.norm = normalize_activities([fill("b1", "buy")], self.ledger, account_id="acct", recorded_at=NOW)

    def good(self, **over):
        kwargs = dict(
            account=account(), positions=[{"asset_class": "us_option", "symbol": CONTRACT, "qty": "1"}],
            open_orders=[], clock=clock(), ledger_summary=self.ledger.daily_summary(DAY),
            inventory=self.ledger.inventory(), normalization=self.norm,
            history_query={"after": (NOW - timedelta(days=7)).isoformat(), "until": NOW.isoformat()},
            order_attempts=(), reserved_cash=Decimal("0"), pending_local=0, now=NOW,
        )
        kwargs.update(over)
        return reconcile(**kwargs)

    def test_fully_consistent_state_reconciles_and_passes_rules(self):
        result = self.good()
        self.assertEqual(result.reasons, ())
        self.assertTrue(result.state.reconciled)
        self.assertEqual(result.state.trading_day, DAY)
        self.assertEqual(result.state.open_positions, 1)
        self.assertEqual(result.state.settled_cash, Decimal("1850.00"))   # min of cash-like fields
        idea = json.loads((Path(__file__).parent.parent / "examples/long_call.json").read_text())
        idea["symbol"] = "SPY"
        for leg in idea["legs"]:
            leg["symbol"] = "SPY"
        decision = evaluate(idea, result.state, now=NOW, trading_day=DAY, kill_switch=False)
        self.assertTrue(decision["approved"], decision["reason"])

    def test_every_failure_mode_yields_unreconciled_with_reason(self):
        cases = {
            "ACCOUNT_STATUS": dict(account=account(status="SUBMITTED")),
            "trading_blocked": dict(account=account(trading_blocked=True)),
            "pattern_day_trader": dict(account=account(pattern_day_trader=True)),
            "OPTIONS_LEVEL": dict(account=account(options_trading_level=1)),
            "NOT_CASH_ACCOUNT": dict(account=account(multiplier="4")),
            "ACCOUNT_FIELDS_INVALID": dict(account=account(cash="NaN")),
            "NEGATIVE_AVAILABLE_CASH": dict(reserved_cash=Decimal("5000")),
            "POSITION_MISMATCH": dict(positions=[]),
            "SHORT_OR_FRACTIONAL_POSITION": dict(positions=[{"asset_class": "us_option", "symbol": CONTRACT, "qty": "-1"}]),
            "NON_OPTION_POSITION": dict(positions=[{"asset_class": "us_equity", "symbol": "IWM", "qty": "100"}]),
            "UNSUPPORTED_MULTILEG_OPEN_ORDER": dict(open_orders=[{"asset_class": "us_option", "side": "buy", "legs": [{}, {}]}]),
            "NON_OPTION_OPEN_ORDER": dict(open_orders=[{"asset_class": "us_equity", "side": "buy"}]),
            "HISTORY_INCOMPLETE": dict(normalization=None),
            "HISTORY_STALE": dict(history_query={"until": (NOW - timedelta(hours=1)).isoformat()}),
            "HISTORY_WINDOW_UNKNOWN": dict(history_query=None),
            "BROKER_CLOCK_SKEW": dict(clock=clock(stamp=NOW - timedelta(minutes=5))),
            "CLOCK_UNAVAILABLE": dict(clock={}),
            "LEDGER_DAY_MISMATCH": dict(ledger_summary=self.ledger.daily_summary(DAY - timedelta(days=1))),
        }
        for code, over in cases.items():
            with self.subTest(code=code):
                result = self.good(**over)
                self.assertFalse(result.state.reconciled)
                self.assertTrue(any(code in r for r in result.reasons), result.reasons)
                decision = evaluate({}, result.state, now=NOW, trading_day=DAY, kill_switch=False)
                self.assertEqual(decision["reason"].split(":")[0], "STATE_UNAVAILABLE")

    def test_margin_paper_requires_explicit_opt_in(self):
        self.assertFalse(self.good(account=account(multiplier="4")).state.reconciled)
        self.assertTrue(self.good(account=account(multiplier="4"),
                                  config=ReconcileConfig(allow_margin_paper=True)).state.reconciled)

    def test_pending_entries_take_max_of_broker_and_local(self):
        result = self.good(open_orders=[{"asset_class": "us_option", "side": "buy"}], pending_local=0)
        self.assertEqual(result.state.pending_entries, 1)
        self.assertEqual(self.good(pending_local=2).state.pending_entries, 2)
        sells = self.good(open_orders=[{"asset_class": "us_option", "side": "sell"}])
        self.assertEqual(sells.state.pending_entries, 0)
        self.assertTrue(sells.state.reconciled)

    def test_breaker_and_loss_flow_through(self):
        normalize_activities([fill("s1", "sell", price="0.45", when=NOW - timedelta(minutes=30))],
                             self.ledger, account_id="acct", recorded_at=NOW)
        result = self.good(positions=[], inventory=self.ledger.inventory(), ledger_summary=self.ledger.daily_summary(DAY))
        self.assertTrue(result.state.reconciled)
        self.assertEqual(result.state.daily_realized_loss, Decimal("45"))
        self.assertTrue(result.state.breaker_tripped)
        decision = evaluate({}, result.state, now=NOW, trading_day=DAY, kill_switch=False)
        self.assertEqual(decision["reason"].split(":")[0], "CIRCUIT_BREAKER")

    def test_trading_day_pre_market_uses_next_open(self):
        pre = clock(is_open=False, stamp=datetime(2026, 9, 16, 11, 0, tzinfo=timezone.utc))
        self.assertEqual(trading_day_from_clock(pre), date(2026, 9, 16))
        pre["next_open"] = "2026-09-17T09:30:00-04:00"
        self.assertEqual(trading_day_from_clock(pre), date(2026, 9, 17))


class Response(BytesIO):
    def __init__(self, payload, request_id="rid"):
        super().__init__(json.dumps(payload).encode())
        self.code, self.headers = 200, {"X-Request-ID": request_id}


class ScriptedOpener:
    """Routes by path so build_risk_state's call order does not matter."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def open(self, request, timeout):
        path = request.full_url.split("paper-api.alpaca.markets")[1].split("?")[0]
        self.calls.append(path)
        payload = self.routes[path]
        if path == "/v2/account/activities":
            payload = payload.pop(0)
        return Response(payload)


class BuildRiskStateTests(unittest.TestCase):
    def test_end_to_end_read_only_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = FillLedger(Path(tmp) / "fills.sqlite3")
            store = ActivityStore(Path(tmp) / "activities.sqlite3")
            opener = ScriptedOpener({
                "/v2/account": account(), "/v2/clock": clock(),
                "/v2/positions": [{"asset_class": "us_option", "symbol": CONTRACT, "qty": "1"}],
                "/v2/orders": [],
                "/v2/account/activities": [[fill("b1", "buy")], []],
            })
            client = PaperClient(Credentials("k", "s"), TraceStore(Path(tmp) / "t.sqlite3"), opener=opener)
            result = build_risk_state(client, ledger, store, now=NOW)
            self.assertEqual(result.reasons, ())
            self.assertTrue(result.state.reconciled)
            self.assertEqual(ledger.inventory()[0]["contract"], CONTRACT)
            self.assertTrue(all(c.startswith("/v2/") for c in opener.calls))
            self.assertNotIn("/v2/orders", [c for c in opener.calls if "status" in c])  # GET only, open filter
            self.assertNotIn("POST", str(opener.calls))


if __name__ == "__main__":
    unittest.main()
