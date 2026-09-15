from datetime import date, datetime, timezone
from decimal import Decimal
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError, URLError

from alpaca_agents.executor.client import Credentials, ExecutorError, PaperClient, TraceStore, validate_order_body
from alpaca_agents.executor.orders import OrderJournal
from alpaca_agents.executor.submit import submit_claimed
from alpaca_agents.rules import RiskState

NOW = datetime(2026, 9, 15, 14, tzinfo=timezone.utc)
DAY = date(2026, 9, 15)
ROOT = Path(__file__).resolve().parents[1]
BODY = {"symbol": "IWM261016C00205000", "qty": "1", "side": "buy", "type": "limit", "time_in_force": "day",
        "limit_price": "0.90", "position_intent": "buy_to_open", "client_order_id": "paper-" + "a" * 32}


class Response(BytesIO):
    def __init__(self, body=b'{}', code=200, headers=None):
        super().__init__(body)
        self.code, self.headers = code, headers or {"X-Request-ID": "rid-1"}


class Opener:
    def __init__(self, response):
        self.response, self.calls = response, []

    def open(self, request, timeout):
        self.calls.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class OrderBodyTests(unittest.TestCase):
    def test_only_journal_shape_is_accepted(self):
        self.assertEqual(validate_order_body(BODY), BODY)
        bad = [
            {**BODY, "type": "market"}, {**BODY, "time_in_force": "gtc"}, {**BODY, "qty": "0"},
            {**BODY, "limit_price": "0.9"}, {**BODY, "limit_price": "0.00"}, {**BODY, "side": "sell"},
            {**BODY, "client_order_id": "mine"}, {**BODY, "extra": "x"}, {k: v for k, v in BODY.items() if k != "qty"},
            {**BODY, "symbol": "IWM"}, {**BODY, "qty": 1}, [], None,
        ]
        for body in bad:
            with self.subTest(body=body), self.assertRaises(ExecutorError):
                validate_order_body(body)
        sell = {**BODY, "side": "sell", "position_intent": "sell_to_close"}
        self.assertEqual(validate_order_body(sell)["side"], "sell")


class SubmitOrderTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.traces = TraceStore(Path(self.temp.name) / "t.sqlite3")

    def client(self, response):
        self.opener = Opener(response)
        return PaperClient(Credentials("k", "s"), self.traces, opener=self.opener)

    def test_post_is_traced_and_sent_to_paper_host_once(self):
        record = {"id": "o-1", "client_order_id": BODY["client_order_id"], "status": "accepted"}
        client = self.client(Response(json.dumps(record).encode()))
        self.assertEqual(client.submit_order(BODY), record)
        request = self.opener.calls[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertTrue(request.full_url.startswith("https://paper-api.alpaca.markets/v2/orders"))
        self.assertEqual(json.loads(request.data), BODY)
        self.assertEqual(request.get_header("Content-type"), "application/json")
        row = self.traces.recent()[0]
        self.assertEqual((row["method"], row["path"], row["outcome"], row["request_id"]), ("POST", "/v2/orders", "success", "rid-1"))
        self.assertEqual(len(self.opener.calls), 1)

    def test_broker_rejection_carries_status_and_request_id(self):
        err = HTTPError("u", 422, "unprocessable", {"X-Request-ID": "rid-422"}, BytesIO(b'{"message":"bad"}'))
        with self.assertRaises(ExecutorError) as ctx:
            self.client(err).submit_order(BODY)
        self.assertEqual((ctx.exception.status, ctx.exception.request_id), (422, "rid-422"))
        self.assertEqual(len(self.opener.calls), 1)  # no retry

    def test_transport_failure_is_unknown_outcome(self):
        with self.assertRaises(ExecutorError) as ctx:
            self.client(URLError("timeout")).submit_order(BODY)
        self.assertIsNone(ctx.exception.status)
        self.assertIsNone(ctx.exception.request_id)
        self.assertEqual(self.traces.recent()[0]["outcome"], "transport_error")

    def test_invalid_body_never_reaches_network(self):
        client = self.client(Response())
        with self.assertRaises(ExecutorError):
            client.submit_order({**BODY, "type": "market"})
        self.assertEqual(self.opener.calls, [])
        self.assertEqual(self.traces.recent(), [])


class SubmitClaimedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "control").write_text("ARMED_PAPER")
        self.journal = OrderJournal(root / "o.sqlite3", account_id="acct", control_file=root / "control")
        self.traces = TraceStore(root / "t.sqlite3")
        idea = json.loads((ROOT / "examples/long_call.json").read_text())
        state = RiskState(DAY, NOW, 0, 0, Decimal(0), Decimal(2000), (), reconciled=True)
        reserved = self.journal.reserve("k1", idea, state_provider=lambda: state, now=NOW, trading_day=DAY)
        self.claimed = self.journal.claim(reserved["authorization_id"], state_provider=lambda: state, now=NOW, trading_day=DAY)
        self.aid = reserved["authorization_id"]
        self.cid = "paper-" + self.aid

    def client(self, response):
        self.opener = Opener(response)
        return PaperClient(Credentials("k", "s"), self.traces, opener=self.opener)

    def accepted(self, status="accepted"):
        return Response(json.dumps({"id": "bo-1", "client_order_id": self.cid, "status": status}).encode())

    def test_submit_flag_defaults_off(self):
        client = self.client(self.accepted())
        result = submit_claimed(client, self.journal, self.claimed, now=NOW)
        self.assertEqual(result["outcome"], "not_submitted")
        self.assertEqual(self.opener.calls, [])
        self.assertEqual(submit_claimed(client, self.journal, self.claimed, now=NOW, submit="yes")["outcome"], "not_submitted")

    def test_accepted_marks_submitted_and_cannot_send_twice(self):
        client = self.client(self.accepted())
        result = submit_claimed(client, self.journal, self.claimed, now=NOW, submit=True)
        self.assertEqual(result, {"outcome": "submitted", "broker_order_id": "bo-1", "broker_status": "accepted"})
        intent = self.journal.live_intents()[0]
        self.assertEqual((intent["status"], intent["broker_order_id"]), ("claimed", "bo-1"))
        again = submit_claimed(client, self.journal, self.claimed, now=NOW, submit=True)
        self.assertEqual(again["outcome"], "not_submitted")
        self.assertEqual(len(self.opener.calls), 1)
        self.assertEqual(self.journal.events()[0]["event"], "order_submitted")

    def test_broker_rejection_releases_slot(self):
        err = HTTPError("u", 403, "forbidden", {"X-Request-ID": "rid-403"}, BytesIO(b'{}'))
        result = submit_claimed(self.client(err), self.journal, self.claimed, now=NOW, submit=True)
        self.assertEqual((result["outcome"], result["status"]), ("unplaced", 403))
        self.assertEqual(self.journal.live_intents(), [])
        self.assertEqual(self.journal.events()[0]["event"], "intent_unplaced")

    def test_rejection_without_request_id_stays_claimed(self):
        err = HTTPError("u", 422, "unprocessable", {}, BytesIO(b'{}'))
        result = submit_claimed(self.client(err), self.journal, self.claimed, now=NOW, submit=True)
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual(self.journal.live_intents()[0]["status"], "claimed")

    def test_timeout_and_5xx_stay_claimed_for_reconciliation(self):
        for failure in (URLError("timeout"), HTTPError("u", 500, "err", {"X-Request-ID": "rid"}, BytesIO(b'{}'))):
            with self.subTest(failure=failure):
                result = submit_claimed(self.client(failure), self.journal, self.claimed, now=NOW, submit=True)
                self.assertEqual(result["outcome"], "unknown")
                intent = self.journal.live_intents()[0]
                self.assertEqual((intent["status"], intent["broker_order_id"]), ("claimed", None))
        self.assertEqual(self.journal.events()[0]["event"], "order_submit_unknown")

    def test_mismatched_broker_echo_is_unknown(self):
        wrong = Response(json.dumps({"id": "bo-2", "client_order_id": "paper-" + "f" * 32, "status": "accepted"}).encode())
        result = submit_claimed(self.client(wrong), self.journal, self.claimed, now=NOW, submit=True)
        self.assertEqual(result["reason"], "BROKER_RESPONSE_MISMATCH")
        self.assertIsNone(self.journal.live_intents()[0]["broker_order_id"])

    def test_forged_or_tampered_claim_is_refused(self):
        client = self.client(self.accepted())
        forged = {**self.claimed, "prepared_order": {**self.claimed["prepared_order"], "qty": "5"}}
        self.assertTrue(submit_claimed(client, self.journal, forged, now=NOW, submit=True)["reason"].startswith("BODY_MISMATCH"))
        swapped = {**self.claimed, "authorization_id": "b" * 32}
        self.assertTrue(submit_claimed(client, self.journal, swapped, now=NOW, submit=True)["reason"].startswith("INTENT_NOT_SENDABLE"))
        unapproved = {**self.claimed, "approved": False}
        self.assertEqual(submit_claimed(client, self.journal, unapproved, now=NOW, submit=True)["reason"], "NOT_APPROVED")
        self.assertEqual(self.opener.calls, [])


if __name__ == "__main__":
    unittest.main()
