"""Owner-verified earnings dates for single stocks. The file is the record; nothing here guesses.

Why a file and not a model: the scanner refuses any stock without a verified future
earnings date, and refuses any contract expiring on or after it. That gate is only
worth having if its input is something a human checked against the company's own
investor-relations page. `python -m alpaca_agents.earnings set SYM YYYY-MM-DD` is
that act of checking: it stamps today's date as `checked`. Entries age out after
MAX_CHECK_AGE_DAYS and past dates are excluded until the next date is entered, so
a stale calendar fails closed instead of quietly trading through a report.
"""
import argparse
from datetime import date, timedelta
import json
import os
from pathlib import Path
import re
import tempfile

from .scanner.scan import INDEX_ETFS

SCHEMA_VERSION = 1
MAX_CHECK_AGE_DAYS = 45      # companies reschedule; re-verify at least every 45 days
MAX_HORIZON_DAYS = 200       # further out than any plausible next report
MAX_BYTES = 256 * 1024
SYMBOL = re.compile(r"[A-Z]{1,6}")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _iso(value):
    if type(value) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("date must be YYYY-MM-DD")
    return date.fromisoformat(value)


def load(path: Path, *, today: date) -> dict:
    """Return {"verified": {SYM: date}, "issues": [{symbol, reason}], "file": status}.

    Only entries that pass every check appear in "verified". Everything else is an
    issue with a reason, so the dashboard and cycle report can show *why* a name
    was not scanned. A missing file is not an error: it means no stocks are verified.
    """
    verified, issues = {}, []
    if type(today) is not date:
        raise TypeError("today must be a date")
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return {"verified": {}, "issues": [], "file": "missing"}
    except OSError:
        return {"verified": {}, "issues": [{"symbol": "*", "reason": "earnings file unreadable"}], "file": "unreadable"}
    try:
        if len(raw) > MAX_BYTES:
            raise ValueError("oversize")
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION or not isinstance(data.get("entries"), dict):
            raise ValueError("schema")
    except (ValueError, UnicodeDecodeError):
        return {"verified": {}, "issues": [{"symbol": "*", "reason": "earnings file malformed; no stocks verified"}], "file": "malformed"}
    for symbol, entry in data["entries"].items():
        def issue(reason):
            issues.append({"symbol": symbol if isinstance(symbol, str) else "?", "reason": reason})
        if not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol):
            issue("invalid symbol")
            continue
        if symbol in INDEX_ETFS:
            issue("index ETFs have no earnings; remove this entry")
            continue
        if not isinstance(entry, dict):
            issue("entry must be an object with date and checked")
            continue
        try:
            when, checked = _iso(entry.get("date")), _iso(entry.get("checked"))
        except ValueError as exc:
            issue(str(exc))
            continue
        source = entry.get("source")
        if source is not None and (type(source) is not str or len(source) > 200):
            issue("source must be a short string")
            continue
        if checked > today:
            issue("checked date is in the future; clock or entry error")
        elif (today - checked).days > MAX_CHECK_AGE_DAYS:
            issue(f"verified {(today - checked).days} days ago; re-check (limit {MAX_CHECK_AGE_DAYS})")
        elif when <= today:
            issue(f"earnings {when} has passed; enter the next confirmed date")
        elif (when - today).days > MAX_HORIZON_DAYS:
            issue(f"earnings {when} is implausibly far out")
        else:
            verified[symbol] = when
    return {"verified": verified, "issues": issues, "file": "ok"}


def _write(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, newline="\n") as stream:
            name = stream.name
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if name is not None and os.path.exists(name):
            os.unlink(name)


def _read_for_edit(path: Path) -> dict:
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "entries": {}}
    data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique)
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION or not isinstance(data.get("entries"), dict):
        raise ValueError("existing earnings file has an unsupported schema; fix or remove it by hand")
    return data


def main(argv=None) -> int:
    from .executor.eastern import eastern_date
    from datetime import datetime, timezone
    parser = argparse.ArgumentParser(description="Owner-verified earnings calendar. Dates you enter are treated as checked TODAY.")
    parser.add_argument("--runtime", type=Path, default=Path("runtime"))
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("set", help="record a date you verified on the company's investor-relations page")
    s.add_argument("symbol")
    s.add_argument("date", help="YYYY-MM-DD")
    s.add_argument("--source", help="where you checked it (kept for the audit trail)")
    r = sub.add_parser("remove")
    r.add_argument("symbol")
    sub.add_parser("list", help="show every entry with its current verdict")
    args = parser.parse_args(argv)
    path = args.runtime / "earnings.json"
    today = eastern_date(datetime.now(timezone.utc))
    try:
        if args.command == "set":
            symbol = args.symbol.upper()
            if not SYMBOL.fullmatch(symbol):
                parser.error("symbol must be 1-6 uppercase letters")
            if symbol in INDEX_ETFS:
                parser.error(f"{symbol} is an index ETF; it has no earnings and needs no entry")
            try:
                when = _iso(args.date)
            except ValueError as exc:
                parser.error(str(exc))
            if when <= today:
                parser.error(f"{when} is not in the future; enter the NEXT report date")
            data = _read_for_edit(path)
            data["entries"][symbol] = {"date": when.isoformat(), "checked": today.isoformat()}
            if args.source:
                data["entries"][symbol]["source"] = args.source[:200]
            _write(path, data)
            print(f"{symbol}: earnings {when}, verified {today}. Re-check within {MAX_CHECK_AGE_DAYS} days.")
        elif args.command == "remove":
            data = _read_for_edit(path)
            if data["entries"].pop(args.symbol.upper(), None) is None:
                print(f"{args.symbol.upper()}: no entry")
                return 1
            _write(path, data)
            print(f"{args.symbol.upper()}: removed")
        else:
            state = load(path, today=today)
            for symbol, when in sorted(state["verified"].items()):
                print(f"{symbol:6} {when}  verified")
            for item in state["issues"]:
                print(f"{item['symbol']:6} EXCLUDED: {item['reason']}")
            print(f"{len(state['verified'])} verified, {len(state['issues'])} excluded, file {state['file']}")
        return 0
    except (OSError, ValueError) as exc:
        print(f"earnings: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
