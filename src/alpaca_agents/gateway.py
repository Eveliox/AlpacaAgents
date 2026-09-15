"""I/O boundary around the pure engine. Audit failure prevents decision release."""
import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .rules import RiskState, evaluate


CONTROL_MODES = ("ARMED_PAPER", "EXITS_ONLY", "DISABLED")


def control_mode(control_file: Path) -> str:
    """Exact file contents, or DISABLED for missing/unreadable/unknown. Read on every evaluation."""
    try:
        mode = control_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return "DISABLED"
    return mode if mode in CONTROL_MODES else "DISABLED"


def trading_disabled(control_file: Path) -> bool:
    """Entries require ARMED_PAPER exactly. EXITS_ONLY still disables new entries."""
    return control_mode(control_file) != "ARMED_PAPER"


def _append_events(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Intended for a single controller process. Production needs transactional
    # storage and locking shared with reservations and broker reconciliation.
    with path.open("a", encoding="utf-8") as stream:
        for event in events:
            stream.write(json.dumps(event, allow_nan=False, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def validate_and_log(idea: Any, state: RiskState, *, now: datetime,
                     trading_day: date, control_file: Path, audit_file: Path) -> dict:
    # Input is JSON, not arbitrary Python objects. Snapshot it so later scanner
    # mutation cannot change the decision being logged.
    try:
        idea = json.loads(json.dumps(idea, allow_nan=False))
    except (TypeError, ValueError, OverflowError):
        # Do not let invalid JSON (including NaN) bypass rejection auditing.
        idea = None
    result = evaluate(idea, state, now=now, trading_day=trading_day,
                      kill_switch=trading_disabled(control_file))
    stamp = now.isoformat()
    events = [
        {"timestamp": stamp, "event": "idea_generated", "idea": idea},
        {"timestamp": stamp, "event": "idea_approved" if result["approved"] else "idea_rejected", **result},
    ]
    if result["reason"].startswith("CIRCUIT_BREAKER:"):
        events.append({"timestamp": stamp, "event": "circuit_breaker_blocked", "reason": result["reason"]})
    _append_events(audit_file, events)
    return result
