"""Read-only Agent Desk projection. No controller, broker, LLM or rule execution.

Cycles are recorded summaries, not a live event stream. Exact decision-key joins
may add a journaled idea and its *current* journal status; neither is an approval
capability. Never infer an entry/exit relationship from a ticker or timestamp.
"""
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, DecimalException
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import parse_qs, urlsplit

from .dashboard_chat import redact_secrets

MAX_BYTES = 512 * 1024
MAX_CYCLES = 20
MAX_ROWS = 12
MAX_LINE = 64 * 1024
ID = re.compile(r"(?:[a-f0-9]{32}|legacy-[a-f0-9]{24})\Z")
ROLES = (
    ("spotter", "Star / Setups", "Spotter"),
    ("prior", "Research / Evidence", "Prior"),
    ("edge", "Research / Validation gaps", "Edge"),
    ("risk", "Moon / Risk budget", "Sizing, not Kelly"),
    ("entry", "Houston / Entry", "Taker"),
    ("exit", "Houston / Exit", "Closer"),
)
LIMITATIONS = [
    "Recorded stage summaries, not live agent events. Stage timing and confidence were not recorded.",
    "No frozen market-input archive in these reports. Current quotes and standalone scans are not joined to old cycles.",
    "Research is not linked to these decisions. Prior calibration, option-level edge and Kelly sizing are not implemented.",
    "Reservation risk pass is not human approval. Submission is not a verified fill. No approval or order controls exist here.",
]


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate record key')
        result[key] = value
    return result


def obj(value):
    return value if isinstance(value, dict) else {}


def rows(value):
    return [r for r in value[:MAX_ROWS] if isinstance(r, dict)] if isinstance(value, list) else []


def text(value):
    if not isinstance(value, str):
        return None
    # Also redact common private identifiers embedded in otherwise allowed prose.
    value = re.sub(r"\b(?:account_id|authorization_id|client_order_id|broker_order_id|request_id|local_id)\s*[:=]\s*[^\s,;]+",
                   "[private reference redacted]", value, flags=re.I)
    return redact_secrets(value)[:400]


def enum(value, allowed):
    return value if isinstance(value, str) and value in allowed else None


def boolean(value):
    return value if type(value) is bool else None


def count(value):
    return value if type(value) is int and 0 <= value <= 1_000_000 else None


def number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        n = Decimal(str(value))
        return format(n, 'f') if n.is_finite() and abs(n) <= 1_000_000_000 and n.as_tuple().exponent >= -12 else None
    except (DecimalException, ValueError):
        return None


def stamp(value):
    try:
        d = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return d.astimezone(timezone.utc).isoformat() if d.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def submission(value):
    r = obj(value)
    return {"outcome": enum(r.get('outcome'), ('submitted', 'unknown', 'unplaced', 'not_submitted')),
            "broker_status": enum(r.get('broker_status'), ('new', 'accepted', 'pending_new', 'partially_filled', 'filled', 'canceled', 'expired', 'rejected', 'done_for_day')),
            "reason": text(r.get('reason'))}


def proposal(idea, contract=None):
    """Fixed fields only. A displayed estimate is never a rules verdict."""
    idea = obj(idea)
    legs = rows(idea.get('legs'))
    leg = legs[0] if len(legs) == 1 else {}
    q = count(idea.get('quantity'))
    debit, fees = number(idea.get('limit_debit')), number(idea.get('estimated_fees'))
    strategy = enum(idea.get('strategy'), ('long_call', 'long_put'))
    risk = None
    if strategy and len(legs) == 1 and q and debit is not None and fees is not None:
        if Decimal(debit) > 0 and Decimal(fees) >= 0:
            risk = number(Decimal(debit) * 100 * q + Decimal(fees))
    return {"symbol": text(idea.get('symbol')), "contract": text(contract),
            "strategy": strategy, "underlying_bias": enum(idea.get('direction'), ('long', 'short')),
            "right": enum(leg.get('right'), ('call', 'put')), "expiration": text(leg.get('expiration')),
            "strike": number(leg.get('strike')), "quantity": q, "multiplier": 100,
            "limit_premium_usd": debit, "estimated_fees_usd": fees, "estimated_max_risk_usd": risk,
            "underlying_entry": number(idea.get('entry_trigger')), "underlying_stop": number(idea.get('stop')),
            "underlying_target": number(idea.get('target')), "reasoning": text(idea.get('thesis')),
            "exit_plan": {"premium_stop_pct": number(obj(idea.get('exit_plan')).get('premium_stop_pct')),
                          "time_stop_dte": count(obj(idea.get('exit_plan')).get('time_stop_dte')),
                          "time_stop_sessions": count(obj(idea.get('exit_plan')).get('time_stop_sessions')),
                          "underlying_stop_rule": text(obj(idea.get('exit_plan')).get('underlying_stop_rule'))},
            "risk_basis": "Single-leg long premium × 100 × quantity + estimated fees; not underlying stop distance.",
            "confidence": None, "expected_option_edge": None}


def _journal(db, cycle_id, entry):
    # The existing controller's exact decision key, never 'latest for symbol'.
    symbol, playbook = entry.get('symbol'), entry.get('playbook')
    if (db is None or not re.fullmatch(r'[a-f0-9]{32}', cycle_id)
            or not isinstance(symbol, str) or not re.fullmatch(r'[A-Z]{1,6}', symbol)
            or not isinstance(playbook, str) or not re.fullmatch(r'[a-z_]{1,50}', playbook)):
        return None
    key = f'entry-{cycle_id}-{symbol}-{playbook}'
    r = db.execute("""SELECT substr(idea,1,16385) idea, status, contract, created_at
                       FROM order_intents WHERE decision_key=? AND kind='entry'""", (key,)).fetchone()
    if not r or len(r['idea']) > 16384:
        return None
    try:
        idea = json.loads(r['idea'], object_pairs_hook=_unique_object)
    except (ValueError, TypeError):
        return None
    if not isinstance(idea, dict) or idea.get('symbol') != symbol or idea.get('playbook') != playbook:
        return None
    return {"proposal": proposal(idea, r['contract']),
            "journal_status_now": enum(r['status'], ('reserved', 'claimed', 'rejected', 'expired', 'released',
                'resolved:filled', 'resolved:canceled', 'resolved:expired', 'resolved:rejected', 'resolved:done_for_day', 'resolved:unplaced', 'resolved:released_manual')),
            "journal_created_at": stamp(r['created_at']),
            "source": "orders.sqlite3 · exact controller decision-key match; status is current at read time"}


def project_cycle(raw, *, cycle_id, db=None):
    """Project one record independently. No joining of unrelated research/scans."""
    s = obj(raw.get('stages'))
    rec, scan, entry_stage = (obj(s.get(k)) for k in ('reconcile', 'scan', 'entries'))
    mode = enum(raw.get('control_mode'), ('DISABLED', 'EXITS_ONLY', 'ARMED_PAPER'))
    enabled = raw.get('enabled_playbooks')
    enabled = [text(p) for p in enabled[:MAX_ROWS]] if isinstance(enabled, list) and all(isinstance(p, str) for p in enabled) else None
    reconcile = {"ok": boolean(rec.get('ok')), "reasons": [text(r) for r in rec.get('reasons', [])[:MAX_ROWS] if isinstance(r, str)] if isinstance(rec.get('reasons'), list) else [],
                 "error": text(rec.get('error')), "open_positions": count(rec.get('open_positions')),
                 "pending": count(rec.get('pending')), "settled_cash": number(rec.get('settled_cash')),
                 "daily_loss": number(rec.get('daily_loss')), "breaker": boolean(rec.get('breaker'))}
    blocked = reconcile['ok'] is False or 'error' in rec or 'halt' in s
    skipped = text(entry_stage.get('skipped'))
    error = text(entry_stage.get('error'))
    entries = []
    for i, r in enumerate(rows(entry_stage.get('entries'))):
        item = {"id": f'{cycle_id}:entry:{i}', "symbol": text(r.get('symbol')), "playbook": text(r.get('playbook')),
                "risk_pass_at_reservation": boolean(r.get('reserved')), "reason": text(r.get('reason')),
                "claimed_at_cycle": boolean(r.get('claimed')), "claim_reason": text(r.get('claim_reason')),
                "submission": submission(r.get('submission')), "journal": None,
                "source": f'cycles.jsonl · stages.entries.entries[{i}]'}
        item['journal'] = _journal(db, cycle_id, r)
        entries.append(item)
    exits = []
    for i, r in enumerate(rows(s.get('exits'))):
        d = obj(r.get('decision'))
        exits.append({"id": f'{cycle_id}:exit:{i}', "contract": text(r.get('contract')), "note": text(r.get('note')),
                      "prepared": boolean(r.get('prepared')), "reason": text(r.get('reason')),
                      "submission": submission(r.get('submission')),
                      "decision": {"exit_reason": text(d.get('exit_reason')), "quantity": count(d.get('quantity')),
                                   "limit_price": number(d.get('limit_price'))},
                      "entry_link": None, "realized_pnl": None,
                      "source": f'cycles.jsonl · stages.exits[{i}]'})
    scan_view = {k: count(scan.get(k)) for k in ('shadow', 'proposals', 'skipped')}
    scan_view['refused'] = [{"symbol": text(r.get('symbol')), "reason": text(r.get('reason'))} for r in rows(scan.get('refused'))]
    if scan:
        scan_state, scan_note = 'recorded', 'Scan counts and gate refusals recorded. Signal inputs and shadow ideas are not archived here.'
    elif blocked or skipped or mode in ('DISABLED', 'EXITS_ONLY'):
        scan_state, scan_note = 'skipped', skipped or 'Cycle halted or entry mode does not permit scanning.'
    else:
        scan_state, scan_note = 'unknown', error or 'No scan stage recorded.'
    if error:
        scan_state = 'error'
    risk_state = 'rejected' if any(r['risk_pass_at_reservation'] is False for r in entries) else (
        'risk_pass' if entries and all(r['risk_pass_at_reservation'] is True for r in entries) else 'skipped' if skipped or blocked else 'unknown')
    entry_state = 'uncertain' if any(r['submission']['outcome'] == 'unknown' for r in entries) else (
        'submission_recorded' if any(r['submission']['outcome'] == 'submitted' for r in entries) else
        'rejected' if any(r['risk_pass_at_reservation'] is False
                          or (r['claimed_at_cycle'] is False and r['claim_reason'] and not r['claim_reason'].startswith('DRY_RUN:'))
                          or r['submission']['outcome'] == 'unplaced' for r in entries) else
        'dry_run' if entries and raw.get('submit') is False and any(r['risk_pass_at_reservation'] is True for r in entries) else
        'recorded' if entries else 'skipped' if skipped or blocked else 'error' if error else 'unknown')
    # The controller writes stages.exits (possibly []) after reconcile passes, but only evaluates exits
    # when there is inventory and the mode permits. Distinguish 'nothing to evaluate' from 'not evaluated'.
    exits_present = isinstance(s.get('exits'), list)
    if exits:
        exit_state, exit_note = 'recorded', 'Exit evaluations precede scanning. No entry linkage or realized P&L is inferred from a matching contract.'
    elif blocked:
        exit_state, exit_note = 'skipped', 'Cycle halted before exit evaluation.'
    elif exits_present and reconcile['open_positions'] == 0:
        exit_state, exit_note = 'recorded', 'Exit stage ran: no open option positions at this cycle, so nothing to evaluate.'
    elif exits_present and mode == 'DISABLED':
        exit_state, exit_note = 'skipped', 'DISABLED does not evaluate automatic exits; open positions were not managed this cycle.'
    else:
        exit_state, exit_note = 'unknown', 'No exit evaluations recorded; this does not prove there were no positions.'
    specs = {
        'spotter': (scan_state, scan_note, 'cycles.jsonl · stages.scan', scan_view),
        'prior': ('not_implemented', 'No calibrated prior. Saved research can be reviewed separately; it was not linked to this cycle.', None,
                  {"probability": None, "research_linked_to_cycle": False}),
        'edge': ('not_implemented', 'Option-level expectancy is not modeled. Underlying R is not an option edge estimate.', None,
                 {"expected_option_edge": None, "options_pnl_modelled": False}),
        'risk': (risk_state, 'Recorded reservation verdicts, not human approval or current permission. Cycle reconciliation is not the later reserve/claim input snapshot.',
                 'cycles.jsonl · stages.reconcile / stages.entries', {"cycle_reconciliation": reconcile, "entry_verdicts": entries}),
        'entry': (entry_state, skipped or error or 'Recorded entry attempts. Acknowledged submission is not a verified fill. Unknown outcomes require reconciliation, never retry.',
                  'cycles.jsonl · stages.entries', {"entries": entries, "skip_reason": skipped, "error": error}),
        'exit': (exit_state, exit_note, 'cycles.jsonl · stages.exits', {"exits": exits, "open_positions_at_reconcile": reconcile['open_positions']}),
    }
    agents = [{"id": key, "name": name, "role": role, "status": specs[key][0], "summary": specs[key][1],
               "source": specs[key][2], "outputs": specs[key][3], "confidence": None, "latency_ms": None,
               "model": None, "inputs": None} for key, name, role in ROLES]
    finished = stamp(raw.get('finished_at'))
    cycle_status = 'incomplete' if not finished else 'halted' if blocked else 'error' if error else entry_state
    if reconcile['ok'] is None or mode is None:
        cycle_status = 'unknown'
    if any(r['submission']['outcome'] == 'unknown' for r in exits):
        cycle_status = 'uncertain'
    flow = [
        {"name": "Reconcile", "status": 'blocked' if blocked else 'passed_at_cycle' if reconcile['ok'] is True else 'unknown'},
        {"name": "Existing exits", "status": exit_state},
        {"name": "Entry-mode gate", "status": 'skipped' if blocked else mode or 'unknown'},
        {"name": "Scan & candidate gates", "status": scan_state},
        {"name": "Reserve → optional claim / submit", "status": entry_state},
    ]
    activity = [{"label": step['name'], "status": step['status'], "recorded_at": finished,
                 "stage_timestamp": None, "detail": detail, "source": source}
                for step, detail, source in zip(flow, (reconcile, {"exits": exits}, {"mode": mode, "enabled_playbooks_at_cycle": enabled, "skip_reason": skipped}, scan_view, {"entries": entries, "error": error}),
                                               ('stages.reconcile', 'stages.exits', 'control_mode / stages.entries', 'stages.scan', 'stages.entries'))]
    return {"id": cycle_id, "started_at": stamp(raw.get('started_at')), "completed_at": finished,
            "status": cycle_status, "control_mode_at_cycle": mode, "enabled_playbooks_at_cycle": enabled,
            "submit_at_cycle": boolean(raw.get('submit')),
            "agents": agents, "flow": flow, "activity": activity, "entries": entries, "exits": exits,
            "market_snapshot": None, "human_approval": "Not available in Agent Desk",
            "limits_reference": {"capital_cap_usd": "2000", "maximum_entry_risk_usd": "100", "maximum_positions": 2,
                                 "cumulative_daily_loss_breaker_usd": "40", "note": "Current system limits, not a frozen historical configuration. No Kelly sizing."},
            "source": "cycles.jsonl; journal enrichment uses exact decision keys only",
            "details_truncated": any(isinstance(v, list) and len(v) > MAX_ROWS for v in (s.get('exits'), entry_stage.get('entries'), scan.get('refused'), rec.get('reasons')))}


def _load_cycles(path, warnings):
    if not path.exists():
        warnings.append('No cycles.jsonl record. Controller state is unknown.')
        return [], False
    try:
        with path.open('rb') as f:
            f.seek(0, 2)
            size = f.tell()
            start = max(0, size - MAX_BYTES)
            f.seek(start)
            data = f.read(MAX_BYTES)
        if start:
            data = data.partition(b'\n')[2]  # Never parse a cut-off first record.
        if data and not data.endswith(b'\n'):
            warnings.append('Trailing cycle record is incomplete; not displayed.')
            data = data.rpartition(b'\n')[0] + b'\n' if b'\n' in data else b''
        lines = data.splitlines()
        truncated = bool(start or len(lines) > MAX_CYCLES)
        result = []
        for line in reversed(lines[-MAX_CYCLES:]):
            try:
                if len(line) > MAX_LINE:
                    raise ValueError()
                value = json.loads(line.decode('utf-8'), object_pairs_hook=_unique_object)
                if not isinstance(value, dict) or not isinstance(value.get('stages'), dict):
                    raise ValueError()
                cid = value.get('cycle_id')
                if not isinstance(cid, str) or not re.fullmatch(r'[a-f0-9]{32}', cid):
                    cid = 'legacy-' + hashlib.sha256(line).hexdigest()[:24]
                    warnings.append('Legacy record has no valid controller cycle ID; journal linkage unavailable.')
                result.append((cid, value))
            except (ValueError, UnicodeError, RecursionError):
                warnings.append('Malformed or oversized cycle record skipped; history is incomplete.')
        duplicates = {cid for cid, n in Counter(cid for cid, _ in result).items() if n > 1}
        if duplicates:
            warnings.append('Duplicate cycle IDs omitted; no ambiguous history or journal linkage displayed.')
        return [(cid, r) for cid, r in result if cid not in duplicates], truncated
    except OSError:
        warnings.append('Cycle history unreadable. Controller state is unknown.')
        return [], False


def read_state(runtime: Path, *, now: datetime):
    warnings = []
    raw, truncated = _load_cycles(runtime / 'cycles.jsonl', warnings)
    db = None
    journal_path = runtime / 'orders.sqlite3'
    try:
        if raw:
            if journal_path.exists():
                db = sqlite3.connect(journal_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.2)
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA query_only=ON')
            else:
                warnings.append('Order journal unavailable; proposal economics and subsequent status are unknown.')
        try:
            cycles = [project_cycle(r, cycle_id=cid, db=db) for cid, r in raw]
        except (sqlite3.Error, TypeError, ValueError):
            warnings.append('Order journal unreadable; showing cycle evidence without journal enrichment.')
            cycles = [project_cycle(r, cycle_id=cid) for cid, r in raw]
    except sqlite3.Error:
        warnings.append('Order journal unreadable; showing cycle evidence without journal enrichment.')
        cycles = [project_cycle(r, cycle_id=cid) for cid, r in raw]
    finally:
        if db is not None:
            db.close()
    state = {"schema_version": 1, "cycles": cycles, "warnings": list(dict.fromkeys(warnings)),
             "history_truncated": truncated, "history_limit": MAX_CYCLES, "limitations": LIMITATIONS}
    state['cursor'] = hashlib.sha256(json.dumps(state, sort_keys=True, allow_nan=False).encode()).hexdigest()
    state['read_at'] = now.astimezone(timezone.utc).isoformat()
    return state


def route(path):
    """Recognize only bounded read routes; no arbitrary filenames or write verbs."""
    if len(path) > 240:
        return None
    u = urlsplit(path)
    if u.fragment or u.scheme or u.netloc:
        return None
    if u.path == '/api/agent-desk/state':
        q = parse_qs(u.query, keep_blank_values=True)
        if not q:
            return ('state', None)
        if set(q) == {'after'} and len(q['after']) == 1 and re.fullmatch(r'[a-f0-9]{64}', q['after'][0]):
            return ('state', q['after'][0])
    if not u.query:
        if u.path == '/api/agent-desk/cycles':
            return ('cycles', None)
        prefix = '/api/agent-desk/cycles/'
        if u.path.startswith(prefix) and ID.fullmatch(u.path[len(prefix):]):
            return ('cycle', u.path[len(prefix):])
    return None


def response(state, kind, value):
    if kind == 'state':
        return {"unchanged": True, "cursor": state['cursor'], "read_at": state['read_at']} if value == state['cursor'] else state
    if kind == 'cycles':
        return {"cycles": [{k: c[k] for k in ('id', 'started_at', 'completed_at', 'status', 'control_mode_at_cycle')} for c in state['cycles']],
                "cursor": state['cursor'], "history_truncated": state['history_truncated'], "warnings": state['warnings']}
    return next((c for c in state['cycles'] if c['id'] == value), None)
