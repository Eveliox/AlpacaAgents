from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from alpaca_agents.executor.client import Credentials, ExecutorError, PaperClient, TraceStore
from alpaca_agents.executor.history import ActivityStore, HistoryError, import_activities

AFTER = datetime(2026, 9, 1, tzinfo=timezone.utc)
UNTIL = datetime(2026, 9, 15, tzinfo=timezone.utc)
NOW = UNTIL + timedelta(days=1)


def activity(identifier, kind="FILL"):
    return {"id": identifier, "activity_type": kind, "qty": "1", "price": "0.90"}


class Response(BytesIO):
    def __init__(self, payload, request_id):
        super().__init__(json.dumps(payload).encode())
        self.code = 200
        self.headers = {"X-Request-ID": request_id}


class SequenceOpener:
    def __init__(self, payloads):
        self.payloads = iter(payloads)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append(request)
        item = next(self.payloads)
        if isinstance(item, Exception):
            raise item
        return Response(item, f"request-{len(self.calls)}")


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "activities.sqlite3"
        self.store = ActivityStore(self.path)
        self.traces = TraceStore(Path(self.temp.name) / "traces.sqlite3")

    def client(self, pages, account_id="account-1"):
        self.opener = SequenceOpener([{"id": account_id}, *pages])
        return PaperClient(Credentials("fake-key", "fake-secret"), self.traces, opener=self.opener)

    def run_import(self, pages, **kwargs):
        return import_activities(self.client(pages), self.store, after=AFTER, until=UNTIL, now=NOW, **kwargs)

    def rows(self, sql):
        db = sqlite3.connect(self.path)
        try:
            return db.execute(sql).fetchall()
        finally:
            db.close()

    def latest(self):
        run_id = self.rows("SELECT run_id FROM history_runs ORDER BY rowid DESC LIMIT 1")[0][0]
        return ActivityStore(self.path).report(run_id)

    def test_multiple_short_pages_and_empty_terminal_page(self):
        result = self.run_import([[activity("id-1")], [activity("id-2", "FEE")], []])
        self.assertEqual((result["status"], result["pages"], result["records"]), ("exhausted", 3, 2))
        self.assertFalse(result["reconciled"])
        self.assertEqual(len(self.opener.calls), 4)  # Identity + 3 pages.
        query = parse_qs(urlsplit(self.opener.calls[2].full_url).query)
        self.assertEqual(query["page_token"], ["id-1"])
        self.assertEqual(query["direction"], ["asc"])
        self.assertEqual(query["page_size"], ["100"])
        self.assertNotIn("activity_types", query)  # Don't hide fees/assignments.
        pages = self.rows("SELECT local_id,request_id,payload FROM history_pages ORDER BY page_number")
        self.assertEqual(pages[0][1], "request-2")
        self.assertEqual(json.loads(pages[1][2])[0]["activity_type"], "FEE")
        self.assertEqual(self.latest(), result)
        self.assertEqual(self.traces.recent()[0]["path"], "/v2/account/activities")
        self.assertNotIn("fake-secret", str(pages))

    def test_empty_history_still_unreconciled(self):
        result = self.run_import([[]])
        self.assertEqual(result["status"], "exhausted")
        self.assertEqual(result["records"], 0)
        self.assertFalse(result["reconciled"])

    def test_repeated_cursor_fails_without_looping(self):
        with self.assertRaises(HistoryError):
            self.run_import([[activity("id-1")], [activity("id-1")]])
        report = self.latest()
        self.assertEqual((report["status"], report["pages"], report["records"]), ("failed", 1, 1))
        self.assertEqual(report["error_request_id"], "request-3")
        self.assertEqual(len(self.opener.calls), 3)

    def test_page_cap_never_claims_complete(self):
        with self.assertRaises(HistoryError):
            self.run_import([[activity("id-1")]], max_pages=1)
        self.assertEqual(self.latest()["failure_code"], "PAGE_LIMIT")
        self.assertEqual(self.latest()["status"], "failed")

    def test_http_error_preserves_partial_run_and_request_id_without_retry(self):
        failure = HTTPError("https://paper-api.alpaca.markets/v2/account/activities", 429, "fake-secret",
                            {"X-Request-ID": "failed-request"}, BytesIO(b'fake-secret'))
        with self.assertRaises(HistoryError) as caught:
            self.run_import([[activity("id-1")], failure])
        self.assertNotIn("fake-secret", str(caught.exception))
        self.assertEqual(self.latest()["error_request_id"], "failed-request")
        self.assertEqual(self.latest()["pages"], 1)
        self.assertEqual(len(self.opener.calls), 3)

    def test_malformed_page_rolls_back_all_records(self):
        for page in ([activity("id-1"), {}], [activity("same"), activity("same")],
                     [{"id": "bad\ntoken", "activity_type": "FILL"}], [activity(str(i)) for i in range(101)]):
            with self.subTest(page_length=len(page)), self.assertRaises(HistoryError):
                self.run_import([page])
            self.assertEqual(self.latest()["records"], 0)
            self.assertEqual(self.latest()["status"], "failed")

    def test_unknown_activity_types_preserved_not_normalized(self):
        result = self.run_import([[activity("unknown", "FUTURE_BROKER_TYPE")], []])
        payload = json.loads(self.rows("SELECT payload FROM history_pages ORDER BY page_number")[0][0])
        self.assertEqual(payload[0]["activity_type"], "FUTURE_BROKER_TYPE")
        self.assertFalse(result["reconciled"])

    def test_account_mismatch_blocks_activity_requests(self):
        self.run_import([[]])
        client = self.client([[]], account_id="different-account")
        with self.assertRaises(HistoryError):
            import_activities(client, self.store, after=AFTER, until=UNTIL, now=NOW)
        self.assertEqual(len(self.opener.calls), 1)
        self.assertEqual(len(self.rows("SELECT run_id FROM history_runs")), 1)

    def test_explicit_rerun_keeps_separate_evidence_for_changed_activity(self):
        first = self.run_import([[activity("id-1")], []])
        second = self.run_import([[{**activity("id-1"), "price": "0.80"}], []])
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(len(self.rows("SELECT * FROM history_ids")), 2)
        self.assertFalse(second["reconciled"])

    def test_invalid_bounds_and_limits_prevent_network(self):
        for change in ({"after": UNTIL}, {"after": AFTER.replace(tzinfo=None)},
                       {"until": NOW + timedelta(seconds=1)}, {"max_pages": 0}, {"max_pages": True}):
            client = self.client([])
            with self.subTest(change=change), self.assertRaises(ExecutorError):
                import_activities(client, self.store, **{ "after": AFTER, "until": UNTIL, "now": NOW, **change})
            self.assertEqual(self.opener.calls, [])

    def test_page_write_failure_stops_before_next_network_call(self):
        client = self.client([[activity("id-1")], []])
        with patch.object(self.store, "append", side_effect=OSError("sensitive raw message")):
            with self.assertRaises(HistoryError) as caught:
                import_activities(client, self.store, after=AFTER, until=UNTIL, now=NOW)
        self.assertNotIn("sensitive raw message", str(caught.exception))
        self.assertEqual(len(self.opener.calls), 2)
        self.assertEqual(self.latest()["status"], "failed")

    def test_crash_leaves_started_run_not_success(self):
        client = self.client([[activity("id-1")]])
        with patch.object(self.store, "append", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                import_activities(client, self.store, after=AFTER, until=UNTIL, now=NOW)
        self.assertEqual(self.latest()["status"], "started")
        self.assertFalse(self.latest()["reconciled"])

    def test_direct_page_token_validation_and_encoding(self):
        opener = SequenceOpener([[]])
        client = PaperClient(Credentials("key", "secret"), self.traces, opener=opener)
        page = client.activities_page(after=AFTER, until=UNTIL, page_token="20260101::abc-def")
        self.assertEqual(parse_qs(urlsplit(opener.calls[0].full_url).query)["page_token"], ["20260101::abc-def"])
        self.assertEqual(page.request_id, "request-1")
        with self.assertRaises(ExecutorError):
            client.activities_page(after=AFTER, until=UNTIL, page_token="bad&until=tomorrow")
        self.assertEqual(len(opener.calls), 1)


if __name__ == "__main__":
    unittest.main()
