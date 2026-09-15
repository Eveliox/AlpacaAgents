"""Paper transport with durable, sanitized request tracing.

GET account/positions/clock/orders/activities, plus ONE write: POST /v2/orders
for a body the order journal prepared. No retries, redirects, live URL override,
or response-body logs. Run only in the executor process; never give scanner
processes broker credentials.

A POST whose outcome is not a parsed broker response is UNKNOWN, not failed:
the order may exist. The caller must leave the journal intent claimed and let
reconciliation match it by client_order_id.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import urllib.error
import urllib.request
from urllib.parse import urlencode
from uuid import uuid4

PAPER_URL = "https://paper-api.alpaca.markets"
MAX_BODY_BYTES = 2 * 1024 * 1024


class ExecutorError(RuntimeError):
    """Sanitized error safe to display; raw HTTP exceptions must not be logged."""

    def __init__(self, message: str, *, local_id=None, request_id=None, status=None):
        super().__init__(message)
        self.local_id = local_id
        self.request_id = request_id
        self.status = status


@dataclass(frozen=True)
class ActivityPage:
    records: list
    local_id: str
    request_id: str | None


def activity_query(*, after: datetime, until: datetime, page_token: str | None = None) -> dict:
    for value in (after, until):
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ExecutorError("Activity bounds must be timezone-aware timestamps")
    if after >= until:
        raise ExecutorError("Activity after must precede until")
    if page_token is not None and (not isinstance(page_token, str)
            or not re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", page_token)):
        raise ExecutorError("Invalid activity pagination token")
    query = {"after": after.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
             "until": until.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
             "direction": "asc", "page_size": "100"}
    if page_token is not None:
        query["page_token"] = page_token
    return query


@dataclass(frozen=True)
class Credentials:
    key: str = field(repr=False)
    secret: str = field(repr=False)

    @classmethod
    def from_environment(cls):
        key = os.environ.get("ALPACA_PAPER_API_KEY", "")
        secret = os.environ.get("ALPACA_PAPER_API_SECRET", "")
        if not key or not secret or any(c.isspace() for c in key + secret):
            raise ExecutorError("Valid paper credentials required in executor environment")
        return cls(key, secret)


ORDER_BODY = {
    "symbol": r"[A-Z]{1,6}[0-9]{6}[CP][0-9]{8}",
    "qty": r"[1-9][0-9]{0,3}",
    "side": r"buy|sell",
    "type": r"limit",
    "time_in_force": r"day",
    "limit_price": r"[0-9]{1,5}\.[0-9]{2}",
    "position_intent": r"buy_to_open|sell_to_close",
    "client_order_id": r"paper-[a-f0-9]{32}",
}


def validate_order_body(body) -> dict:
    """Exactly the single-leg limit shape the journal prepares; anything else is refused."""
    if not isinstance(body, dict) or set(body) != set(ORDER_BODY):
        raise ExecutorError("Order body must contain exactly the journal-prepared fields")
    for key, pattern in ORDER_BODY.items():
        value = body[key]
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            raise ExecutorError(f"Order body field rejected: {key}")
    if (body["side"], body["position_intent"]) not in (("buy", "buy_to_open"), ("sell", "sell_to_close")):
        raise ExecutorError("Order side and position intent disagree")
    if body["limit_price"] == "0.00":
        raise ExecutorError("Zero limit price")
    return dict(body)


class TraceStore:
    """Durable SQLite request journal, separate from order/risk authorization.

    Start record is committed before network I/O. An unfinished record means the
    process crashed or could not persist its outcome; it is NOT proof of failure.
    No bodies, credential headers, or arbitrary URLs are stored.
    """

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS api_requests (
                local_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                method TEXT NOT NULL,
                path TEXT NOT NULL,
                status INTEGER,
                request_id TEXT,
                outcome TEXT NOT NULL
            )""")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def begin(self, path: str, method: str = "GET") -> str:
        if method not in ("GET", "POST"):
            raise ExecutorError("Unsupported trace method")
        local_id = uuid4().hex
        with self._connect() as db:
            db.execute("INSERT INTO api_requests VALUES (?, ?, NULL, ?, ?, NULL, NULL, 'started')",
                       (local_id, datetime.now(timezone.utc).isoformat(), method, path))
        return local_id

    def finish(self, local_id: str, status: int | None, request_id: str | None, outcome: str):
        with self._connect() as db:
            changed = db.execute("""UPDATE api_requests SET completed_at=?, status=?, request_id=?, outcome=?
                                    WHERE local_id=? AND outcome='started'""",
                                 (datetime.now(timezone.utc).isoformat(), status, request_id, outcome, local_id))
            if changed.rowcount != 1:
                raise ExecutorError("Request trace missing or already completed")

    def recent(self, limit: int = 20) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(
                "SELECT * FROM api_requests ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,))]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward broker credentials to a redirect destination.
        return None


def _request_id(headers) -> str | None:
    if headers is None:
        return None
    # Header names are case-insensitive, including with injected test transports.
    value = next((v for k, v in headers.items() if k.lower() == "x-request-id"), None)
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", value):
        return None
    return value


class PaperClient:
    """Read endpoints plus journal-prepared order submission against the PAPER host only.

    Inject opener only in tests. Default transport disables environment proxies
    and redirects and uses Python's default verified TLS context.
    """

    def __init__(self, credentials: Credentials, traces: TraceStore, *, opener=None):
        self._credentials = credentials
        self._traces = traces
        self._opener = opener if opener is not None else urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect())

    def account(self) -> dict:
        return self._get("/v2/account", dict)

    def positions(self) -> list:
        return self._get("/v2/positions", list)

    def clock(self) -> dict:
        return self._get("/v2/clock", dict)

    def open_orders(self) -> list:
        return self._get("/v2/orders", list, query={"status": "open", "limit": "500", "nested": "true"})

    def order_by_client_id(self, client_order_id: str) -> dict | None:
        """Order record for one of OUR client ids, or None when the broker has no such order (404)."""
        if not re.fullmatch(r"paper-[a-f0-9]{32}", client_order_id or ""):
            raise ExecutorError("Only journal-issued client order ids may be looked up")
        try:
            return self._get("/v2/orders:by_client_order_id", dict, query={"client_order_id": client_order_id})
        except ExecutorError as exc:
            if "status=404" in str(exc):
                return None
            raise

    def activities_page(self, *, after: datetime, until: datetime,
                        page_token: str | None = None) -> ActivityPage:
        query = activity_query(after=after, until=until, page_token=page_token)
        return self._get("/v2/account/activities", list, query=query, include_trace=True)

    def submit_order(self, body: dict) -> dict:
        """POST one journal-prepared order. Raises ExecutorError on ANY non-2xx or unknown outcome.

        The raised error carries local_id/request_id/status so the caller can
        distinguish a broker-answered rejection (definitely not placed) from an
        unknown outcome (possibly placed). Never retried here or anywhere.
        """
        return self._call("POST", "/v2/orders", dict, body=validate_order_body(body))

    def _get(self, path: str, expected_type, *, query=None, include_trace=False):
        return self._call("GET", path, expected_type, query=query, include_trace=include_trace)

    def _call(self, method: str, path: str, expected_type, *, query=None, body=None, include_trace=False):
        if path not in ("/v2/account", "/v2/positions", "/v2/account/activities", "/v2/clock", "/v2/orders",
                        "/v2/orders:by_client_order_id"):
            raise ExecutorError("Endpoint not permitted by paper client")
        if method == "GET" and path == "/v2/orders" and (query or {}).get("status") != "open":
            raise ExecutorError("Only open-order listing is permitted")
        if method == "POST" and (path != "/v2/orders" or query is not None or body is None):
            raise ExecutorError("Only order submission may POST")
        if path == "/v2/account/activities" and query is None:
            raise ExecutorError("Bounded activity query required")
        suffix = "?" + urlencode(query) if query else ""
        local_id = self._traces.begin(path, method)  # Failure here prevents broker access.
        status, request_id, outcome = None, None, "transport_error"
        payload = None
        response = None
        try:
            headers = {"APCA-API-KEY-ID": self._credentials.key,
                       "APCA-API-SECRET-KEY": self._credentials.secret,
                       "Accept": "application/json"}
            data = None
            if body is not None:
                headers["Content-Type"] = "application/json"
                data = json.dumps(body, allow_nan=False, separators=(",", ":")).encode()
            request = urllib.request.Request(PAPER_URL + path + suffix, data=data, method=method, headers=headers)
            try:
                response = self._opener.open(request, timeout=15)
            except urllib.error.HTTPError as exc:
                response = exc  # HTTP errors still have valuable response headers.
            status = response.code
            request_id = _request_id(response.headers)
            if not 200 <= status < 300:
                outcome = "http_error"
            else:
                # Capture headers before body reads: truncated/error bodies must
                # not lose the broker's support request identifier.
                outcome = "response_read_error"
                body = response.read(MAX_BODY_BYTES + 1)
                outcome = "invalid_response"
                if len(body) <= MAX_BODY_BYTES:
                    def invalid_constant(value):
                        raise ValueError("Nonstandard JSON constant")
                    payload = json.loads(body, parse_constant=invalid_constant)
                    if isinstance(payload, expected_type):
                        outcome = "success"
        except Exception:
            # Never propagate raw exceptions: they may contain credentials or
            # sensitive broker response bodies. No automatic retry.
            pass
        finally:
            try:
                if response is not None:
                    response.close()
            except Exception:
                outcome = "response_close_error"
            finally:
                # If persistence fails, do not return data as successful.
                self._traces.finish(local_id, status, request_id, outcome)
        if outcome != "success":
            raise ExecutorError(f"Paper API {outcome}; status={status}; "
                                f"request_id={request_id or 'unavailable'}; local_id={local_id}",
                                local_id=local_id, request_id=request_id, status=status) from None
        if include_trace:
            return ActivityPage(payload, local_id, request_id)
        return payload
