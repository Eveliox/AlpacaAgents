"""Additive Agent Desk markup; presentation only, no services or trading calls."""
import html
import json

from .agent_desk import LIMITATIONS, ROLES


def e(value):
    return html.escape(str(value) if value is not None else 'Unknown', quote=True)


# Signal-path replay colours. Same table lives in agent_desk.js; keep both in sync.
# pass = the recorded stage let the signal continue; stop = refused/halted/uncertain; warn = skipped/unknown.
TONES = {'passed_at_cycle': 'pass', 'recorded': 'pass', 'risk_pass': 'pass', 'submission_recorded': 'pass',
         'dry_run': 'pass', 'ARMED_PAPER': 'pass', 'EXITS_ONLY': 'warn',
         'blocked': 'stop', 'rejected': 'stop', 'error': 'stop', 'uncertain': 'stop', 'halted': 'stop'}


def tone(status):
    return TONES.get(status, 'warn')


def empty_state():
    return {"schema_version": 1, "cycles": [], "warnings": ['No controller cycles available in this snapshot.'],
            "history_truncated": False, "history_limit": 20, "limitations": LIMITATIONS,
            "cursor": None, "read_at": None}


def render_desk(state):
    state = state or empty_state()
    cycles = state['cycles']
    c = cycles[0] if cycles else None
    options = ''.join(f'<option value="{e(x["id"])}"{" selected" if i == 0 else ""}>{e(x["completed_at"])} · {e(x["status"])} · {e(x["id"][:12])}</option>' for i, x in enumerate(cycles))
    flow = ''.join(f'<li data-tone="{tone(x["status"])}" class="lit"><strong>{e(x["name"])}</strong><span>{e(x["status"])}</span></li>' for x in c['flow']) if c else '<li>No recorded flow. Unknown is not idle.</li>'
    agents = c['agents'] if c else [{"id": k, "name": n, "role": r, "status": 'unknown', "summary": 'No cycle selected.'} for k, n, r in ROLES]
    cards = ''.join(f'<button type="button" class="desk-agent" data-desk-agent="{e(a["id"])}" data-tone="{tone(a["status"])}" aria-controls="desk-inspector" disabled>'
                    f'<span class="desk-role">{e(a["role"])}</span><strong>{e(a["name"])}</strong><span class="desk-state">{e(a["status"])}</span></button>' for a in agents)
    warnings = ''.join(f'<li>{e(w)}</li>' for w in state['warnings'])
    evidence = json.dumps(c, indent=2, ensure_ascii=True) if c else 'No saved cycle. This page does not start one.'
    return f'''<aside class="desk-module wide" id="agent-desk" aria-label="Agent Desk">
<details id="desk-panel"><summary class="desk-launch"><span><span class="eyebrow">Decision observability</span><strong>Agent Desk</strong></span><span class="dim small">Inspect recorded decisions ↗</span></summary>
<p class="desk-intro">Six functional views of the existing engine. Read-only: no orders, approvals or new models.</p>
<div class="desk-toolbar"><label for="desk-cycle">Recorded cycle<select id="desk-cycle" disabled><option value="">No selection</option>{options}</select></label><button id="desk-follow" type="button" disabled>Follow latest</button><button id="desk-refresh" type="button" disabled>Refresh records</button><button id="desk-replay" type="button" disabled>▶ Replay path</button></div>
<p id="desk-connection" role="status">Saved snapshot · no live stage stream.</p>
<p id="desk-freshness" class="dim small">Last read: {e(state['read_at'])}. Controller running status is not verified.</p>
<ul id="desk-warnings" class="desk-warnings">{warnings}</ul>
<p id="desk-history-note" class="dim small">Up to {state['history_limit']} saved cycles. {'Older records omitted.' if state['history_truncated'] else 'History may be incomplete.'}</p>
<div id="desk-content"><h3 id="desk-cycle-title">{e(c['id']) if c else 'No recorded cycle'}</h3>
<ol class="desk-flow" aria-label="Actual controller order">{flow}</ol>
<p class="dim small desk-legend"><span data-tone="pass">●</span> passed, signal continued · <span data-tone="warn">●</span> skipped / unknown · <span data-tone="stop">●</span> refused / halted · Replay of the saved record, not a live stream. Exit checks precede entry scanning; DISABLED skips entry scanning and automatic exits.</p>
<div class="desk-swarm" aria-label="Six views sharing one cycle, not six independent predictors">
<svg class="desk-connectors" viewBox="0 0 600 300" preserveAspectRatio="none" aria-hidden="true"><path data-link="spotter" d="M100 50L300 150"/><path data-link="prior" d="M300 50L300 150"/><path data-link="edge" d="M500 50L300 150"/><path data-link="risk" d="M300 150L100 250"/><path data-link="entry" d="M300 150L300 250"/><path data-link="exit" d="M300 150L500 250"/></svg>
{cards}<div class="desk-core" data-tone="{tone(c['status']) if c else 'warn'}"><span class="eyebrow">Shared recorded state</span><strong>{e(c['status']) if c else 'Unknown'}</strong><small>No consensus probability</small></div></div>
<p class="dim small">Connectors mean shared evidence, not messages, signal weight or agreement.</p>
<div id="desk-inspector" tabindex="-1" aria-live="polite"><h3>Inspect a functional view</h3><p class="dim">Select a card to inspect its recorded inputs, outputs and source.</p></div>
<div id="desk-proposals"></div><div id="desk-activity"></div>
<details class="desk-evidence"><summary>Complete selected-cycle evidence · also available without JavaScript</summary><pre id="desk-evidence">{e(evidence)}</pre></details></div>
<details class="desk-limitations"><summary>What this desk can and cannot establish</summary><ul>{''.join(f'<li>{e(x)}</li>' for x in state['limitations'])}</ul><p>System limits: $2,000 capital cap · $100 maximum entry risk including fees · 2 positions · $40 cumulative daily loss breaker. These are not Kelly recommendations or a frozen historical configuration.</p><a href="#research-lab">Review underlying research separately ↗</a></details>
<noscript><p class="notice">JavaScript is required to switch cycles and refresh. The latest recorded cycle and its evidence remain readable above. No actions are available.</p></noscript>
</details></aside>'''


CSS = """
.desk-module{grid-column:1/-1;min-width:0;border:1px solid #49334f;border-radius:9px;background:#19171e;padding:0 18px;scroll-margin-top:24px}.desk-launch{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:18px 0;list-style:none}.desk-launch::-webkit-details-marker{display:none}.desk-launch strong{display:block;font-size:18px;letter-spacing:-.025em;color:var(--fg);font-weight:550}.desk-launch:after{content:'+';color:var(--gold);font-size:22px}#desk-panel[open]>.desk-launch:after{content:'−'}#desk-panel[open]{padding-bottom:18px}.desk-intro{border-top:1px solid var(--line);padding-top:12px;font-size:12px;color:var(--dim)}.desk-toolbar{display:flex;align-items:end;gap:8px;flex-wrap:wrap;margin-top:16px}.desk-toolbar label{flex:1;min-width:160px;font-size:10px;color:var(--dim)}.desk-toolbar select{display:block;width:100%;max-width:100%;font-size:11px;color:var(--fg);background:#211e27;border:1px solid #44334c;border-radius:5px;padding:8px;margin-top:4px}.desk-toolbar button{border:1px solid #44334c;border-radius:5px;padding:8px 10px;font-size:11px;color:var(--gold);background:#2a2030}.desk-toolbar button[aria-pressed=true]{background:#45304d}#desk-connection{font-size:11px;color:var(--gold)}.desk-warnings{font-size:11px;color:var(--red);padding-left:18px}.desk-flow{list-style:none;display:flex;gap:6px;padding:0;margin:16px 0;overflow-x:auto}.desk-flow li{flex:1;min-width:105px;background:#131319;border:1px solid var(--line);border-radius:5px;padding:10px;position:relative;transition:border-color .3s,box-shadow .3s}.desk-flow li:not(:last-child):after{content:'→';position:absolute;right:-8px;top:8px;color:#5a4b60;z-index:1;transition:color .2s}.desk-flow li:before{content:'';position:absolute;left:0;bottom:0;height:2px;width:0;border-radius:0 0 5px 5px;background:var(--tone,transparent);transition:width .35s linear}.desk-flow li.lit:before{width:100%}[data-tone=pass]{--tone:var(--green)}[data-tone=warn]{--tone:var(--gold)}[data-tone=stop]{--tone:var(--red)}.desk-flow li.lit{border-color:var(--tone);box-shadow:0 0 12px color-mix(in srgb,var(--tone) 22%,transparent)}.desk-flow li.signal:after{color:var(--tone);text-shadow:0 0 8px var(--tone)}.desk-flow li.signal>span:last-child:after{content:'';position:absolute;top:9px;right:-14px;width:5px;height:5px;border-radius:50%;background:var(--tone);box-shadow:0 0 6px var(--tone);animation:desk-hop .45s ease-out both;z-index:2}@keyframes desk-hop{from{transform:translateX(-32px);opacity:0}60%{opacity:1}to{transform:translateX(0);opacity:0}}.desk-legend [data-tone]{color:var(--tone)}.desk-flow strong{display:block;font-size:10px;font-weight:500}.desk-flow span{display:block;font-size:9px;color:var(--dim);margin-top:7px;overflow-wrap:anywhere}.desk-swarm{position:relative;isolation:isolate;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));grid-template-rows:110px 90px 110px;gap:10px;margin:18px 0}.desk-connectors{position:absolute;width:100%;height:100%;z-index:-1;opacity:.85;pointer-events:none}.desk-connectors path{fill:none;stroke:#4a3a52;stroke-width:1;vector-effect:non-scaling-stroke;transition:stroke .3s}.desk-connectors path.lit{stroke:var(--tone);stroke-width:1.5;filter:drop-shadow(0 0 3px var(--tone))}.desk-swarm.flowing .desk-connectors path.lit{stroke-dasharray:5 9;animation:desk-dash .9s linear infinite}@keyframes desk-dash{to{stroke-dashoffset:-28}}.desk-agent,.desk-core{transition:border-color .3s,box-shadow .3s}.desk-agent.lit,.desk-core.lit{border-color:var(--tone);box-shadow:0 0 16px color-mix(in srgb,var(--tone) 24%,transparent),inset 0 0 0 1px color-mix(in srgb,var(--tone) 35%,transparent)}.desk-agent.lit .desk-state{color:var(--tone)}@media(prefers-reduced-motion:reduce){.desk-connectors,.desk-connectors path,.desk-flow li,.desk-flow li:before,.desk-flow li.signal>span:last-child:after,.desk-agent,.desk-core{animation:none!important;transition:none!important}}.desk-agent{display:flex;flex-direction:column;align-items:flex-start;justify-content:center;gap:5px;text-align:left;background:#201b26;border:1px solid #403146;border-radius:7px;padding:12px;min-width:0}.desk-agent:nth-of-type(n+4){grid-row:3}.desk-agent:hover,.desk-agent[aria-pressed=true]{border-color:var(--gold);background:#302336}.desk-role{font:9px ui-monospace,Consolas,monospace;text-transform:uppercase;color:var(--dim)}.desk-agent strong{font-size:12px;font-weight:550}.desk-state{font:10px ui-monospace,Consolas,monospace;color:var(--gold);overflow-wrap:anywhere}.desk-agent[data-status=not_implemented],.desk-agent[data-status=unknown]{border-style:dashed}.desk-agent[data-status=rejected] .desk-state,.desk-agent[data-status=uncertain] .desk-state,.desk-agent[data-status=error] .desk-state{color:var(--red)}.desk-core{grid-row:2;grid-column:1/-1;justify-self:center;align-self:center;display:flex;flex-direction:column;align-items:center;background:#24212c;border:1px solid #675072;border-radius:7px;padding:10px 22px;max-width:100%}.desk-core strong{font-size:14px;font-weight:500;color:var(--fg)}.desk-core small{font-size:9px;color:var(--dim)}#desk-inspector{padding:14px;border:1px solid var(--line);border-radius:7px;background:#141319;font-size:12px}#desk-inspector h3{margin-top:0}#desk-inspector dl{font-size:11px}#desk-inspector pre,.desk-evidence pre{max-height:360px;white-space:pre-wrap;overflow-wrap:anywhere}.desk-activity-item,.desk-proposal{border-top:1px solid var(--line);padding:5px 0}.desk-activity-item summary,.desk-proposal summary{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap}.desk-activity-item pre{max-height:260px;white-space:pre-wrap;overflow-wrap:anywhere}.desk-source{font-size:10px;color:var(--dim);overflow-wrap:anywhere}.desk-proposal dl{font-size:11px;padding:10px 0}.desk-limitations{font-size:11px;color:var(--dim);border-top:1px solid var(--line);padding-top:8px;margin-top:16px}#desk-cycle-title{overflow-wrap:anywhere;font:11px ui-monospace,Consolas,monospace;margin-top:16px}.desk-empty{border:1px dashed var(--line);border-radius:6px;color:var(--dim);padding:14px;font-size:12px}
@media(max-width:760px){.desk-module{padding:0 12px}.desk-launch .small{display:none}.desk-swarm{grid-template-columns:repeat(2,minmax(0,1fr));grid-template-rows:repeat(3,110px) 85px;gap:8px}.desk-agent:nth-of-type(n){grid-row:auto}.desk-core{grid-row:4}.desk-connectors{display:none}.desk-agent{padding:10px}.desk-agent strong{font-size:11px}.desk-toolbar select{font-size:10px}#desk-inspector dl,.desk-proposal dl{grid-template-columns:1fr}.desk-flow li{min-width:120px}}
"""
