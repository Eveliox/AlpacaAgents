"""Layer 4: static HTML dashboard rendered from runtime files. No server, no JS, no secrets.

Reads (all read-only): orders journal, fills ledger, cycles.jsonl,
notifications.jsonl, shadow-scan.json, trading-control, playbook approvals,
api-requests trace. Writes one self-contained HTML file. Everything is
html-escaped; nothing is executed from data.
"""
from datetime import date, datetime, timezone
from decimal import Decimal
import html
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from .executor.eastern import eastern_date
from .gateway import control_mode
from .scanner.scan import PLAYBOOKS

# Display identities only: these do not change playbook IDs or trading permissions.
AGENTS = (
    ("houston", "Houston", "Executor", "Orders, positions and the exclusive broker boundary."),
    ("star", "Star", "Scanner", "Market-data ideas and playbooks. No broker access."),
    ("moon", "Moon", "Rules Engine", "Deterministic risk validation. No I/O or LLM calls."),
    ("astra", "Astra", "Dashboard & Notifications", "Read-only monitoring, cycle reports and alerts."),
)

CSS = """
:root{--bg:#1d1f22;--panel:#26292e;--line:#34383f;--fg:#d9d6cf;--dim:#8d8a82;--gold:#c9a24d;--red:#c96b5a;--green:#7fa96b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
header{display:flex;align-items:baseline;gap:18px;padding:14px 20px;border-bottom:1px solid var(--line)}
header h1{margin:0;font-size:15px;letter-spacing:.08em;color:var(--gold);font-weight:600}
header .meta{color:var(--dim)}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:14px;padding:14px 20px}
section{background:var(--panel);border:1px solid var(--line);border-radius:4px;padding:10px 12px}
section.wide{grid-column:1/-1}h2{margin:0 0 8px;font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--gold);font-weight:600}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:3px 6px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--dim);font-weight:normal}td.num{text-align:right;font-variant-numeric:tabular-nums}td:first-child{white-space:nowrap}section{overflow-x:auto}
.pill{display:inline-block;padding:1px 7px;border-radius:9px;font-size:11px;border:1px solid var(--line)}
.ok{color:var(--green);border-color:var(--green)}.bad{color:var(--red);border-color:var(--red)}.warn{color:var(--gold);border-color:var(--gold)}
.dim{color:var(--dim)}dl{display:grid;grid-template-columns:max-content 1fr;gap:2px 14px;margin:0}dt{color:var(--dim)}dd{margin:0}
.reasons li{color:var(--red)}ul{margin:4px 0 0 16px;padding:0}footer{padding:10px 20px;color:var(--dim);border-top:1px solid var(--line)}
.agent-picker{border:0;margin:0;padding:14px 20px 0;min-width:0}
.agent-picker legend{padding-top:12px;color:var(--dim)}
.agent-picker>main{padding:14px 0}
.agent-filter{position:absolute;width:1px;height:1px;opacity:0}
.agent-tab{display:inline-block;margin:0 6px 8px 0;padding:8px 14px;border:1px solid var(--line);border-radius:4px;cursor:pointer}
.agent-filter:checked+label{background:var(--gold);color:var(--bg);border-color:var(--gold)}
.agent-filter:focus-visible+label{outline:2px solid var(--fg);outline-offset:3px}
.agent-owner{display:block;font-size:10px;letter-spacing:.12em;color:var(--dim);margin-bottom:5px}
.agent-summary h2{font-size:15px;text-transform:none}.agent-summary p{margin:4px 0}
@media(max-width:480px){main{grid-template-columns:minmax(0,1fr)}header{flex-wrap:wrap;gap:8px}}
"""
# CSS-only radio filters keep the file self-contained and usable offline.
CSS += "\n".join(
    f'#agent-{key}:checked ~ main [data-agent]:not([data-agent="{key}"]){{display:none}}'
    for key, _, _, _ in AGENTS
)


def _panel(agent: str, title: str, body: str, *, wide=False) -> str:
    name = next(name for key, name, _, _ in AGENTS if key == agent)
    return (f'<section data-agent="{agent}" class="{"wide" if wide else ""}">'
            f'<span class="agent-owner">{name}</span><h2>{_e(title)}</h2>{body}</section>')


def _e(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _rows(db_path: Path, sql: str, params=()) -> list:
    if not db_path.exists():
        return []
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in db.execute(sql, params).fetchall()]
    except sqlite3.Error:
        return []
    finally:
        db.close()


def _jsonl(path: Path, limit: int) -> list:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out = []
    for line in reversed(lines[-limit:]):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _t(stamp, today) -> str:
    """HH:MM:SS for today's ET date, else YYYY-MM-DD HH:MM. Unknown input passes through."""
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return str(stamp or "")
    if when.tzinfo is None:
        return str(stamp)
    local = when.astimezone(timezone.utc)
    return local.strftime("%H:%M:%S") if eastern_date(local) == today else local.strftime("%Y-%m-%d %H:%M")


def _pill(text, cls) -> str:
    return f'<span class="pill {cls}">{_e(text)}</span>'


def _table(headers, rows, numeric=()) -> str:
    if not rows:
        return '<p class="dim">none</p>'
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = []
    for row in rows:
        cells = "".join(f'<td class="{"num" if i in numeric else ""}">{cell if isinstance(cell, Html) else _e(cell)}</td>'
                        for i, cell in enumerate(row))
        body.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


class Html(str):
    """Marker for already-escaped fragments."""


def collect(runtime: Path, *, now: datetime) -> dict:
    today = eastern_date(now)
    control = control_mode(runtime / "trading-control")
    approvals = {}
    for name in PLAYBOOKS:
        marker = runtime / "playbooks" / f"{name}.approved"
        try:
            approvals[name] = marker.read_text(encoding="utf-8").strip() == "APPROVED"
        except OSError:
            approvals[name] = False
    fills_db = runtime / "fills.sqlite3"
    orders_db = runtime / "orders.sqlite3"
    day = today.isoformat()
    pnl_rows = _rows(fills_db, "SELECT pnl_units FROM option_fills WHERE trading_day=?", (day,))
    fee_rows = _rows(fills_db, "SELECT amount_units FROM option_fees WHERE trading_day=?", (day,))
    pnls = [r["pnl_units"] for r in pnl_rows]
    fees = sum(r["amount_units"] for r in fee_rows)
    loss = (sum(-p for p in pnls if p < 0) + fees) / Decimal(1_000_000)
    net = (sum(pnls) - fees) / Decimal(1_000_000)
    latched = bool(_rows(fills_db, "SELECT 1 FROM fill_breakers WHERE trading_day=?", (day,)))
    inventory = _rows(fills_db, """SELECT l.position_id, SUM(l.quantity) qty, SUM(l.basis_units) basis,
        (SELECT json_extract(payload,'$.contract') FROM option_fills f WHERE f.position_id=l.position_id ORDER BY seq LIMIT 1) contract,
        (SELECT trading_day FROM option_fills f WHERE f.position_id=l.position_id ORDER BY seq LIMIT 1) opened
        FROM option_lots l WHERE l.quantity>0 GROUP BY l.position_id ORDER BY MIN(l.seq)""")
    closed = _rows(fills_db, """SELECT trading_day, json_extract(payload,'$.contract') contract, pnl_units,
        json_extract(payload,'$.side') side FROM option_fills WHERE json_extract(payload,'$.side')='sell_to_close'
        ORDER BY seq DESC LIMIT 15""")
    intents = _rows(orders_db, """SELECT created_at, kind, status, symbol, contract, cost, reason, broker_order_id
        FROM order_intents ORDER BY created_at DESC LIMIT 25""")
    live = [i for i in intents if i["status"] in ("reserved", "claimed")]
    order_events = _rows(orders_db, "SELECT timestamp, event, payload FROM order_events ORDER BY sequence DESC LIMIT 20")
    cycles = _jsonl(runtime / "cycles.jsonl", 12)
    notifications = _jsonl(runtime / "notifications.jsonl", 15)
    shadow = _json(runtime / "shadow-scan.json") or {}
    # Writes are what matter on a dashboard: every POST ever, plus recent reads.
    traces = _rows(runtime / "api-requests.sqlite3", """SELECT started_at, method, path, status, outcome FROM api_requests
        WHERE method='POST' OR started_at >= (SELECT MIN(started_at) FROM (SELECT started_at FROM api_requests ORDER BY started_at DESC LIMIT 8))
        ORDER BY started_at DESC LIMIT 40""")
    # Shadow per-playbook counts: how often each disabled playbook would have fired.
    shadow_counts = {name: 0 for name in PLAYBOOKS}
    for idea in shadow.get("shadow", []):
        if isinstance(idea, dict) and idea.get("playbook") in shadow_counts:
            shadow_counts[idea["playbook"]] += 1
    return {"now": now, "today": today, "control": control, "approvals": approvals, "daily_loss": loss, "daily_net": net,
            "latched": latched, "inventory": inventory, "closed": closed, "intents": intents, "live": live,
            "order_events": order_events, "cycles": cycles, "notifications": notifications, "shadow": shadow,
            "shadow_counts": shadow_counts, "traces": traces}


def render(data: dict) -> str:
    d = data
    t = lambda stamp: _t(stamp, d["today"])
    last = d["cycles"][0] if d["cycles"] else None
    rec = (last or {}).get("stages", {}).get("reconcile", {})
    control_cls = {"ARMED_PAPER": "ok", "EXITS_ONLY": "warn"}.get(d["control"], "bad")
    loss_cls = "bad" if d["latched"] or d["daily_loss"] >= 40 else ("warn" if d["daily_loss"] >= 25 else "ok")

    status = f"""<section><h2>Status</h2><dl>
<dt>control</dt><dd>{_pill(d['control'], control_cls)}</dd>
<dt>trading day</dt><dd>{_e(d['today'])} <span class="dim">(ET)</span></dd>
<dt>daily loss</dt><dd>{_pill(f"${d['daily_loss']:.2f} / $40.00", loss_cls)} {'latched' if d['latched'] else ''}</dd>
<dt>daily net</dt><dd class="{'ok' if d['daily_net'] >= 0 else 'bad'}">${d['daily_net']:.2f}</dd>
<dt>open positions</dt><dd>{len(d['inventory'])} / 2</dd>
<dt>live intents</dt><dd>{len(d['live'])}</dd>
<dt>last cycle</dt><dd>{_e(t((last or {}).get('finished_at')) or 'never')} <span class="dim">UTC</span> {_pill('reconciled', 'ok') if rec.get('ok') else _pill('unreconciled', 'bad') if last else ''}</dd>
<dt>submit</dt><dd>{_pill('enabled', 'warn') if (last or {}).get('submit') else _pill('dry run', 'ok')}</dd>
</dl>{'<ul class="reasons">' + ''.join(f'<li>{_e(r)}</li>' for r in rec.get('reasons', [])) + '</ul>' if rec.get('reasons') else ''}</section>"""

    playbooks = _table(["playbook", "approved", "shadow ideas"],
                       [(name, Html(_pill("APPROVED", "ok") if d["approvals"][name] else _pill("shadow only", "dim")),
                         d["shadow_counts"][name]) for name in PLAYBOOKS], numeric={2})
    playbooks = _panel("star", "Playbooks", playbooks +
                       f'<p class="dim">shadow scan: {_e(d["shadow"].get("generated_at", "none"))}</p>')

    positions = _table(["contract", "qty", "basis", "opened"],
                       [(r["contract"], r["qty"], f"${Decimal(r['basis']) / 1_000_000:.2f}", r["opened"]) for r in d["inventory"]], numeric={1, 2})
    positions = _panel("houston", "Open positions", positions)

    intents = _table(["time", "kind", "status", "contract", "cost", "broker", "reason"],
                     [(t(r["created_at"]), r["kind"], Html(_pill(r["status"], "ok" if r["status"].startswith(("resolved", "claimed")) else "warn" if r["status"] == "reserved" else "dim")),
                       r["contract"] or r["symbol"], r["cost"], r["broker_order_id"] or "", r["reason"]) for r in d["intents"]])
    intents = _panel("houston", "Order intents", intents, wide=True)

    closed = _table(["day", "contract", "pnl"],
                    [(r["trading_day"], r["contract"], Html(f'<span class="{"ok" if r["pnl_units"] >= 0 else "bad"}">${Decimal(r["pnl_units"]) / 1_000_000:.2f}</span>'))
                     for r in d["closed"]], numeric={2})
    closed = _panel("houston", "Closed trades", closed)

    def stage_summary(c):
        s = c.get("stages", {})
        parts = []
        if "reconcile" in s:
            parts.append("reconciled" if s["reconcile"].get("ok") else f"unreconciled({len(s['reconcile'].get('reasons', []))})")
        if "exits" in s:
            fired = [x for x in s["exits"] if x.get("prepared") or x.get("decision")]
            parts.append(f"exits {len(fired)}/{len(s['exits'])}")
        if isinstance(s.get("entries"), dict) and "entries" in s["entries"]:
            sent = [e for e in s["entries"]["entries"] if e.get("submission", {}).get("outcome") == "submitted"]
            parts.append(f"entries {len(sent)}/{len(s['entries']['entries'])}")
        elif isinstance(s.get("entries"), dict):
            parts.append(next(iter(s["entries"].values()), ""))
        if "halt" in s:
            parts.append("HALT")
        return ", ".join(str(p) for p in parts)

    cycles = _table(["finished", "mode", "submit", "summary"],
                    [(t(c.get("finished_at")), c.get("control_mode"), "yes" if c.get("submit") else "dry", stage_summary(c)) for c in d["cycles"]])
    cycles = _panel("astra", "Cycles", cycles, wide=True)

    notes = _table(["time", "level", "title"],
                   [(t(n.get("timestamp")), Html(_pill(n.get("level"), {"info": "dim", "warning": "warn"}.get(n.get("level"), "bad"))), n.get("title")) for n in d["notifications"]])
    notes = _panel("astra", "Notifications", notes)

    events = _table(["time", "event"], [(t(e["timestamp"]), e["event"]) for e in d["order_events"]])
    events = _panel("houston", "Journal events", events)

    traces = _table(["started", "method", "path", "status", "outcome"],
                    [(t(x["started_at"]), x["method"], x["path"], x["status"], x["outcome"]) for x in d["traces"]])
    traces = _panel("houston", "Broker requests", traces)

    risk = _panel("moon", "Risk checks", """<dl>
<dt>maximum entry risk</dt><dd>$100 including estimated fees</dd>
<dt>position limit</dt><dd>2 concurrent positions</dd>
<dt>daily loss breaker</dt><dd>$40 cumulative realized losses; wins do not offset</dd>
</dl><p class="dim">The breaker blocks new entries, not losses on existing positions.
These are system-wide limits, not separate budgets per agent.</p>""" +
                  ('<h2>Latest reconciliation blockers</h2><ul class="reasons">' +
                   ''.join(f'<li>{_e(r)}</li>' for r in rec.get("reasons", [])) + '</ul>'
                   if rec.get("reasons") else
                   '<p class="dim">No blockers reported in the latest cycle.</p>' if rec else
                   '<p class="dim">No controller reconciliation snapshot yet.</p>'))
    filters = '<input class="agent-filter" type="radio" name="agent" id="agent-all" checked><label class="agent-tab" for="agent-all">All agents</label>'
    summaries = []
    for key, name, role, description in AGENTS:
        filters += (f'<input class="agent-filter" type="radio" name="agent" id="agent-{key}">'
                    f'<label class="agent-tab" for="agent-{key}">{name}</label>')
        summaries.append(f'<section class="agent-summary" data-agent="{key}"><h2>{name}</h2>'
                         f'<p>{_e(role)}</p><p class="dim">{_e(description)}</p></section>')

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>AlpacaAgent - paper</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><style>{CSS}</style></head><body>
<header><h1>ALPACA AGENT</h1><span class="meta">paper only</span><span class="meta">rendered {_e(d['now'].astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'))} UTC</span></header>
<fieldset class="agent-picker"><legend>Four roles, one paper-trading system. Filter the view; trading settings stay unchanged.</legend>
{filters}<main>{status}{''.join(summaries)}{playbooks}{risk}{positions}{closed}{intents}{cycles}{notes}{events}{traces}</main></fieldset>
<footer>Static snapshot. Regenerate with <code>python -m alpaca_agents.dashboard</code>. Nothing on this page can place an order.</footer>
</body></html>"""


def build(runtime: Path, output: Path, *, now: datetime | None = None) -> Path:
    data = collect(runtime, now=now or datetime.now(timezone.utc))
    page = render(data)
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
