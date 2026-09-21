"""Read-only, offline paper workspace. Local JS chat; no broker calls or secrets.

Reads local runtime reports and journals; writes one self-contained HTML file.
Names are display roles, not independent traders. Data is always HTML-escaped.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import html
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from .dashboard_style import CSS as BASE_CSS
from .dashboard_chat import ART, avatar_uri, render_chat
from .agent_desk import read_state as read_desk
from .agent_desk_view import CSS as DESK_CSS, empty_state, render_desk
from .executor.eastern import eastern_date
from .gateway import control_mode
from .scanner.scan import PLAYBOOKS
from .research_scan import read_reports
from .earnings import MAX_CHECK_AGE_DAYS, load as load_earnings

AGENTS = (
    ("houston", "Houston", "Executor", "Checks broker records, sends approved paper orders and manages exits."),
    ("star", "Star", "Scanner", "Looks for setups and proposes ideas. Cannot place orders."),
    ("moon", "Moon", "Rules Engine", "Checks cash, position limits and risk. Same inputs, same decision."),
    ("astra", "Astra", "Dashboard & Notifications", "Explains what happened and surfaces alerts. Read-only."),
)
CSS = BASE_CSS + DESK_CSS + "\n".join(
    f'#agent-{key}:checked ~ main [data-agent]:not([data-agent="{key}"]){{display:none}}'
    for key, _, _, _ in AGENTS
)


def _e(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _records(value):
    return [r for r in _list(value) if isinstance(r, dict)]


def _warning(issues, message):
    if issues is not None and message not in issues:
        issues.append(message)


def _rows(db_path: Path, sql: str, params=(), *, issues=None) -> list:
    if not db_path.exists():
        _warning(issues, f"{db_path.name}: no local journal yet; values are unknown.")
        return []
    db = None
    try:
        db = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute(sql, params).fetchall()]
    except sqlite3.Error:
        _warning(issues, f"{db_path.name}: could not read journal; values are unknown.")
        return []
    finally:
        if db is not None:
            db.close()


def _jsonl(path: Path, limit: int, *, issues=None) -> list:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        _warning(issues, f"{path.name}: report unreadable.")
        return []
    out = []
    for line in reversed(lines[-limit:]):
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("object required")
            out.append(row)
        except ValueError:
            _warning(issues, f"{path.name}: malformed record skipped; history may be incomplete.")
    return out


def _json(path: Path, *, issues=None):
    if not path.exists():
        return None
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(row, dict):
            raise ValueError("object required")
        return row
    except (OSError, ValueError):
        _warning(issues, f"{path.name}: report unreadable or malformed.")
        return None


def _stamp(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp.astimezone(timezone.utc) if stamp.tzinfo else None
    except ValueError:
        return None


def _t(stamp, today) -> str:
    """UTC timestamps with an explicit date to avoid ET/UTC day ambiguity."""
    when = _stamp(stamp)
    return when.strftime("%Y-%m-%d %H:%M:%S UTC") if when else str(stamp or "never")


def _age(stamp, now, seconds=60):
    when = _stamp(stamp)
    if when is None:
        return "Unknown age", "warn"
    elapsed = (now - when).total_seconds()
    if elapsed < 0:
        return "Future timestamp — check clock", "bad"
    if elapsed > seconds:
        return "Stale snapshot", "warn"
    return "Recent at render time", "ok"


def _pill(text, cls) -> str:
    return f'<span class="pill {cls}">{_e(text)}</span>'


def _empty(message):
    return f'<p class="empty">{_e(message)}</p>'


def _command(command):
    return f'<pre><code>{_e(command)}</code></pre>'


def _panel(agent, title, body, *, wide=False, collapsed=False):
    name = next(name for key, name, _, _ in AGENTS if key == agent)
    if collapsed:
        body = f'<details><summary>Show technical details</summary>{body}</details>'
    anchor = '-'.join(title.lower().replace('&', '').split())
    return (f'<section id="{anchor}" data-agent="{agent}" class="{"wide" if wide else ""}">'
            f'<span class="agent-owner">{name}</span><h2>{_e(title)}</h2>{body}</section>')


class Html(str):
    """Marker for already-escaped fragments."""


def _table(headers, rows, numeric=(), *, empty="No records yet."):
    if not rows:
        return _empty(empty)
    head = "".join(f'<th scope="col">{_e(h)}</th>' for h in headers)
    body = []
    for row in rows:
        cells = "".join(f'<td class="{"num" if i in numeric else ""}">{cell if isinstance(cell, Html) else _e(cell)}</td>'
                        for i, cell in enumerate(row))
        body.append(f"<tr>{cells}</tr>")
    return f'<div class="table-scroll" tabindex="0" role="region" aria-label="Scrollable data table"><table><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def collect(runtime: Path, *, now: datetime) -> dict:
    today, issues = eastern_date(now), []
    control = control_mode(runtime / "trading-control")
    approvals = {}
    for name in PLAYBOOKS:
        try:
            approvals[name] = (runtime / "playbooks" / f"{name}.approved").read_text(encoding="utf-8").strip() == "APPROVED"
        except (OSError, UnicodeError):
            approvals[name] = False
    fills_db, orders_db = runtime / "fills.sqlite3", runtime / "orders.sqlite3"
    day = today.isoformat()
    def rows(path, sql, params=()):
        return _rows(path, sql, params, issues=issues)
    pnl_rows = rows(fills_db, "SELECT pnl_units FROM option_fills WHERE trading_day=?", (day,))
    fee_rows = rows(fills_db, "SELECT amount_units FROM option_fees WHERE trading_day=?", (day,))
    pnls = [r["pnl_units"] for r in pnl_rows]
    fees = sum(r["amount_units"] for r in fee_rows)
    loss = (sum(-p for p in pnls if p < 0) + fees) / Decimal(1_000_000)
    net = (sum(pnls) - fees) / Decimal(1_000_000)
    latched = bool(rows(fills_db, "SELECT 1 FROM fill_breakers WHERE trading_day=?", (day,)))
    inventory = rows(fills_db, """SELECT l.position_id, SUM(l.quantity) qty, SUM(l.basis_units) basis,
        (SELECT json_extract(payload,'$.contract') FROM option_fills f WHERE f.position_id=l.position_id ORDER BY seq LIMIT 1) contract,
        (SELECT trading_day FROM option_fills f WHERE f.position_id=l.position_id ORDER BY seq LIMIT 1) opened
        FROM option_lots l WHERE l.quantity>0 GROUP BY l.position_id ORDER BY MIN(l.seq)""")
    closed = rows(fills_db, """SELECT trading_day, json_extract(payload,'$.contract') contract, pnl_units,
        json_extract(payload,'$.side') side FROM option_fills WHERE json_extract(payload,'$.side')='sell_to_close'
        ORDER BY seq DESC LIMIT 15""")
    intents = rows(orders_db, """SELECT created_at, kind, status, symbol, contract, cost, reason, broker_order_id
        FROM order_intents ORDER BY created_at DESC LIMIT 25""")
    # Outstanding orders must not disappear behind the recent-history limit.
    live = rows(orders_db, "SELECT * FROM order_intents WHERE status IN ('reserved','claimed')")
    events = rows(orders_db, "SELECT timestamp, event, payload FROM order_events ORDER BY sequence DESC LIMIT 20")
    cycles = _jsonl(runtime / "cycles.jsonl", 12, issues=issues)
    notifications = _jsonl(runtime / "notifications.jsonl", 15, issues=issues)
    shadow = _json(runtime / "shadow-scan.json", issues=issues) or {}
    traces = rows(runtime / "api-requests.sqlite3", """SELECT started_at, method, path, status, outcome FROM api_requests
        WHERE method='POST' OR started_at >= (SELECT MIN(started_at) FROM (SELECT started_at FROM api_requests ORDER BY started_at DESC LIMIT 8))
        ORDER BY started_at DESC LIMIT 40""")
    shadow_counts = {name: 0 for name in PLAYBOOKS}
    for idea in _records(shadow.get("shadow")):
        if idea.get("playbook") in shadow_counts:
            shadow_counts[idea["playbook"]] += 1
    research = []
    for path in sorted(runtime.glob("bt-*.json")):
        report = _json(path, issues=issues)
        if report is not None:
            if report.get("level") != "underlying" or report.get("options_pnl_modelled") is not False or not isinstance(report.get("summary"), dict):
                _warning(issues, f"{path.name}: unsupported research report; not displayed.")
            else:
                research.append({**report, "file": path.name})
    calendar = load_earnings(runtime / "earnings.json", today=today)
    earnings = {"file": calendar["file"], "verified": [{"symbol": s, "date": d.isoformat()} for s, d in sorted(calendar["verified"].items())],
                "issues": calendar["issues"]}
    return {"now": now, "today": today, "control": control, "approvals": approvals, "daily_loss": loss, "daily_net": net, "earnings": earnings,
            "latched": latched, "inventory": inventory, "closed": closed, "intents": intents, "live": live,
            "order_events": events, "cycles": cycles, "notifications": notifications, "shadow": shadow,
            "shadow_counts": shadow_counts, "traces": traces, "research": research, "issues": issues,
            "research_scans": read_reports(runtime),
            "ledger_known": not any(i.startswith(fills_db.name + ":") for i in issues),
            "orders_known": not any(i.startswith(orders_db.name + ":") for i in issues)}


def next_action(d, rec):
    """Presentation advice only. Never grants authorization or edits runtime state."""
    if d["inventory"] and d["control"] == "DISABLED":
        return ("Positions are not being managed", "DISABLED blocks automatic exits too. Review held positions in Alpaca and decide how to manage them. Do not leave them unattended.", "python -m alpaca_agents.executor reconcile", "bad")
    reasons = _list(rec.get("reasons"))
    if rec.get("error") or any(str(r) not in ("MARKET_CLOSED", "NOT_A_SESSION") for r in reasons):
        return ("Resolve the broker/risk blocker", "Review Moon's blockers and reconcile again. Do not bypass checks or retry an uncertain order manually.", "python -m alpaca_agents.executor reconcile", "bad")
    if d["issues"]:
        return ("Check missing or unreadable records", "Some local data is unavailable. Unknown values are not zero balances. See the data-quality details below.", "python -m alpaca_agents.executor reconcile", "warn")
    if d["latched"] or d["daily_loss"] >= 40:
        return ("Pause new entries", "The daily loss breaker has been reached. Review losses; do not reset the ledger. Existing positions still need monitoring.", "python -m alpaca_agents.executor intents", "bad")
    errors = _records(d["shadow"].get("errors"))
    if errors:
        forbidden = any("403" in str(e.get("reason")) for e in errors)
        return ("Options data needs attention", "Options access was denied (403). Check options-snapshot and realtime-quote entitlements; Stocks Advanced alone does not provide them. Historical stock research can continue." if forbidden else "Star could not validate one or more market snapshots. Review the scan errors; zero ideas does not mean zero setups.", "Get-Content runtime\\shadow-scan.json", "warn")
    if not rec:
        return ("Capture your first controller snapshot", "Standalone account/reconcile commands do not populate this dashboard's cycle history. With broker and market-data keys loaded, run one cycle without --submit. No orders are sent.", "python -m alpaca_agents.controller --runtime runtime --dashboard", "warn")
    if reasons:
        return ("Market closed — research time", "The last cycle reported a closed session. Review research now and reconcile again during market hours; this is not a live market clock.", "python -m alpaca_agents.executor reconcile", "dim")
    last = d["cycles"][0] if d["cycles"] else {}
    if _age(last.get("started_at"), d["now"])[1] != "ok":
        return ("Refresh the controller snapshot", "The saved check is stale or its age is unknown. Run a fresh dry cycle; an old successful check does not mean the account is ready now.", "python -m alpaca_agents.controller --runtime runtime --dashboard", "warn")
    if d["control"] == "DISABLED":
        return ("Research mode — trading disabled", "Review Star's backtests and data access. Keep unvalidated playbooks disabled; a positive historical average is not an approval.", "python -m alpaca_agents.dashboard", "dim")
    if d["control"] == "EXITS_ONLY":
        return ("Monitor existing positions", "New entries are disabled. Exit submission still requires a running controller with --submit and valid broker checks.", "python -m alpaca_agents.executor intents", "warn")
    return ("Review before the next cycle", "ARMED_PAPER is permission, not proof that a controller is running. Check cycle age, playbook enablement and the last submission setting.", "python -m alpaca_agents.executor reconcile", "warn")


def _number(value, places=2):
    try:
        if isinstance(value, bool):
            return "—"
        number = Decimal(str(value))
        return f"{number:.{places}f}" if number.is_finite() else "—"
    except (InvalidOperation, ValueError):
        return "—"


def _research_value(value):
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except InvalidOperation:
        return None


def _research_plot(mean, median, scale):
    # Both marks share a symmetric scale across every study. Missing is not zero.
    if mean is None or median is None:
        return Html('<span class="dim">Unknown</span>')
    x1, x2 = (float(Decimal(55) + value / scale * 45) for value in (mean, median))
    label = f'Mean {mean:.2f} R; median {median:.2f} R. Center line is zero.'
    return Html(f'<svg class="r-chart" viewBox="0 0 110 32" role="img" aria-label="{_e(label)}">'
                '<path d="M10 16H100" stroke="#49404f"/><path d="M55 4V28" stroke="#797080" stroke-dasharray="2 2"/>'
                f'<path d="M{x1:.2f} 16H{x2:.2f}" stroke="#9c80a7" stroke-width="2"/>'
                f'<circle cx="{x1:.2f}" cy="16" r="4" fill="#d39acc"/>'
                f'<circle cx="{x2:.2f}" cy="16" r="3" fill="#8fe1c7" stroke="#19191e"/></svg>')


def _research(reports):
    studies = [(report, playbook, _dict(summary)) for report in reports
               for playbook, summary in _dict(report.get("summary")).items()]
    values = [_research_value(s.get(key)) for _, _, s in studies for key in ("expectancy_r", "median_r")]
    scale = max([Decimal(1)] + [abs(v) for v in values if v is not None])
    rows = []
    for report, playbook, s in studies:
        study = Html(f'<strong class="research-study">{_e(report.get("symbol"))}<small>{_e(playbook)}</small></strong>')
        rows.append((study, _research_plot(_research_value(s.get("expectancy_r")), _research_value(s.get("median_r")), scale),
                     _number(s.get("expectancy_r"), 2), _number(s.get("median_r"), 2),
                     _number(s.get("profit_factor"), 2), _number(s.get("max_loss_r"), 1), s.get("resolved")))
    sources = ''.join(f'<li><strong>{_e(report.get("symbol"))} · {_e(playbook)}</strong>: '
                      f'{_e(report.get("first_bar", "?"))} → {_e(report.get("last_bar", "?"))} · '
                      f'{_e(s.get("no_trade", "Unknown"))} no trade · '
                      f'{"Count threshold met; not statistical proof" if s.get("sample_sufficient") is True else "Small / unknown sample"} · '
                      f'{_e(report.get("file"))}</li>' for report, playbook, s in studies)
    return _panel("star", "Research lab", '<p class="research-warning">Underlying-price research only · options P&amp;L is NOT modeled.</p>' +
                  (f'<div class="research-legend"><span><i></i>Mean R</span><span><i class="median-key"></i>Median R</span>'
                   f'<span>Shared axis: −{scale:.2f} to +{scale:.2f} R · center = 0</span></div>' if studies else '') +
                  _table(["Study", "Mean / median", "Mean R", "Median R", "Profit factor", "Worst R", "Resolved"],
                         rows, numeric={2, 3, 4, 5, 6},
                         empty="No backtest reports yet. Save reports as runtime/bt-SYMBOL.json to compare them here.") +
                  (f'<details><summary>Sources, coverage &amp; sample notes</summary><ul class="small dim">{sources}</ul></details>' if studies else '') +
                  '<p class="dim small">R measures the underlying move relative to the modeled stop distance, not dollars earned on an option. '
                  'Fees, spreads, IV and time decay are absent. Mean R is dominated by gap outliers: when the median is negative while the mean is positive, '
                  'a few trades carry the result. Win/loss is the sign of R; "no trade" counts fills already past the stop or target that a rational '
                  'executor would skip. Test out-of-sample, then validate option-level execution. No result here approves a playbook or proves an edge.</p>', wide=True)


def render(d: dict, *, studio=None, desk=None) -> str:
    desk = desk or empty_state()
    t = lambda stamp: _t(stamp, d["today"])
    last = d["cycles"][0] if d["cycles"] else {}
    stages = _dict(last.get("stages"))
    rec = _dict(stages.get("reconcile"))
    freshness, fresh_cls = _age(last.get("started_at"), d["now"])
    title, advice, command, action_cls = next_action(d, rec)
    control_cls = {"ARMED_PAPER": "warn", "EXITS_ONLY": "warn"}.get(d["control"], "dim")
    mode_text = {"ARMED_PAPER": "Paper entries + exits permitted", "EXITS_ONLY": "No new entries; exits permitted"}.get(d["control"], "Automatic entries and exits disabled")
    loss_cls = "bad" if d["latched"] or d["daily_loss"] >= 40 else "dim"
    ledger_known = d["ledger_known"]
    net = f"${d['daily_net']:.2f}" if ledger_known else "Unknown"
    loss = f"${d['daily_loss']:.2f} / $40.00" if ledger_known else "Unknown"
    positions_count = f"{len(d['inventory'])} / 2" if ledger_known else "Unknown"
    rec_label = "No controller check yet" if not rec else "Passed at last check" if rec.get("ok") is True else "Blocked at last check"
    rec_cls = "ok" if rec.get("ok") is True and fresh_cls == "ok" else "warn"
    status = f'''<section class="wide hero" id="overview"><div class="section-top"><div><span class="eyebrow">Account overview · options only</span><h2 class="hero-title">Paper trading</h2></div>{_pill(d['control'], control_cls)}</div>
<p class="dim">{mode_text}. One shared account and risk budget across all four roles.</p>
<div class="metrics"><div class="metric"><span>Recorded net today · ET</span><strong>{net}</strong></div>
<div class="metric"><span>Cumulative daily loss</span><strong class="{loss_cls}">{loss}</strong><span>{'Breaker latched' if d['latched'] else 'Breaker is not a maximum-loss guarantee'}</span></div>
<div class="metric"><span>Recorded open positions</span><strong>{positions_count}</strong></div>
<div class="metric"><span>Outstanding intents</span><strong>{len(d['live']) if d['orders_known'] else 'Unknown'}</strong></div></div>
<div class="section-top"><p>{_pill(rec_label, rec_cls)} {_pill(freshness, fresh_cls)}</p><span class="dim small">Last cycle: {_e(t(last.get('finished_at')))}</span></div>
<p class="dim small">Last cycle submission: {'enabled (paper)' if last.get('submit') is True else 'dry run' if last else 'unknown'}. Snapshot date: {_e(d['today'])} (ET). Controller running status is not verified.</p>
<div class="next-action"><div><span class="eyebrow">Next step</span><p><strong class="{action_cls}">{_e(title)}</strong></p></div><div><p>{_e(advice)}</p><details><summary>Show diagnostic command</summary>{_command(command)}</details></div></div>
<p class="dim small">Snapshot only. Age is calculated when rendered, not continuously. Refreshing this file does not fetch broker data.</p></section>'''

    panels = []
    agent_states = {
        "houston": rec_label,
        "star": "Snapshot errors recorded" if _records(d["shadow"].get("errors")) else "Shadow report available" if d["shadow"] else "No shadow scan yet",
        "moon": "Breaker latched" if d["latched"] else "Risk checks at each decision",
        "astra": "Local snapshot · not a live feed",
    }
    for key, name, role, description in AGENTS:
        panels.append(f'<section class="agent-summary agent-{key}" data-agent="{key}">'
                      f'<div class="avatar-stage"><img src="{avatar_uri(key)}" alt="{name} · {ART[key]}" width="110" height="124"></div>'
                      f'<span class="eyebrow">{_e(role)}</span><h2>{name}</h2><p class="role">{_e(agent_states[key])}</p>'
                      f'<button class="talk-button" type="button" data-chat-agent="{key}" disabled>Talk to {name} <span aria-hidden="true">↗</span></button></section>')
    crew = ''.join(panels)
    panels = [_research(d["research"])]

    reasons = _list(rec.get("reasons"))
    risk = '<dl><dt>Maximum entry risk</dt><dd>$100 including estimated fees</dd><dt>Concurrent positions</dt><dd>2</dd><dt>Daily loss breaker</dt><dd>$40 cumulative losses; wins do not offset</dd></dl>'
    risk += f'<p class="dim small">Last cycle spendable-cash bound: {_e(_number(rec.get("settled_cash")))} USD · recorded, not a live balance.</p>'
    risk += '<p class="dim small">Limits are system-wide. The breaker blocks new entries, not losses on existing positions. Approval markers alone do not enable trading.</p>'
    if reasons or rec.get("error"):
        risk += '<h3>Latest reconciliation blockers</h3><ul class="reasons">' + ''.join(f'<li>{_e(r)}</li>' for r in reasons + ([rec["error"]] if rec.get("error") else [])) + '</ul>'
    else:
        risk += _empty("No blockers reported in the latest cycle. This is not an authorization." if rec else "No controller reconciliation snapshot yet.")
    panels.append(_panel("moon", "Risk checks", risk))
    positions = _table(["Contract", "Qty", "Recorded basis", "Opened"],
                       [(r["contract"], r["qty"], f"${Decimal(r['basis']) / 1_000_000:.2f}", r["opened"]) for r in d["inventory"]], numeric={1, 2},
                       empty="No open positions in the local ledger." if ledger_known else "Position ledger unavailable. Check the broker before drawing conclusions.")
    panels.append(_panel("houston", "Open positions", positions + '<p class="dim small">No live marks or unrealized P&amp;L are fetched by this page.</p>'))
    playbooks = _table(["Playbook", "Approval file", "Shadow ideas"],
                       [(name, Html(_pill("APPROVED", "warn") if d["approvals"][name] else _pill("shadow only", "dim")), d["shadow_counts"][name]) for name in PLAYBOOKS], numeric={2})
    panels.append(_panel("star", "Playbooks", playbooks + '<p class="dim small">Approval is not enablement. The controller also requires --enable-playbook, an armed control mode, valid checks and --submit. Debit-spread execution is not implemented.</p>'))
    shadow = d["shadow"]
    scan_age, scan_cls = _age(shadow.get("generated_at"), d["now"], seconds=120)
    errors = _table(["Symbol", "Why data was rejected"], [(r.get("symbol"), r.get("reason")) for r in _records(shadow.get("errors"))], empty="No snapshot errors recorded in this report.")
    ideas = _table(["Symbol", "Playbook", "Thesis"], [(r.get("symbol"), r.get("playbook"), r.get("thesis")) for r in _records(shadow.get("shadow"))], empty="No shadow ideas recorded. Check errors and report age before interpreting this as no setups.")
    scan_body = (f'<p>{_pill(scan_age, scan_cls)} <span class="dim small">{_e(t(shadow.get("generated_at")))}</span></p>' + errors + ideas) if shadow else _empty("Run a shadow scan with realtime options access. Stocks Advanced supports stock research, not realtime option selection.")
    panels.append(_panel("star", "Scan health & ideas", scan_body))
    cal = _dict(d.get("earnings"))
    verified_rows = [(r.get("symbol"), r.get("date")) for r in _records(cal.get("verified"))]
    issue_rows = [(r.get("symbol"), r.get("reason")) for r in _records(cal.get("issues"))]
    cal_body = (f'<p>{_pill(f"{len(verified_rows)} verified", "ok" if verified_rows else "dim")} '
                f'{_pill(f"{len(issue_rows)} excluded", "warn" if issue_rows else "dim")} '
                f'<span class="dim small">file: {_e(cal.get("file", "unknown"))}</span></p>'
                '<p class="dim small">Single stocks are scanned only with a date you verified on the company\'s investor-relations page. '
                f'Entries expire {MAX_CHECK_AGE_DAYS} days after checking; past dates are excluded until the next one is entered. '
                'Index ETFs need no entry. A model\'s guess is not a source.</p>'
                + _table(["Stock", "Next earnings (verified)"], verified_rows, empty="No verified stocks: only index ETFs can be scanned.")
                + _table(["Stock", "Why excluded"], issue_rows, empty="No excluded entries.")
                + _command("python -m alpaca_agents.earnings set AAPL 2026-10-30 --source investor.apple.com"))
    panels.append(_panel("star", "Verified earnings calendar", cal_body))
    profiles = []
    for style in ('scalp', 'swing'):
        report = d.get('research_scans', {}).get(style)
        body = _empty('No readable research report. This does not start scanning or change trading mode.')
        if report:
            age, cls = _age(report.get('generated_at'), d['now'], seconds=120 if style == 'scalp' else 86400)
            records = _records(report.get('rows'))
            records = sorted(records, key=lambda r: r.get('status') != 'setup')
            body = (f'<p>{_pill(age, cls)} {_e(report.get("generated_at"))} · '
                    f'{_e(report.get("status"))} · observed {_e(report.get("observed"))}/{_e(report.get("requested"))}</p>'
                    '<p class="dim small">Latest pass, not a live feed. Each row has its own observation time. Showing up to 30 rows; setups first.</p>' +
                    _table(['Underlying', 'Observed / bar', 'Result', 'Technique / bias', 'Underlying entry / stop / projected target', 'Reason'],
                           [(r.get('symbol'), f'{r.get("observed_at")} / {r.get("bar_at")}', r.get('status'),
                             f'{r.get("technique")} / {r.get("direction")}',
                             f'{r.get("entry")} / {r.get("stop")} / {r.get("target")}', r.get('reason')) for r in records[:30]],
                           empty='No observations in this report; setups unknown.'))
        profiles.append(f'<div data-research-profile="{style}"><h3>{style.title()} research</h3>{body}</div>')
    research_body = ('<label for="research-profile">Research view only <select id="research-profile" disabled>'
                     '<option value="scalp">Scalp</option><option value="swing">Swing</option></select></label>'
                     '<p class="notice">Underlying research only — NOT an options proposal or a trading-mode switch. '
                     'No contracts selected, no option liquidity checks, no new order path. Stocks require verified earnings and instrument data. '
                     'Scalp execution and same-day exit policy are not implemented. Refresh the page to load new reports.</p>' + ''.join(profiles))
    panels.append(_panel('star', 'Scalp & swing research', research_body, wide=True))

    closed = _table(["Day", "Contract", "Booked fill P&L"],
                    [(r["trading_day"], r["contract"], Html(f'<span class="{"ok" if r["pnl_units"] >= 0 else "bad"}">${Decimal(r["pnl_units"]) / 1_000_000:.2f}</span>')) for r in d["closed"]], numeric={2},
                    empty="No closing fills recorded yet. Backtest returns are separate from paper results.")
    panels.append(_panel("houston", "Closed trades", closed + '<p class="dim small">Latest 15 closing fills, including partial closes; not necessarily 15 complete trades. Separately booked fees appear in the daily net.</p>'))
    notes = _table(["When · UTC", "Level", "Notification"], [(t(n.get("timestamp")), Html(_pill(n.get("level"), {"info": "dim", "warning": "warn"}.get(n.get("level"), "bad"))), n.get("title")) for n in d["notifications"]], empty="No notifications recorded. This does not prove the controller is running.")
    panels.append(_panel("astra", "Notifications", notes))
    workflow = '<ol><li><strong>Before a session:</strong> load keys locally and reconcile. Stop on unexplained blockers.</li><li><strong>Research:</strong> compare historical reports; keep unvalidated playbooks disabled.</li><li><strong>After a session:</strong> review fills, errors and exit reasons. Record surprises before changing any rules.</li></ol>'
    workflow += '<details><summary>Safe PowerShell commands (no order submission)</summary>' + _command("python -m alpaca_agents.executor reconcile\npython -m alpaca_agents.executor intents\npython -m alpaca_agents.dashboard\nStart-Process runtime\\dashboard.html") + '</details>'
    panels.append(_panel("astra", "Your daily routine", workflow))
    modes = '<dl><dt>DISABLED</dt><dd>No automatic entries or exits.</dd><dt>EXITS_ONLY</dt><dd>No new entries. Exits need --submit and a running controller.</dd><dt>ARMED_PAPER</dt><dd>Entries and exits permitted, but still subject to all checks.</dd></dl><p class="notice">Closing PowerShell may stop the controller. Dashboard filters do not control trading. Do not leave open positions assuming this page is managing them.</p>'
    panels.append(_panel("moon", "Understand your controls", modes))

    intents = _table(["Time · UTC", "Kind", "Status", "Contract", "Cost", "Broker ID", "Reason"],
                     [(t(r["created_at"]), r["kind"], Html(_pill(r["status"], "warn" if r["status"] in ("reserved", "claimed") else "dim")), r["contract"] or r["symbol"], r["cost"], r["broker_order_id"] or "", r["reason"]) for r in d["intents"]])
    panels.append(_panel("houston", "Order intents", intents, wide=True, collapsed=True))
    def stage_summary(c):
        s = _dict(c.get("stages"))
        r = _dict(s.get("reconcile"))
        e = _dict(s.get("entries"))
        parts = ["reconciled" if r.get("ok") is True else "unreconciled"]
        if "halt" in s:
            parts.append("HALT: " + str(_dict(s["halt"]).get("reason", "unknown")))
        if e.get("skipped") or e.get("error"):
            parts.append(str(e.get("skipped") or e.get("error")))
        if "scan" in s:
            parts.append(f"shadow ideas: {_dict(s['scan']).get('shadow', '?')}")
        return "; ".join(parts)
    cycles = _table(["Finished · UTC", "Mode", "Submission", "Summary"], [(t(c.get("finished_at")), c.get("control_mode"), "paper enabled" if c.get("submit") is True else "dry run", stage_summary(c)) for c in d["cycles"]], empty="No controller cycles recorded. Standalone reconcile output is not cycle history.")
    panels.append(_panel("astra", "Cycles", cycles, wide=True, collapsed=True))
    events = _table(["Time · UTC", "Event"], [(t(e["timestamp"]), e["event"]) for e in d["order_events"]])
    panels.append(_panel("houston", "Journal events", events, collapsed=True))
    traces = _table(["Started · UTC", "Method", "Path", "Status", "Outcome"], [(t(x["started_at"]), x["method"], x["path"], x["status"], x["outcome"]) for x in d["traces"]])
    panels.append(_panel("houston", "Broker requests", traces, collapsed=True))
    issues = '<ul>' + ''.join(f'<li>{_e(i)}</li>' for i in d["issues"]) + '</ul>' if d["issues"] else _empty("No local read errors detected. This is not verification of broker state.")
    # Kept global so filtering cannot hide source-data failures.
    status += f'<aside class="wide notice" style="grid-column:1/-1"><details{" open" if d["issues"] else ""}><summary>Data quality · {len(d["issues"])} local warnings</summary>{issues}</details></aside>'
    filters = '<input class="agent-filter" type="radio" name="agent" id="agent-all" checked><label class="agent-tab" for="agent-all">All agents</label>'
    for key, name, _, _ in AGENTS:
        filters += f'<input class="agent-filter" type="radio" name="agent" id="agent-{key}"><label class="agent-tab" for="agent-{key}">{name}</label>'
    chat, scripts, csp = render_chat(d, AGENTS, studio=studio, desk=desk)
    footer = ('Local snapshot · no broker requests from this page · no live-money orders. Regenerate with '
              '<code>python -m alpaca_agents.dashboard</code>, then refresh your browser. No API keys belong on this page.')
    if studio is not None:
        footer = ('Local server · chat reads runtime records, not live quotes. Refresh for updated panels. '
                  'Houston’s exact <code>reconcile</code> command requests a dry controller diagnostic. No chat orders. '
                  'Keep this local session private; never paste broker or data keys.')
        status = status.replace('Refreshing this file does not fetch broker data.',
                                'Refreshing reads local records, not the broker. Chat rereads records for each question.')
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Mission Control · AlpacaAgent paper</title>
<meta http-equiv="Content-Security-Policy" content="{_e(csp)}">
<meta name="viewport" content="width=device-width,initial-scale=1"><style>{CSS}</style></head><body>
<a class="skip-link" href="#overview">Skip to overview</a>
<aside class="sidebar" aria-label="Workspace navigation">
<a class="wordmark" href="#overview" aria-label="Alpaca overview"><span class="brand-mark" aria-hidden="true">a/</span>alpaca<span class="wordmark-dot">.</span></a>
<div class="workspace-label"><span class="workspace-icon" aria-hidden="true">P</span><div>Personal workspace<small>Options · paper account</small></div></div>
<nav aria-label="Dashboard"><span class="nav-label">Workspace</span>
<a href="#overview" aria-current="location"><span aria-hidden="true">▦</span>Overview</a>
<a href="#agent-desk"><span aria-hidden="true">⠿</span>Agent Desk</a>
<a href="#open-positions"><span aria-hidden="true">▤</span>Positions</a>
<a href="#research-lab"><span aria-hidden="true">↗</span>Research</a>
<a href="#scan-health-ideas"><span aria-hidden="true">⌕</span>Scanner</a>
<a href="#scalp-swing-research"><span aria-hidden="true">≋</span>Scan profiles</a>
<a href="#verified-earnings-calendar"><span aria-hidden="true">▣</span>Earnings</a>
<a href="#risk-checks"><span aria-hidden="true">◇</span>Risk &amp; limits</a>
<a href="#cycles"><span aria-hidden="true">≡</span>Activity</a>
<a href="#agent-chat"><span aria-hidden="true">◌</span>Chat</a>
</nav><div class="sidebar-foot"><span class="paper-dot" aria-hidden="true"></span>Paper environment<p>No live-money orders.<br>Controls are read-only.</p></div></aside>
<div class="app-shell"><header><div><span class="eyebrow">Workspace / Overview</span><h1>Dashboard</h1></div><div class="header-actions"><div class="meta"><span class="snapshot-label">Local snapshot</span><p>Rendered {_e(t(d['now']))}</p></div>{_pill('PAPER ONLY · READ-ONLY', 'warn')}<a class="chat-jump" href="#agent-chat">Open chat <span aria-hidden="true">↗</span></a></div></header>
<div class="workspace"><fieldset class="agent-picker"><legend>Filter by role · changes the view, never trading permissions.</legend>{filters}<main>{status}{crew}{render_desk(desk)}{''.join(panels)}</main></fieldset>{chat}</div>
<footer>{footer}</footer></div>{scripts}</body></html>'''


def build(runtime: Path, output: Path, *, now: datetime | None = None) -> Path:
    now = now or datetime.now(timezone.utc)
    page = render(collect(runtime, now=now), desk=read_desk(runtime, now=now))
    output.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=output.parent, delete=False, suffix=".tmp") as stream:
            name = stream.name
            stream.write(page)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, output)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)
    return output


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Render the static paper dashboard")
    parser.add_argument("--runtime", type=Path, default=Path("runtime"))
    parser.add_argument("--output", type=Path, default=Path("runtime/dashboard.html"))
    args = parser.parse_args()
    print(build(args.runtime, args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
