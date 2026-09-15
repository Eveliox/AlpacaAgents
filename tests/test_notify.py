from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest

from alpaca_agents.notify import Notifier

NOW = datetime(2026, 9, 15, 14, tzinfo=timezone.utc)


class Response(BytesIO):
    def __init__(self, code=200):
        super().__init__(b"")
        self.code = code

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Opener:
    def __init__(self, response):
        self.response, self.calls = response, []

    def open(self, request, timeout):
        self.calls.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class NotifierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "n.jsonl"

    def test_jsonl_is_written_without_webhook(self):
        n = Notifier(self.path)
        event = n.send("info", "hello", {"a": 1}, now=NOW)
        self.assertIsNone(event["delivered"])
        line = json.loads(self.path.read_text().splitlines()[0])
        self.assertEqual((line["level"], line["title"], line["payload"]), ("info", "hello", {"a": 1}))

    def test_webhook_best_effort(self):
        opener = Opener(Response())
        n = Notifier(self.path, webhook_url="https://hooks.example/abc", opener=opener)
        self.assertTrue(n.send("warning", "w", now=NOW)["delivered"])
        self.assertEqual(json.loads(opener.calls[0].data)["title"], "w")
        failing = Notifier(self.path, webhook_url="https://hooks.example/abc", opener=Opener(RuntimeError("down")))
        self.assertFalse(failing.send("incident", "i", now=NOW)["delivered"])
        self.assertEqual(len(self.path.read_text().splitlines()), 2)   # persisted regardless

    def test_validation(self):
        with self.assertRaises(ValueError):
            Notifier(self.path, webhook_url="http://insecure.example/x")
        n = Notifier(self.path)
        with self.assertRaises(ValueError):
            n.send("debug", "x")
        with self.assertRaises(ValueError):
            n.send("info", "")


if __name__ == "__main__":
    unittest.main()
