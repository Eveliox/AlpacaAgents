"""Polygon/Massive GET-only market data. Never imports the broker executor.

Current documentation uses api.massive.com. No URL override, redirects, retries,
proxy environment, or key-in-query authentication. TLS verification stays on.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
from uuid import uuid4

BASE_URL = "https://api.massive.com"
BODY_LIMIT = 4 * 1024 * 1024


class MarketDataError(RuntimeError):
    """Only sanitized messages, never provider response bodies or credentials."""


def symbol_checked(symbol: str) -> str:
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z]{1,6}", symbol):
        raise MarketDataError("Unsupported underlying symbol")
    return symbol


@dataclass(frozen=True)
class DataCredentials:
    key: str = field(repr=False)

    @classmethod
    def from_environment(cls):
        key = os.environ.get("MASSIVE_API_KEY") or os.environ.get("POLYGON_API_KEY", "")
        if not key or any(c.isspace() for c in key):
            raise MarketDataError("Market-data key required in MASSIVE_API_KEY or POLYGON_API_KEY")
        return cls(key)


class JsonlAudit:
    """Single-process durable diagnostics. Failure prevents data release."""
    def __init__(self, path: Path):
        self.path = path

    def __call__(self, event: dict):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, allow_nan=False, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _safe_id(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", value) else None


class MarketDataClient:
    def __init__(self, credentials: DataCredentials, *, audit, opener=None):
        self._credentials = credentials
        self._audit = audit
        self._opener = opener if opener is not None else build_opener(ProxyHandler({}), _NoRedirect())

    def _get(self, path: str, query: dict) -> dict:
        if not (re.fullmatch(r"/v3/snapshot/options/[A-Z]{1,6}", path)
                or re.fullmatch(r"/v2/aggs/ticker/[A-Z]{1,6}/range/1/day/\d{4}-\d{2}-\d{2}/\d{4}-\d{2}-\d{2}", path)):
            raise MarketDataError("Market-data endpoint not allowed")
        if any(k.lower() in ("apikey", "api_key", "authorization") for k in query):
            raise MarketDataError("Credentials in query are forbidden")
        local_id = uuid4().hex
        common = {"provider": "massive", "local_id": local_id, "method": "GET", "path": path}
        self._audit({**common, "event": "request_started", "timestamp": datetime.now(timezone.utc).isoformat()})
        response, payload, status, request_id = None, None, None, None
        outcome = "transport_error"
        try:
            request = Request(BASE_URL + path + "?" + urlencode(query), method="GET", headers={
                "Authorization": "Bearer " + self._credentials.key, "Accept": "application/json"})
            try:
                response = self._opener.open(request, timeout=15)
            except HTTPError as exc:
                response = exc
            status = response.code
            request_id = _safe_id(response.headers.get("X-Request-ID"))
            outcome = "response_read_error"
            body = response.read(BODY_LIMIT + 1)
            outcome = "invalid_response"
            if len(body) > BODY_LIMIT:
                raise ValueError("oversize")
            def invalid_constant(value):
                raise ValueError("Nonstandard JSON constant")
            payload = json.loads(body, parse_constant=invalid_constant)
            if not isinstance(payload, dict):
                raise ValueError("object required")
            request_id = _safe_id(payload.get("request_id")) or request_id
            if not 200 <= status < 300:
                outcome = "http_error"
            elif payload.get("status") != "OK":
                outcome = "provider_error"
            else:
                outcome = "success"
        except Exception:
            pass  # Never propagate provider/raw exceptions containing secrets.
        finally:
            try:
                if response is not None:
                    response.close()
            except Exception:
                outcome = "response_close_error"
            finally:
                self._audit({**common, "event": "request_completed", "status": status,
                             "request_id": request_id, "outcome": outcome,
                             "timestamp": datetime.now(timezone.utc).isoformat()})
        if outcome != "success":
            raise MarketDataError(f"Market data {outcome}; status={status}; local_id={local_id}; "
                                  f"request_id={request_id or 'unavailable'}") from None
        return payload

    def daily_bars(self, symbol: str, *, start: date, end: date) -> dict:
        symbol_checked(symbol)
        if type(start) is not date or type(end) is not date or not 0 <= (end - start).days <= 730:
            raise MarketDataError("Daily-bar window must be ordered and at most 730 days")
        path = f"/v2/aggs/ticker/{symbol}/range/1/day/{start}/{end}"
        payload = self._get(path, {"adjusted": "true", "sort": "asc", "limit": "50000"})
        if payload.get("next_url"):
            # This bounded daily request should fit in one page. Never pretend
            # a truncated series is a complete indicator history.
            raise MarketDataError("Unexpected paginated daily bars; history incomplete")
        return payload

    def option_chain(self, symbol: str, *, expiration_min: date, expiration_max: date,
                     max_pages: int = 20) -> tuple[dict, ...]:
        symbol_checked(symbol)
        if (type(expiration_min) is not date or type(expiration_max) is not date
                or expiration_min > expiration_max or type(max_pages) is not int or not 1 <= max_pages <= 100):
            raise MarketDataError("Invalid option-chain bounds or page cap")
        path = f"/v3/snapshot/options/{symbol}"
        query = {"expiration_date.gte": str(expiration_min), "expiration_date.lte": str(expiration_max),
                 "limit": "250", "sort": "ticker", "order": "asc"}
        pages, seen_urls, seen_contracts = [], set(), set()
        for _ in range(max_pages):
            canonical = urlencode(sorted(query.items()))
            if canonical in seen_urls:
                raise MarketDataError("Repeated option-chain cursor")
            seen_urls.add(canonical)
            payload = self._get(path, query)
            records = payload.get("results")
            if not isinstance(records, list) or len(records) > 250:
                raise MarketDataError("Malformed option-chain page")
            for record in records:
                # Even unusable contracts remain in the raw page for the
                # normalizer to explain. Duplicate known IDs invalidate paging.
                details = record.get("details") if isinstance(record, dict) else None
                ticker = details.get("ticker") if isinstance(details, dict) else None
                if isinstance(ticker, str):
                    if ticker in seen_contracts:
                        raise MarketDataError("Duplicate contract across option-chain pages")
                    seen_contracts.add(ticker)
            pages.append(payload)
            next_url = payload.get("next_url")
            if not next_url:
                return tuple(pages)
            if not isinstance(next_url, str) or len(next_url) > 8192:
                raise MarketDataError("Invalid option-chain next URL")
            try:
                parts = urlsplit(next_url)
            except ValueError:
                raise MarketDataError("Malformed option-chain next URL") from None
            if (parts.scheme != "https" or parts.netloc != "api.massive.com" or parts.path != path or parts.fragment):
                raise MarketDataError("Unsafe option-chain next URL")
            try:
                pairs = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=20)
            except ValueError:
                raise MarketDataError("Malformed option-chain pagination query") from None
            if len(dict(pairs)) != len(pairs):
                raise MarketDataError("Duplicate pagination query keys")
            query = dict(pairs)
            if set(query) - {"cursor", "limit", "sort", "order", "expiration_date.gte", "expiration_date.lte"}:
                raise MarketDataError("Unexpected pagination query parameters")
            if not query.get("cursor"):
                raise MarketDataError("Missing option-chain cursor")
        raise MarketDataError("Option-chain page limit reached; incomplete data")
