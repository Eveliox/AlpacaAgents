"""Operator notifications: durable JSONL first, optional HTTPS webhook second.

The JSONL file is the source of truth; the webhook is best-effort and its
failure is recorded, never raised into the trading loop. No secrets, broker
payloads or credentials are ever included in a notification.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import urllib.request
from urllib.parse import urlsplit

LEVELS = ("info", "warning", "incident")


class Notifier:
    def __init__(self, path: Path, *, webhook_url: str | None = None, opener=None):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        if webhook_url is not None:
            parts = urlsplit(webhook_url)
            if parts.scheme != "https" or not parts.netloc or parts.fragment:
                raise ValueError("Webhook must be an https URL")
        self.webhook_url = webhook_url
        self._opener = opener if opener is not None else urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def from_environment(cls, path: Path):
        url = os.environ.get("ALPACA_AGENT_WEBHOOK_URL") or None
        return cls(path, webhook_url=url)

    def send(self, level: str, title: str, payload: dict | None = None, *, now: datetime | None = None) -> dict:
        if level not in LEVELS:
            raise ValueError("Unknown notification level")
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise ValueError("Title required")
        stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        event = {"timestamp": stamp, "level": level, "title": title, "payload": payload or {}}
        line = json.dumps(event, allow_nan=False, sort_keys=True, default=str)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        event["delivered"] = None
        if self.webhook_url:
            event["delivered"] = self._post(line)
        return event

    def _post(self, line: str) -> bool:
        request = urllib.request.Request(self.webhook_url, data=line.encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with self._opener.open(request, timeout=10) as response:
                return 200 <= response.code < 300
        except Exception:
            return False
