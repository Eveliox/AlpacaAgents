"""Multi-year daily-bar history: stitched provider fetches or a local JSON cache."""
from datetime import date, timedelta
import json
import os
from pathlib import Path
import tempfile

from alpaca_agents.marketdata.client import MarketDataError, symbol_checked
from alpaca_agents.marketdata.snapshot import bars_from_payload
from alpaca_agents.scanner.indicators import Bar, validate_bars

CHUNK_DAYS = 700   # under the client's 730-day per-request cap


def fetch_history(client, symbol: str, *, start: date, end: date) -> tuple:
    symbol_checked(symbol)
    if type(start) is not date or type(end) is not date or start >= end:
        raise MarketDataError("History window must be ordered dates")
    bars = []
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=CHUNK_DAYS))
        try:
            chunk = bars_from_payload(client.daily_bars(symbol, start=cursor, end=chunk_end), symbol=symbol)
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
            raise MarketDataError("Malformed daily-bar chunk") from None
        if bars and chunk and chunk[0].day <= bars[-1].day:
            raise MarketDataError("Overlapping or unordered history chunks")
        bars.extend(chunk)
        cursor = chunk_end + timedelta(days=1)
    try:
        validate_bars(bars)
    except ValueError as exc:
        raise MarketDataError(f"Stitched history invalid: {exc}") from None
    return tuple(bars)


def save_bars(path: Path, symbol: str, bars) -> None:
    rows = [{"day": b.day.isoformat(), "open": b.open, "high": b.high, "low": b.low,
             "close": b.close, "volume": b.volume} for b in bars]
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            name = stream.name
            json.dump({"symbol": symbol, "bars": rows}, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def load_bars(path: Path, symbol: str) -> tuple:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"), parse_constant=lambda v: (_ for _ in ()).throw(ValueError(v)))
        if payload["symbol"] != symbol:
            raise ValueError("bars file is for a different symbol")
        bars = [Bar(date.fromisoformat(r["day"]), float(r["open"]), float(r["high"]),
                    float(r["low"]), float(r["close"]), float(r["volume"])) for r in payload["bars"]]
        validate_bars(bars)
        return tuple(bars)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise MarketDataError(f"Unusable bars file: {type(exc).__name__}") from None
