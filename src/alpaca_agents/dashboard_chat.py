"""Layer 4's offline conversation guide, not an LLM or a trading agent.

Only curated snapshot fields enter the browser. No credentials, account IDs,
order bodies, network access, broker actions or persistent chat storage.
"""
import base64
from functools import lru_cache
import hashlib
import html
from importlib.resources import files
import json
import re
from decimal import Decimal, InvalidOperation

ART = {
    "houston": "Wallet character",
    "star": "Spark character",
    "moon": "Ghost character",
    "astra": "Vinyl character",
}
PROMPTS = {
    "houston": ["Show my positions", "Why aren't we trading?", "What happened with orders?"],
    "star": ["Compare my backtests", "Explain QQQ results", "Why are scans failing?"],
    "moon": ["Explain my risk limits", "Can I trust these results?", "What does DISABLED mean?"],
    "astra": ["Give me a briefing", "What should I do next?", "Show recent notifications"],
}
NOVA = ("nova", "Nova", "General assistant", "Ask anything: markets, options concepts, today's news, or how this system works. Generative mode only.")
GENERATIVE_PROMPTS = {
    "nova": ["What happened in the market today?", "Explain how a long call can lose money while the stock rises",
             "What should I understand before trading options?"],
    "houston": ["Show my positions", "Why aren't we trading?", "What would you need to see before you'd place a trade?"],
    "star": ["What happened in the market today?", "Any major news on QQQ?", "Compare my QQQ and SPY backtests — which do you trust less?"],
    "moon": ["What would you refuse right now, and why?", "Explain same-day exits", "Walk me through the breaker"],
    "astra": ["Give me today's briefing: market, news, then our system", "What should I do next?", "Anything in the notifications I should worry about?"],
}


NOVA_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 110 124" width="110" height="124">'
            '<rect width="110" height="124" rx="16" fill="#2a2d33"/>'
            '<circle cx="55" cy="60" r="34" fill="#1c1f23" stroke="#c9a961" stroke-width="3"/>'
            '<path d="M55 30 L62 52 L85 60 L62 68 L55 90 L48 68 L25 60 L48 52 Z" fill="#c9a961"/>'
            '<circle cx="55" cy="60" r="6" fill="#f1e6c8"/></svg>')


@lru_cache(maxsize=8)
def avatar_uri(agent):
    if agent == "nova":
        return "data:image/svg+xml;base64," + base64.b64encode(NOVA_SVG.encode("utf-8")).decode("ascii")
    if agent not in ART:
        raise ValueError("Unknown display agent")
    raw = files("alpaca_agents").joinpath("assets", f"{agent}.png").read_bytes()
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


def _obj(value):
    return value if isinstance(value, dict) else {}


def _rows(value):
    return [r for r in value if isinstance(r, dict)] if isinstance(value, list) else []


def _decimal(value, places=2):
    try:
        number = Decimal(str(value))
        return f"{number:.{places}f}" if number.is_finite() and not isinstance(value, bool) else "unknown"
    except (InvalidOperation, ValueError):
        return "unknown"


def _reply(text, source):
    return {"text": text, "source": source}


def redact_secrets(text):
    text = re.sub(r'\b(?:api[_ -]?key|api[_ -]?secret|secret|token|account[_ -]?id|request[_ -]?id|trace[_ -]?id|broker_order_id|client_order_id)\s*[:=]\s*[\"\']?[^\s\"\',;&]+',
                  '[credential redacted]', str(text), flags=re.I)
    return re.sub(r'\b[A-Za-z0-9_-]{24,}\b', '[long token redacted]', text)


def answer_for(question, agent_id, data):
    """Server-side equivalent of dashboard.js's offline topic router; no actions."""
    q = question.lower().strip()
    agent = next((a for a in data['agents'] if a['id'] == agent_id), data['agents'][0])
    if re.search(r'\b(buy|sell|submit|execute|enable|disable|arm|flatten|cancel)\b|place.*order|change.*(limit|risk|control)|trade for me|guarantee.*(profit|return)|live price|price.*(now|today|tomorrow)|what.*(buy|trade)|best trade', q):
        return data['topics']['safety']
    if re.search(r'backtest|research|expectancy|\bresults?\b|\bedge\b|profitab|trust|\bmean r\b|\bwin rate\b', q):
        symbol = next((s for s in data['research'] if s.lower() in re.split(r'[^a-z0-9]+', q)), None)
        return data['research'][symbol] if symbol else data['topics']['research']
    if re.search(r'who are you|what do you do|your role|\bhello\b|\bhi\b', q):
        return _reply(agent['intro'], 'Display persona · local snapshot guide, not a live trader')
    for pattern, topic in (
        (r'risk|breaker|loss limit|stop loss|\$40|\$100|position limit|cash limit', 'risk'),
        (r'disabled|exits.only|armed.paper|control mode', 'controls'),
        (r'scan|data access|403|subscription|massive|polygon|stock.*advanced|options.*data|\bideas?\b', 'scan'),
        (r'block|why.*trad|not.*trad|aren.t.*trad|reconcil|market open|market closed', 'blockers'),
        (r'position|holding|inventory|unrealized', 'positions'),
        (r'order|intent|fill|last trade', 'orders'), (r'notif|alert|message', 'notifications'),
        (r'next|routine|recommend|improv|more effect|how.*use', 'next'),
        (r'roles|team|crew|who.*agents', 'roles'), (r'status|brief|summary|overview|cash|balance|p&l|pnl', 'briefing'),
    ):
        if re.search(pattern, q):
            return data['topics'][topic]
    return data['topics']['help']


def conversation_data(d, agents, *, live=False, generative=False):
    """Precompute auditable answers; JS only selects a topic, never invents facts."""
    last = d["cycles"][0] if d["cycles"] else {}
    rec = _obj(_obj(last.get("stages")).get("reconcile"))
    cycle_source = "cycles.jsonl · last recorded cycle " + str(last.get("finished_at") or "not available")
    rec_text = "No controller reconciliation snapshot has been saved. Standalone reconcile output does not populate cycle history."
    if rec:
        rec_text = "The saved controller check passed." if rec.get("ok") is True else "The saved controller check was blocked."
        rec_text += " This is a past check, not proof of current readiness."
    mode = d["control"]
    net = "$" + _decimal(d["daily_net"]) if d["ledger_known"] else "unknown"
    loss = "$" + _decimal(d["daily_loss"]) if d["ledger_known"] else "unknown"
    count = str(len(d["inventory"])) if d["ledger_known"] else "unknown"
    outstanding = str(len(d["live"])) if d["orders_known"] else "unknown"
    briefing = (f"Saved workspace: {mode}. Recorded positions: {count}. Outstanding intents: {outstanding}.\n"
                f"Recorded daily net: {net}; cumulative daily loss: {loss}.\n{rec_text}\n"
                "I cannot see whether a controller is running or whether the market is open right now. Rebuild the dashboard after new cycles to update my sources.")
    controls = (f"Your control file currently reads {mode}.\n"
                "DISABLED: no automatic entries OR exits.\nEXITS_ONLY: no new entries; exits require a running controller, --submit and valid checks.\n"
                "ARMED_PAPER: permits paper entries/exits but still requires checks, playbook enablement and --submit.\n"
                "A filter or chat message never changes these settings. If positions are open, do not assume this dashboard manages them.")
    blockers = [str(r) for r in rec.get("reasons", [])] if isinstance(rec.get("reasons"), list) else []
    if rec.get("error"):
        blockers.append(str(rec["error"]))
    if mode == "DISABLED":
        blockers.insert(0, "Trading control is DISABLED (entries and exits).")
    if last and last.get("submit") is not True:
        blockers.append("Last controller cycle ran without submission enabled.")
    if not last:
        blockers.append("No controller cycle recorded; broker readiness is unknown here.")
    if d["latched"]:
        blockers.append("The daily loss breaker is latched.")
    blockers.extend(str(i) for i in d["issues"][:5])
    if not any(d["approvals"].values()):
        blockers.append("No playbook approval markers are present.")
    errors = _rows(d["shadow"].get("errors"))
    blockers.extend(f"Scan {r.get('symbol', '?')}: {r.get('reason', 'unknown error')}" for r in errors[:3])
    blocked_text = "Recorded blockers / missing prerequisites:\n" + "\n".join("• " + b for b in blockers) if blockers else "No blocker is recorded in these saved sources. That is not an authorization or a guarantee that all prerequisites are met."
    blocked_text += "\nDo not bypass a check or repeat an uncertain order. Reconcile and inspect the broker first."

    if d["ledger_known"]:
        position_text = "No open positions recorded in the local ledger. Confirm with a fresh reconciliation before acting."
        if d["inventory"]:
            position_text = "Local positions (basis, not market value):\n" + "\n".join(
                f"• {r.get('contract')}: {r.get('qty')} contract(s), basis ${_decimal(Decimal(str(r['basis'])) / 1_000_000)}, opened {r.get('opened')}"
                for r in d["inventory"][:10])
        position_text += "\nI have no live option marks or unrealized P&L."
    else:
        position_text = "The position ledger is missing or unreadable. Unknown is not zero. Check broker records and reconcile."
    order_text = f"Outstanding intents: {outstanding}. Claimed means awaiting resolution, not necessarily filled.\n"
    order_text += "\n".join(f"• {r.get('kind')} {r.get('contract') or r.get('symbol')}: {r.get('status')}" for r in d["intents"][:5]) or "No recent order records."
    order_text += "\nI cannot submit, cancel, release or flatten orders through chat."
    notes = "\n".join(f"• {n.get('timestamp', '?')} · {n.get('level', '?')}: {n.get('title', '')}" for n in d["notifications"][:5])
    notes = notes or "No notifications recorded. This does not establish that the system is running."
    risk = ("Moon's checklist: maximum $100 risk per entry including estimated fees; at most 2 concurrent positions; "
            "$40 cumulative daily realized-loss breaker (wins do not offset losses).\n"
            f"Recorded loss today: {loss}; breaker latched: {'yes' if d['latched'] else 'no' if d['ledger_known'] else 'unknown'}.\n"
            "The $40 breaker blocks NEW entries after losses are booked. It does not cap losses on existing positions or guarantee stop fills. "
            "No strategy or risk limit guarantees a profit. These are shared limits, not four separate budgets.")
    research_lines, by_symbol = [], {}
    caution = ("These are underlying-price R results, NOT option profits or a dollar forecast. A positive mean with a negative median means a few "
               "outlier trades carry the result. The sample flag is only a count threshold, "
               "not statistical proof. Premium, IV, theta, spreads and fees are not modeled. "
               "Outcome labels may describe an exit trigger rather than positive P&L. Audit unusual fills and large R values; "
               "test out-of-sample before considering paper validation. No report enables a playbook.")
    for report in d["research"]:
        symbol = str(report.get("symbol", "unknown"))
        lines = []
        for name, raw in _obj(report.get("summary")).items():
            s = _obj(raw)
            lines.append(f"• {symbol} / {name}: {s.get('resolved', '?')} resolved, mean {_decimal(s.get('expectancy_r'), 2)}R, "
                         f"median {_decimal(s.get('median_r'), 2)}R, profit factor {_decimal(s.get('profit_factor'), 2)}, "
                         f"worst {_decimal(s.get('max_loss_r'), 1)}R, no-trade fills {s.get('no_trade', '?')}. "
                         f"{'Count threshold met, not proof.' if s.get('sample_sufficient') is True else 'Small or unknown sample.'}")
        research_lines.extend(lines)
        by_symbol[symbol.upper()] = _reply("\n".join(lines) + "\n\n" + caution,
            f"{report.get('file')} · {report.get('first_bar')} through {report.get('last_bar')} · saved {report.get('generated_at', 'unknown')}")
    research_text = ("\n".join(research_lines) if research_lines else "No supported backtest reports saved as runtime/bt-*.json.") + "\n\n" + caution
    scan = d["shadow"]
    scan_text = "No shadow scan has been saved. Stocks Advanced can support stock research, but not realtime options selection."
    if scan:
        scan_text = f"Last saved shadow report: {scan.get('generated_at', 'unknown time')}. Ideas: {len(_rows(scan.get('shadow')))}. Snapshot errors: {len(errors)}.\n"
        scan_text += "\n".join(f"• {r.get('symbol')}: {r.get('reason')}" for r in errors[:5])
        if any("403" in str(r.get("reason")) for r in errors):
            scan_text += "\n403 means access was denied. Check options-snapshot and realtime-quote entitlements. Stocks Advanced alone does not grant them."
        scan_text += "\nZero ideas with data errors does not mean there were no market setups. I cannot fetch a fresh scan from chat."
    next_steps = ("1. Check mode, positions, data-quality warnings and the age of your last cycle.\n"
                  "2. Resolve unexplained broker/risk blockers; do not bypass them.\n"
                  "3. Use Stocks Advanced for historical research while options access is pending. Keep unvalidated playbooks disabled.\n"
                  "4. Review fills and exit reasons after each session. Rebuild this dashboard to update the snapshot.\n"
                  "Safe diagnostic command: python -m alpaca_agents.executor reconcile\n"
                  "Dashboard command: python -m alpaca_agents.dashboard\n"
                  "These commands are displayed for you to review; I do not execute them.")
    roles = "\n".join(f"{name} — {description}" for _, name, _, description in agents)
    topics = {
        "briefing": _reply(briefing, cycle_source + " · local fill/order journals"),
        "blockers": _reply(blocked_text, cycle_source + " · trading-control · approval files · shadow-scan.json"),
        "risk": _reply(risk, "System risk rules · fills.sqlite3 · " + str(d["today"]) + " ET"),
        "positions": _reply(position_text, "fills.sqlite3 · local recorded inventory"),
        "orders": _reply(order_text, "orders.sqlite3 · latest 5 of 25 displayed intents"),
        "notifications": _reply(notes, "notifications.jsonl · latest 5 local notifications"),
        "research": _reply(research_text, "Local bt-*.json reports · underlying-only replay"),
        "scan": _reply(scan_text, "shadow-scan.json · saved report, not live quotes"),
        "controls": _reply(controls, "trading-control · saved at dashboard render"),
        "next": _reply(next_steps, "Operating guidance · no commands executed"),
        "roles": _reply(roles, "Four architectural roles · chat is a Layer 4 guide only"),
        "safety": _reply("I can't place, approve, cancel or change trades, controls, limits or keys. This chat is read-only and has no broker connection. "
                         "I also can't promise returns or recommend a specific trade from a saved snapshot. Ask about recorded blockers, research or risk instead.", "Read-only chat boundary"),
        "help": _reply("I'm a local, rule-based snapshot guide, not generative AI. I can explain your saved positions, orders, scans, research, risk limits, controls and notifications. "
                       "Try the suggested questions. I don't have live prices, and chat is cleared on refresh. Never paste API keys here.", "Local guide capabilities"),
    }
    if live:
        topics['briefing']['text'] = briefing.replace(
            'I cannot see whether a controller is running or whether the market is open right now. Rebuild the dashboard after new cycles to update my sources.',
            'These local records were read for this request, not fetched from the broker. A recent saved check is not proof of current readiness. Ask Houston exactly "reconcile" for a dry diagnostic cycle.')
        topics['controls']['source'] = 'trading-control · read for this request'
        topics['next']['text'] = next_steps.replace('Rebuild this dashboard to update the snapshot.', 'Ask again to read updated local records; refresh the page for updated panels.')
        topics['help']['text'] = ('I am a local rule-based guide, not generative AI. I read current runtime records for each question, '
                                  'not live quotes. Ask Houston exactly "reconcile" for a dry diagnostic cycle. No orders, controls or approvals from chat. Never paste keys.')
        topics['safety']['text'] = ("I can't place, approve, cancel or change trades, controls, limits or keys. Chat has no order route. "
                                    'Only Houston\'s exact "reconcile" command requests a dry controller check; it cannot submit orders.')
    chat_agents = list(agents) + ([NOVA] if generative else [])
    return {"rendered_at": d["now"].isoformat(), "live": live,
            "last_cycle": last.get('finished_at'), "topics": topics, "research": by_symbol,
            "default_agent": "nova" if generative else "astra",
            "agents": [{"id": key, "name": name, "role": role, "avatar": avatar_uri(key),
                        "intro": (f"{name} here — {role.lower()}. Ask me anything about the market today, the news, or what this system has recorded. "
                                  "I read records and data feeds; I can't place or change trades.") if generative else
                                 f"I'm {name}'s dashboard guide. My specialty: {role.lower()}. Ask me to explain the saved records; I cannot operate the trading system.",
                        "prompts": (GENERATIVE_PROMPTS if generative else PROMPTS)[key]} for key, name, role, description in chat_agents]}


def safe_json(data):
    # Prevent HTML-parser breakout from the non-executable JSON script block.
    return json.dumps(data, ensure_ascii=True, allow_nan=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def script_policy(*, live=False):
    javascript = files("alpaca_agents").joinpath("assets", "dashboard.js").read_text(encoding="utf-8")
    digest = base64.b64encode(hashlib.sha256(javascript.encode("utf-8")).digest()).decode("ascii")
    connect = "'self'" if live else "'none'"
    return f"default-src 'none'; img-src data:; style-src 'unsafe-inline'; script-src 'sha256-{digest}'; connect-src {connect}; form-action 'none'; base-uri 'none'; object-src 'none'"


def render_chat(d, agents, *, studio=None):
    data = conversation_data(d, agents, live=studio is not None, generative=bool(studio and studio.get('generative')))
    if studio is not None:
        data['studio'] = studio  # In-memory HTTP bootstrap only; never written by build().
    javascript = files("alpaca_agents").joinpath("assets", "dashboard.js").read_text(encoding="utf-8")
    csp = script_policy(live=studio is not None)
    generative = bool(studio and studio.get('generative'))
    default = "nova" if generative else "astra"
    chat_agents = list(agents) + ([NOVA] if generative else [])
    options = "".join(f'<option value="{key}"{" selected" if key == default else ""}>{name} · {html.escape(role)}</option>' for key, name, role, _ in chat_agents)
    persona_key, persona_name, persona_role, _ = next(a for a in chat_agents if a[0] == default)
    nova_button = ('<button class="nova-button" type="button" data-chat-agent="nova" disabled>Ask Nova anything ↗</button>' if generative else "")
    markup = f'''<aside class="chat-dock" id="agent-chat" aria-labelledby="chat-title">
<div class="chat-heading"><span class="eyebrow">Workspace assistant</span><h2 id="chat-title">Chat</h2><p>Local snapshot guide · not generative AI</p></div>
<label class="chat-label" for="chat-agent">Choose your agent</label><select id="chat-agent">{options}</select>
<div class="chat-persona"><img id="chat-avatar" src="{avatar_uri(persona_key)}" alt="{persona_name} avatar" width="58" height="64"><div><strong id="chat-name">{persona_name}</strong><span id="chat-role">{html.escape(persona_role)}</span></div><span class="local-badge">LOCAL</span></div>{nova_button}
<p class="chat-snapshot" id="chat-snapshot">Saved snapshot · not live market data</p>
<div id="chat-log" role="log" aria-live="polite" aria-relevant="additions" aria-label="Agent conversation" tabindex="0"><p class="chat-placeholder">Select a suggested question or type below. Enable JavaScript for local chat; the dashboard remains usable without it.</p></div>
<div id="chat-prompts" aria-label="Suggested questions"></div>
<form id="chat-form"><label class="chat-label" for="chat-input">Ask about your workspace</label><div class="composer"><textarea id="chat-input" rows="2" maxlength="800" placeholder="Ask a question…" required disabled></textarea><button id="chat-send" type="submit" disabled aria-label="Send message">↗</button></div></form>
<div class="chat-bottom"><span>Read-only · no network · no orders</span><span id="chat-usage" hidden></span><span class="chat-buttons"><button id="chat-expand" type="button" aria-pressed="false" disabled>Expand chat</button><button id="chat-clear" type="button" disabled>Clear chat</button></span></div>
<p class="chat-privacy">Don’t paste keys. Messages stay in memory and clear on refresh. Replies use only the saved snapshot and supported topics.</p>
<noscript><p class="notice">Local chat needs JavaScript. No remote service or API key is required.</p></noscript></aside>'''
    if studio is not None:
        markup = markup.replace('Read-only · no network · no orders', 'Local server · no chat orders')
        markup = markup.replace('Replies use only the saved snapshot and supported topics.', 'Replies read current local records. Houston: type reconcile for a dry diagnostic cycle.')
        if studio.get('generative'):
            markup = markup.replace('Local snapshot guide · not generative AI', 'Generative replies (Anthropic API) · read-only tools · no orders')
            markup = markup.replace('<span class="local-badge">LOCAL</span>', '<span class="local-badge">LOCAL + API</span>')
            markup = markup.replace('Don’t paste keys. Messages stay in memory and clear on refresh.',
                                    'Don’t paste keys. Your questions and sanitized local records are sent to Anthropic’s API. Memory is per agent, in this process only; Clear chat erases it.')
    scripts = f'<script id="agent-context" type="application/json">{safe_json(data)}</script><script>{javascript}</script>'
    return markup, scripts, csp
