from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from alpaca_agents.marketdata.client import (
    BASE_URL, DataCredentials, JsonlAudit, MarketDataClient, MarketDataError, _NoRedirect,
)
from alpaca_agents.marketdata.snapshot import load_snapshot, normalize_bars, normalize_chain
from alpaca_agents.scanner.contracts import OptionQuote, is_liquid
from alpaca_agents.scanner.scan import ScanConfig, scan
from alpaca_agents.scanner.__main__ import main

NOW = datetime(2026, 9, 15, 15, tzinfo=timezone.utc)
SESSION = date(2026, 9, 14)
EXPIRY = date(2026, 10, 23)
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def ns(value):
    return int((value - EPOCH).total_seconds()) * 1_000_000_000


def bars_payload():
    days, day = [], SESSION
    while len(days) < 220:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    rows = []
    for i, day in enumerate(reversed(days)):
        price = 40 * 1.003 ** i
        stamp = datetime(day.year, day.month, day.day, 4, tzinfo=timezone.utc)
        rows.append({"t": ns(stamp) // 1_000_000, "o": price, "c": price,
                     "h": price * 1.005, "l": price * .995, "v": 1_000_000})
    return {"status": "OK", "request_id": "bars-req", "ticker": "IWM", "adjusted": True, "results": rows}


def option_row():
    return {"details": {"ticker": "O:IWM261023C00080000", "contract_type": "call", "expiration_date": str(EXPIRY),
                        "strike_price": 80, "shares_per_contract": 100},
            "underlying_asset": {"ticker": "IWM"}, "greeks": {"delta": .4}, "open_interest": 800,
            "last_quote": {"bid": .88, "ask": .90, "bid_size": 5, "ask_size": 5,
                           "timeframe": "REAL-TIME", "last_updated": ns(NOW - timedelta(seconds=30))}}


def page(rows=None, **kwargs):
    return {"status": "OK", "request_id": "options-req", "results": [option_row()] if rows is None else rows, **kwargs}


class Response(BytesIO):
    def __init__(self, payload):
        super().__init__(json.dumps(payload).encode())
        self.code, self.headers = 200, {}


class Opener:
    def __init__(self, payloads):
        self.payloads = iter(payloads)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        item = next(self.payloads)
        if isinstance(item, Exception):
            raise item
        return Response(item)


class MarketDataTests(unittest.TestCase):
    def client(self, *payloads):
        self.events = []
        self.opener = Opener(payloads)
        return MarketDataClient(DataCredentials("fake-data-secret"), audit=self.events.append, opener=self.opener)

    def test_full_data_to_shadow_scan(self):
        client = self.client(bars_payload(), page())
        result = load_snapshot(client, symbol="IWM", completed_session=SESSION, now=NOW)
        self.assertTrue(result.snapshot.earnings_not_applicable)
        self.assertIsNone(result.snapshot.next_earnings)
        self.assertEqual(result.snapshot.valuation_day, NOW.date())
        self.assertEqual(len(result.snapshot.bars), 220)
        self.assertEqual(result.oldest_quote_at, NOW - timedelta(seconds=30))
        scanned = scan([result.snapshot], ScanConfig(), as_of=SESSION)
        self.assertTrue(scanned.shadow)
        self.assertEqual(scanned.proposals, [])
        request = self.opener.requests[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(urlsplit(request.full_url).netloc, "api.massive.com")
        self.assertEqual(request.get_header("Authorization"), "Bearer fake-data-secret")
        self.assertNotIn("fake-data-secret", request.full_url)
        self.assertNotIn("fake-data-secret", json.dumps(self.events))
        self.assertEqual(self.events[-1]["request_id"], "options-req")
        query = parse_qs(urlsplit(self.opener.requests[1].full_url).query)
        self.assertEqual(query["expiration_date.gte"], [str(NOW.date() + timedelta(days=30))])

    def test_current_and_old_sessions_reject_before_network(self):
        client = self.client()
        for session in (NOW.date(), SESSION - timedelta(days=5)):
            with self.assertRaises(MarketDataError):
                load_snapshot(client, symbol="IWM", completed_session=session, now=NOW)
        self.assertEqual(self.opener.requests, [])

    def test_unknown_stock_earnings_reject_before_network(self):
        client = self.client()
        with self.assertRaises(MarketDataError):
            load_snapshot(client, symbol="AAPL", completed_session=SESSION, now=NOW)
        self.assertEqual(self.opener.requests, [])

    def test_bars_reject_wrong_symbol_duplicates_nan_and_staleness(self):
        for mutation in ("symbol", "duplicate", "nan", "stale", "unadjusted", "short"):
            raw = bars_payload()
            if mutation == "symbol": raw["ticker"] = "SPY"
            if mutation == "duplicate": raw["results"][-1] = deepcopy(raw["results"][-2])
            if mutation == "nan": raw["results"][0]["c"] = float("nan")
            if mutation == "stale": raw["results"].pop()
            if mutation == "unadjusted": raw["adjusted"] = False
            if mutation == "short": raw["results"] = raw["results"][-20:]
            with self.subTest(mutation=mutation), self.assertRaises(MarketDataError):
                normalize_bars(raw, symbol="IWM", completed_session=SESSION)

    def test_missing_greeks_and_delayed_stale_future_quotes(self):
        for mutation, code in (("greeks", "MISSING_OR_MALFORMED_QUOTE_FIELDS"),
                               ("delayed", "REALTIME_QUOTE_REQUIRED"),
                               ("stale", "STALE_OR_FUTURE_QUOTE"), ("future", "STALE_OR_FUTURE_QUOTE")):
            row = option_row()
            if mutation == "greeks": del row["greeks"]
            if mutation == "delayed": row["last_quote"]["timeframe"] = "DELAYED"
            if mutation == "stale": row["last_quote"]["last_updated"] = ns(NOW - timedelta(seconds=121))
            if mutation == "future": row["last_quote"]["last_updated"] = ns(NOW + timedelta(seconds=1))
            with self.subTest(mutation=mutation), self.assertRaises(MarketDataError) as caught:
                normalize_chain([page([row])], symbol="IWM", now=NOW)
            self.assertEqual(caught.exception.diagnostics[0]["reason"], code)

    def test_one_bad_contract_does_not_hide_valid_candidates(self):
        bad = option_row()
        bad["last_quote"]["bid_size"] = 0
        quotes, issues, oldest = normalize_chain([page([bad, option_row()])], symbol="IWM", now=NOW)
        self.assertEqual(len(quotes), 1)
        self.assertEqual(issues[0]["reason"], "TWO_SIDED_SIZE_REQUIRED")

    def test_contract_identity_multiplier_price_and_delta_checks(self):
        mutations = [
            ("details", "shares_per_contract", 10), ("details", "ticker", "O:SPY261023C00080000"),
            ("underlying_asset", "ticker", "SPY"), ("greeks", "delta", -.4),
            ("greeks", "delta", float("inf")), ("last_quote", "bid", 1),
            ("last_quote", "ask", .901),
        ]
        for section, key, value in mutations:
            row = option_row()
            row[section][key] = value
            with self.subTest(key=key), self.assertRaises(MarketDataError):
                normalize_chain([page([row])], symbol="IWM", now=NOW)

    def test_chain_pagination_safe_same_origin_and_path(self):
        next_url = BASE_URL + "/v3/snapshot/options/IWM?cursor=abc"
        client = self.client(page(next_url=next_url), page([]))
        pages = client.option_chain("IWM", expiration_min=EXPIRY, expiration_max=EXPIRY)
        self.assertEqual(len(pages), 2)
        self.assertEqual(len(self.opener.requests), 2)
        self.assertEqual(parse_qs(urlsplit(self.opener.requests[-1].full_url).query), {"cursor": ["abc"]})
        for unsafe in ("http://api.massive.com/v3/snapshot/options/IWM?cursor=abc",
                       "https://evil.example/v3/snapshot/options/IWM?cursor=abc",
                       BASE_URL + "/v3/snapshot/options/SPY?cursor=abc",
                       next_url + "&apiKey=secret", next_url + "&cursor=duplicate"):
            client = self.client(page(next_url=unsafe))
            with self.subTest(url=unsafe), self.assertRaises(MarketDataError):
                client.option_chain("IWM", expiration_min=EXPIRY, expiration_max=EXPIRY)
            self.assertEqual(len(self.opener.requests), 1)

    def test_pagination_repeated_cursor_duplicate_contract_and_page_cap(self):
        next_url = BASE_URL + "/v3/snapshot/options/IWM?cursor=abc"
        for payloads, limit in (([page(next_url=next_url), page([], next_url=next_url)], 20),
                                ([page(next_url=next_url), page()], 20), ([page(next_url=next_url)], 1)):
            client = self.client(*payloads)
            with self.assertRaises(MarketDataError):
                client.option_chain("IWM", expiration_min=EXPIRY, expiration_max=EXPIRY, max_pages=limit)

    def test_truncated_daily_history_is_not_returned(self):
        client = self.client({**bars_payload(), "next_url": "more"})
        with self.assertRaises(MarketDataError):
            client.daily_bars("IWM", start=SESSION - timedelta(days=450), end=SESSION)

    def test_http_and_transport_errors_sanitized_and_never_retried(self):
        failure = HTTPError(BASE_URL, 403, "fake-data-secret", {},
                            BytesIO(b'{"status":"ERROR","request_id":"failed-request"}'))
        client = self.client(failure)
        with self.assertRaises(MarketDataError) as caught:
            client.daily_bars("IWM", start=SESSION, end=SESSION)
        self.assertNotIn("fake-data-secret", str(caught.exception))
        self.assertEqual(self.events[-1]["request_id"], "failed-request")
        self.assertEqual(len(self.opener.requests), 1)
        client = self.client(URLError("fake-data-secret"))
        with self.assertRaises(MarketDataError):
            client.daily_bars("IWM", start=SESSION, end=SESSION)
        self.assertEqual(self.events[-1]["outcome"], "transport_error")

    def test_audit_failure_blocks_network_or_data_release(self):
        opener = Opener([bars_payload()])
        def broken(event): raise OSError("disk full")
        client = MarketDataClient(DataCredentials("secret"), audit=broken, opener=opener)
        with self.assertRaises(OSError):
            client.daily_bars("IWM", start=SESSION, end=SESSION)
        self.assertEqual(opener.requests, [])
        def fail_completion(event):
            if event["event"] == "request_completed": raise OSError("disk full")
        client = MarketDataClient(DataCredentials("secret"), audit=fail_completion, opener=opener)
        with self.assertRaises(OSError):
            client.daily_bars("IWM", start=SESSION, end=SESSION)

    def test_credentials_and_endpoint_isolation(self):
        with patch.dict("os.environ", {"ALPACA_PAPER_API_KEY": "not-a-data-key"}, clear=True):
            with self.assertRaises(MarketDataError): DataCredentials.from_environment()
        with patch.dict("os.environ", {"POLYGON_API_KEY": "old-name-key"}, clear=True):
            self.assertEqual(DataCredentials.from_environment().key, "old-name-key")
            self.assertNotIn("old-name-key", repr(DataCredentials.from_environment()))
        client = self.client()
        with self.assertRaises(MarketDataError): client._get("/v2/orders", {})
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.example"))

    def test_clock_refreshed_after_fetch_without_treating_new_quote_as_future(self):
        row = option_row()
        row["last_quote"]["last_updated"] = ns(NOW + timedelta(seconds=5))
        client = self.client(bars_payload(), page([row]))
        result = load_snapshot(client, symbol="IWM", completed_session=SESSION, now=NOW,
                               clock=lambda: NOW + timedelta(seconds=10))
        self.assertEqual(result.observed_at, NOW + timedelta(seconds=10))

    def test_scanner_rejects_fake_stock_earnings_exemption(self):
        client = self.client(bars_payload(), page())
        snapshot = load_snapshot(client, symbol="IWM", completed_session=SESSION, now=NOW).snapshot
        bad = replace(snapshot, symbol="AAPL")
        result = scan([bad], ScanConfig(universe=frozenset({"AAPL"})), as_of=SESSION)
        self.assertEqual(result.shadow, [])
        self.assertEqual(result.skipped[0]["reason"], "invalid earnings exemption")

    def test_liquidity_function_handles_malformed_inputs_without_crashing(self):
        quote = OptionQuote(EXPIRY, Decimal(80), "call", Decimal(".88"), Decimal(".90"), .4, 800)
        for change in ({"bid": Decimal("NaN")}, {"delta": True}, {"open_interest": True},
                       {"delta": -.4}, {"ask": Decimal(".901")}, {"strike": Decimal("1.0001")}):
            self.assertFalse(is_liquid(replace(quote, **change)))

    def test_shadow_cli_with_mocked_snapshot_never_enables_proposals(self):
        client = self.client(bars_payload(), page())
        loaded = load_snapshot(client, symbol="IWM", completed_session=SESSION, now=NOW)
        class Clock:
            @staticmethod
            def now(tz): return NOW
        with tempfile.TemporaryDirectory() as tmp:
            output, audit = Path(tmp) / "shadow.json", Path(tmp) / "audit.jsonl"
            with patch.dict("os.environ", {"MASSIVE_API_KEY": "test-key"}, clear=True), \
                 patch("sys.argv", ["scanner", "--session", str(SESSION), "--symbols", "IWM", "--output", str(output), "--audit", str(audit)]), \
                 patch("alpaca_agents.scanner.__main__.load_snapshot", return_value=loaded), \
                 patch("alpaca_agents.scanner.__main__.datetime", Clock), patch("builtins.print"):
                self.assertEqual(main(), 0)
            report = json.loads(output.read_text())
            self.assertEqual(report["proposals"], [])
            self.assertTrue(report["shadow"])
            self.assertFalse(report["backtested"])
            events = [json.loads(line) for line in audit.read_text().splitlines()]
            self.assertTrue(any(e["event"] == "shadow_idea" for e in events))
            self.assertNotIn("test-key", audit.read_text())


if __name__ == "__main__":
    unittest.main()
