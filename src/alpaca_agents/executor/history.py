"""Bounded, read-only activity-history staging. Exhausted != reconciled.

Preserves all activity types, including fees and option lifecycle events, rather
than dropping anything that cannot yet be mapped to OptionFill. Raw records are
sensitive account data: store outside git with executor-only filesystem access.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from .client import ActivityPage, ExecutorError, PaperClient, activity_query


class HistoryError(ExecutorError):
    pass


def _id(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", value) is not None


class ActivityStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS history_account (singleton INTEGER PRIMARY KEY CHECK(singleton=1), account_id TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS history_runs (
                run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
                query TEXT NOT NULL, status TEXT NOT NULL, next_cursor TEXT,
                failure_code TEXT, error_local_id TEXT, error_request_id TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS history_pages (
                run_id TEXT NOT NULL, page_number INTEGER NOT NULL, requested_cursor TEXT,
                local_id TEXT NOT NULL, request_id TEXT, payload TEXT NOT NULL,
                PRIMARY KEY(run_id,page_number))""")
            db.execute("""CREATE TABLE IF NOT EXISTS history_ids (
                run_id TEXT NOT NULL, activity_id TEXT NOT NULL,
                PRIMARY KEY(run_id,activity_id))""")

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            with db:
                yield db
        finally:
            db.close()

    def start(self, account_id: str, query: dict) -> str:
        if not _id(account_id):
            raise HistoryError("Valid broker account identity required")
        run_id = uuid4().hex
        with self._transaction() as db:
            previous = db.execute("SELECT account_id FROM history_account WHERE singleton=1").fetchone()
            if previous and previous[0] != account_id:
                raise HistoryError("History database belongs to another account")
            db.execute("INSERT OR IGNORE INTO history_account VALUES (1,?)", (account_id,))
            db.execute("INSERT INTO history_runs VALUES (?,?,NULL,?,'started',NULL,NULL,NULL,NULL)",
                       (run_id, datetime.now(timezone.utc).isoformat(), json.dumps(query, sort_keys=True)))
        return run_id

    def append(self, run_id: str, cursor: str | None, page: ActivityPage):
        if not isinstance(page.records, list) or len(page.records) > 100:
            raise HistoryError("Invalid activity page shape or size")
        if not _id(page.local_id) or (page.request_id is not None and not _id(page.request_id)):
            raise HistoryError("Invalid activity trace metadata")
        identifiers = []
        for record in page.records:
            if (not isinstance(record, dict) or not _id(record.get("id"))
                    or not isinstance(record.get("activity_type"), str) or not record["activity_type"]):
                raise HistoryError("Malformed activity record; import blocked")
            identifiers.append(record["id"])
        if len(set(identifiers)) != len(identifiers):
            raise HistoryError("Duplicate activity in page; import blocked")
        encoded = json.dumps(page.records, allow_nan=False, sort_keys=True, separators=(",", ":"))
        with self._transaction() as db:
            run = db.execute("SELECT status,next_cursor FROM history_runs WHERE run_id=?", (run_id,)).fetchone()
            if run != ("started", cursor):
                raise HistoryError("Import state/cursor mismatch")
            for identifier in identifiers:
                if db.execute("SELECT 1 FROM history_ids WHERE run_id=? AND activity_id=?", (run_id, identifier)).fetchone():
                    raise HistoryError("Repeated activity across pages; import blocked")
                db.execute("INSERT INTO history_ids VALUES (?,?)", (run_id, identifier))
            number = db.execute("SELECT COUNT(*) FROM history_pages WHERE run_id=?", (run_id,)).fetchone()[0] + 1
            db.execute("INSERT INTO history_pages VALUES (?,?,?,?,?,?)",
                       (run_id, number, cursor, page.local_id, page.request_id, encoded))
            if identifiers:
                db.execute("UPDATE history_runs SET next_cursor=? WHERE run_id=?", (identifiers[-1], run_id))
            else:
                # Persist empty terminal page and exhausted status atomically.
                db.execute("UPDATE history_runs SET status='exhausted',finished_at=? WHERE run_id=?",
                           (datetime.now(timezone.utc).isoformat(), run_id))

    def fail(self, run_id: str, code: str, *, local_id=None, request_id=None):
        with self._transaction() as db:
            db.execute("""UPDATE history_runs SET status='failed',finished_at=?,failure_code=?,
                          error_local_id=?,error_request_id=? WHERE run_id=? AND status='started'""",
                       (datetime.now(timezone.utc).isoformat(), code, local_id, request_id, run_id))

    def report(self, run_id: str) -> dict:
        with self._transaction() as db:
            row = db.execute("SELECT status,failure_code,error_local_id,error_request_id FROM history_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise HistoryError("Unknown import run")
            pages = db.execute("SELECT COUNT(*) FROM history_pages WHERE run_id=?", (run_id,)).fetchone()[0]
            records = db.execute("SELECT COUNT(*) FROM history_ids WHERE run_id=?", (run_id,)).fetchone()[0]
            return {"run_id": run_id, "status": row[0], "failure_code": row[1],
                    "error_local_id": row[2], "error_request_id": row[3],
                    "pages": pages, "records": records, "reconciled": False}


def import_activities(client: PaperClient, store: ActivityStore, *, after: datetime,
                      until: datetime, max_pages: int = 100, now: datetime | None = None) -> dict:
    query = activity_query(after=after, until=until)
    now = datetime.now(timezone.utc) if now is None else now
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None or until > now:
        raise HistoryError("Import upper bound must not be in the future")
    if type(max_pages) is not int or not 1 <= max_pages <= 1000:
        raise HistoryError("max_pages must be between 1 and 1000")
    # Identity comes from the authenticated paper endpoint, not a CLI/scanner ID.
    account = client.account()
    run_id = store.start(account.get("id"), query)
    cursor = None
    page = None
    try:
        for _ in range(max_pages):
            page = None
            page = client.activities_page(after=after, until=until, page_token=cursor)
            store.append(run_id, cursor, page)  # Commit before fetching another page.
            if not page.records:
                return store.report(run_id)
            cursor = page.records[-1]["id"]
        store.fail(run_id, "PAGE_LIMIT")
        raise HistoryError(f"Activity import hit page limit; run_id={run_id}")
    except Exception as exc:
        # Preserve partial runs, but never return them as exhausted/reconciled.
        local_id = getattr(exc, "local_id", None) or (page.local_id if page is not None else None)
        request_id = getattr(exc, "request_id", None) or (page.request_id if page is not None else None)
        store.fail(run_id, "IMPORT_ERROR", local_id=local_id, request_id=request_id)
        raise HistoryError(f"Activity import stopped; run_id={run_id}; "
                           f"local_id={local_id or 'unavailable'}; request_id={request_id or 'unavailable'}",
                           local_id=local_id, request_id=request_id) from None
