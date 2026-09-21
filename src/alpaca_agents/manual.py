"""Owner-drafted single idea. Star's job (pick a real contract), nothing more.

`draft` builds ONE idea from live data using the scanner's own contract selector and
idea builder, then writes runtime/manual-idea.json. It does not reserve, claim, submit,
or touch the journal. The controller, launched with --manual-idea and
--enable-playbook manual (which needs runtime/playbooks/manual.approved), picks the
draft up on its next cycle and feeds it through the normal path: Moon evaluates it,
the journal reserves and claims, Houston submits the journal's stored body.

A draft is single-use and short-lived: it expires MAX_AGE after drafting so a stale
quote is never submitted, and the controller renames it after ONE attempt whatever
the outcome, so a second cycle can never resubmit it.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from uuid import uuid4

from .scanner.scan import MANUAL_PLAYBOOK, ScanConfig, build_manual_idea, earnings_exempt

SCHEMA_VERSION = 1
MAX_AGE = timedelta(minutes=10)
FILENAME = "manual-idea.json"
MAX_BYTES = 64 * 1024


def _unique(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate key")
        out[k] = v
    return out


def load_draft(path: Path, *, now: datetime) -> tuple[dict | None, str]:
    """(idea, reason). idea is None unless the draft is well-formed, unexpired and unconsumed."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, "no draft"
    except OSError:
        return None, "draft unreadable"
    try:
        if len(raw) > MAX_BYTES:
            raise ValueError("oversize")
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
        if (not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION
                or not re.fullmatch(r"[0-9a-f]{32}", str(data.get("id")))
                or not isinstance(data.get("idea"), dict) or data["idea"].get("playbook") != MANUAL_PLAYBOOK):
            raise ValueError("schema")
        drafted = datetime.fromisoformat(data["drafted_at"])
        if drafted.tzinfo is None:
            raise ValueError("naive timestamp")
    except (ValueError, TypeError, KeyError, UnicodeDecodeError):
        return None, "draft malformed; re-draft"
    age = now - drafted
    if age < timedelta(0):
        return None, "draft timestamp is in the future; clock error"
    if age > MAX_AGE:
        return None, f"draft expired ({int(age.total_seconds() // 60)} min old; limit {int(MAX_AGE.total_seconds() // 60)}); re-draft"
    return data["idea"], data["id"]


def consume(path: Path, draft_id: str) -> Path | None:
    """Rename the draft after one attempt so it can never be picked up again."""
    target = path.with_name(f"{path.stem}.used-{draft_id}{path.suffix}")
    try:
        os.replace(path, target)
        return target
    except OSError:
        return None


def _write(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, newline="\n") as stream:
            name = stream.name
            json.dump(data, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if name is not None and os.path.exists(name):
            os.unlink(name)


def main(argv=None) -> int:
    from .calendar import previous_session
    from .earnings import load as load_earnings
    from .executor.eastern import eastern_date
    from .marketdata.client import DataCredentials, JsonlAudit, MarketDataClient, MarketDataError
    from .marketdata.snapshot import load_snapshot
    parser = argparse.ArgumentParser(description="Draft ONE owner-requested idea from live data. Does not trade.")
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("draft", help="pick a real contract and write runtime/manual-idea.json")
    d.add_argument("symbol")
    d.add_argument("right", choices=("call", "put"))
    d.add_argument("--note", required=True, help="why you want this trade; recorded in the idea's thesis")
    d.add_argument("--runtime", type=Path, default=Path("runtime"))
    sub.add_parser("show", help="print the current draft and its status").add_argument("--runtime", type=Path, default=Path("runtime"))
    args = parser.parse_args(argv)
    path = args.runtime / FILENAME
    now = datetime.now(timezone.utc)
    if args.command == "show":
        idea, status = load_draft(path, now=now)
        if idea is None:
            print(f"draft: {status}")
            return 1
        print(json.dumps(idea, indent=2))
        print(f"draft id {status}; expires {(datetime.fromisoformat(json.loads(path.read_text(encoding='utf-8'))['drafted_at']) + MAX_AGE).isoformat()}")
        return 0
    symbol = args.symbol.upper()
    if not re.fullmatch(r"[A-Z]{1,6}", symbol):
        parser.error("symbol must be 1-6 uppercase letters")
    today = eastern_date(now)
    session = previous_session(today)
    next_earnings = None
    if not earnings_exempt(symbol):
        calendar = load_earnings(args.runtime / "earnings.json", today=today)
        if symbol not in calendar["verified"]:
            reasons = {i["symbol"]: i["reason"] for i in calendar["issues"]}
            print(f"{symbol}: earnings not verified: {reasons.get(symbol, 'no entry in earnings.json')}. "
                  f"Run: python -m alpaca_agents.earnings set {symbol} YYYY-MM-DD")
            return 1
        next_earnings = calendar["verified"][symbol]
    try:
        client = MarketDataClient(DataCredentials.from_environment(), audit=JsonlAudit(args.runtime / "market-data.jsonl"))
        loaded = load_snapshot(client, symbol=symbol, completed_session=session, now=now,
                               next_earnings=next_earnings, clock=lambda: datetime.now(timezone.utc))
    except MarketDataError as exc:
        print(f"{symbol}: {exc}")
        return 1
    result = build_manual_idea(loaded.snapshot, args.right, ScanConfig(universe=frozenset({symbol})),
                               as_of=session, contract_day=loaded.observed_at.date(), note=args.note)
    if isinstance(result, str):
        print(f"{symbol} {args.right}: refused at draft: {result}")
        return 1
    if next_earnings is not None and next_earnings <= datetime.fromisoformat(result["legs"][0]["expiration"]).date():
        print(f"{symbol}: contract expires {result['legs'][0]['expiration']}, on or after earnings {next_earnings}; refused")
        return 1
    draft_id = uuid4().hex
    _write(path, {"schema_version": SCHEMA_VERSION, "id": draft_id, "drafted_at": loaded.observed_at.isoformat(),
                  "quote_at": loaded.oldest_quote_at.isoformat(), "idea": result})
    leg = result["legs"][0]
    print(f"Drafted {result['strategy']} {symbol} {leg['expiration']} {leg['strike']}{'C' if leg['right'] == 'call' else 'P'} "
          f"limit {result['limit_debit']} -> premium ${result['est_contract_cost']} + fees ${result['estimated_fees']}")
    print(f"Underlying entry {result['entry_trigger']} stop {result['stop']} target {result['target']} (rr {result['reward_risk']})")
    print(f"Draft {draft_id} written to {path}; expires in {int(MAX_AGE.total_seconds() // 60)} min.")
    print("Nothing was reserved or sent. The controller submits it only if launched with "
          f"--manual-idea {path} --enable-playbook {MANUAL_PLAYBOOK} --submit and {MANUAL_PLAYBOOK}.approved exists.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
