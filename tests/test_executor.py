from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
import urllib.request

from alpaca_agents.executor.client import (
    Credentials, ExecutorError, MAX_BODY_BYTES, PAPER_URL, PaperClient, TraceStore, _NoRedirect,
)


class Response(BytesIO):
    def __init__(self, body=b'{}', code=200, headers=None):
        super().__init__(body)
        self.code = code
        self.headers = headers or {}


class FakeOpener:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "requests.sqlite3"
        self.store = TraceStore(self.path)
        self.credentials = Credentials("test-key", "test-secret")

    def client(self, response):
        self.opener = FakeOpener(response)
        return PaperClient(self.credentials, self.store, opener=self.opener)

    def latest(self):
        # Reopen the journal to prove persistence across client instances.
        return TraceStore(self.path).recent()[0]

    def test_success_and_headers(self):
        client = self.client(Response(b'{"equity":"2000"}', headers={"x-request-id": "request-123"}))
        self.assertEqual(client.account(), {"equity": "2000"})
        row = self.latest()
        self.assertEqual((row["request_id"], row["status"], row["outcome"]), ("request-123", 200, "success"))
        request, timeout = self.opener.calls[0]
        self.assertEqual(request.full_url, PAPER_URL + "/v2/account")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(timeout, 15)
        self.assertEqual(request.get_header("Apca-api-key-id"), "test-key")
        self.assertNotIn("test-secret", json.dumps(row))
        self.assertNotIn("test-key", repr(self.credentials))

    def test_http_errors_persist_ids_without_retry(self):
        for status in (301, 403, 429, 500):
            with self.subTest(status=status):
                error = HTTPError(PAPER_URL + "/v2/account", status, "test-secret",
                                  {"X-Request-ID": f"req-{status}"}, BytesIO(b'test-secret'))
                client = self.client(error)
                with self.assertRaises(ExecutorError) as caught:
                    client.account()
                self.assertNotIn("test-secret", str(caught.exception))
                self.assertIn(f"req-{status}", str(caught.exception))
                self.assertEqual(len(self.opener.calls), 1)
                self.assertEqual(self.latest()["outcome"], "http_error")

    def test_transport_failure_has_no_request_id(self):
        client = self.client(URLError("test-secret"))
        with self.assertRaises(ExecutorError) as caught:
            client.account()
        self.assertNotIn("test-secret", str(caught.exception))
        row = self.latest()
        self.assertIsNone(row["status"])
        self.assertIsNone(row["request_id"])
        self.assertEqual(row["outcome"], "transport_error")
        self.assertEqual(len(self.opener.calls), 1)

    def test_bad_body_still_preserves_request_id(self):
        for body in (b'not json test-secret', b'[]', b'{"x":NaN}', b'x' * (MAX_BODY_BYTES + 1)):
            client = self.client(Response(body, headers={"X-Request-ID": "body-error"}))
            with self.assertRaises(ExecutorError):
                client.account()
            self.assertEqual(self.latest()["request_id"], "body-error")
            self.assertEqual(self.latest()["outcome"], "invalid_response")

    def test_body_read_failure_still_preserves_request_id(self):
        response = Response(headers={"X-Request-ID": "read-error"})
        with patch.object(response, "read", side_effect=OSError("test-secret")):
            with self.assertRaises(ExecutorError):
                self.client(response).account()
        self.assertEqual(self.latest()["request_id"], "read-error")
        self.assertEqual(self.latest()["outcome"], "response_read_error")

    def test_missing_and_invalid_request_ids(self):
        for value in (None, "bad\nheader", "x" * 257):
            self.client(Response(headers={"X-Request-ID": value})).account()
            self.assertIsNone(self.latest()["request_id"])

    def test_positions_and_endpoint_allowlist(self):
        client = self.client(Response(b'[]'))
        self.assertEqual(client.positions(), [])
        with self.assertRaises(ExecutorError):
            client._get("/v2/orders", list)
        self.assertEqual(len(self.opener.calls), 1)

    def test_journal_failure_prevents_access_or_success(self):
        client = self.client(Response())
        with patch.object(self.store, "begin", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                client.account()
        self.assertEqual(self.opener.calls, [])
        with patch.object(self.store, "finish", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                client.account()
        self.assertEqual(self.latest()["outcome"], "started")

    def test_redirect_policy_does_not_forward_request(self):
        request = urllib.request.Request(PAPER_URL + "/v2/account")
        self.assertIsNone(_NoRedirect().redirect_request(
            request, None, 302, "Found", {}, "https://example.com"))

    def test_environment_explicitly_paper_only(self):
        with patch.dict("os.environ", {"APCA_API_KEY_ID": "ignored-live-key"}, clear=True):
            with self.assertRaises(ExecutorError):
                Credentials.from_environment()
        with patch.dict("os.environ", {"ALPACA_PAPER_API_KEY": "paper-key",
                                       "ALPACA_PAPER_API_SECRET": "paper-secret"}, clear=True):
            self.assertEqual(Credentials.from_environment().key, "paper-key")


if __name__ == "__main__":
    unittest.main()
