/* Layer 4 guide. Static mode is offline; served mode calls only its loopback origin.
   No storage, shell commands, order endpoints or broker credentials. */
(() => {
  "use strict";

  function redactSecrets(text) {
    return String(text)
      .replace(/\b(?:api[_ -]?key|api[_ -]?secret|secret|token)\s*[:=]\s*["']?[^\s"']+/gi, "[credential redacted]")
      .replace(/\b[A-Za-z0-9_-]{24,}\b/g, "[long token redacted]");
  }

  function answerFor(question, agentId, data) {
    const q = String(question).toLowerCase().trim();
    const agent = data.agents.find(a => a.id === agentId) || data.agents[0];
    let topic;
    if (/\b(buy|sell|submit|execute|enable|disable|arm|flatten|cancel)\b|place.*order|change.*(limit|risk|control)|trade for me|guarantee.*(profit|return)|live price|price.*(now|today|tomorrow)|what.*(buy|trade)|best trade/.test(q)) {
      topic = "safety";
    } else if (/backtest|research|expectancy|\bresults?\b|\bedge\b|profitab|trust|\bmean r\b|\bwin rate\b/.test(q)) {
      const symbol = Object.keys(data.research).find(s => q.split(/[^a-z0-9]+/).includes(s.toLowerCase()));
      if (symbol) return data.research[symbol];
      topic = "research";
    } else if (/who are you|what do you do|your role|\bhello\b|\bhi\b/.test(q)) {
      return {text: agent.intro, source: "Display persona · local snapshot guide, not a live trader"};
    } else if (/risk|breaker|loss limit|stop loss|\$40|\$100|position limit|cash limit/.test(q)) {
      topic = "risk";
    } else if (/disabled|exits.only|armed.paper|control mode/.test(q)) {
      topic = "controls";
    } else if (/scan|data access|403|subscription|massive|polygon|stock.*advanced|options.*data|\bideas?\b/.test(q)) {
      topic = "scan";
    } else if (/block|why.*trad|not.*trad|aren.t.*trad|reconcil|market open|market closed/.test(q)) {
      topic = "blockers";
    } else if (/position|holding|inventory|unrealized/.test(q)) {
      topic = "positions";
    } else if (/order|intent|fill|last trade/.test(q)) {
      topic = "orders";
    } else if (/notif|alert|message/.test(q)) {
      topic = "notifications";
    } else if (/next|routine|recommend|improv|more effect|how.*use/.test(q)) {
      topic = "next";
    } else if (/roles|team|crew|who.*agents/.test(q)) {
      topic = "roles";
    } else if (/status|brief|summary|overview|cash|balance|p&l|pnl/.test(q)) {
      topic = "briefing";
    } else {
      topic = "help";
    }
    return data.topics[topic];
  }

  // Pure functions can be tested in Node without a browser or an API key.
  if (typeof module !== "undefined" && module.exports) module.exports = {answerFor, redactSecrets};
  if (typeof document === "undefined") return;
  const context = document.getElementById("agent-context");
  if (!context) return;
  const data = JSON.parse(context.textContent);
  const studio = data.studio;
  if (studio && (location.origin !== studio.base_url || location.hostname !== '127.0.0.1')) return;
  if (studio) {
    window.__studio = studio;
    data.schedule = studio.schedule;
  }
  let pending = false;
  let pendingAgent = null;
  let generation = 0;
  let connected = true;
  async function request(route, body) {
    const response = await fetch(studio.base_url + route, {
      method: body ? 'POST' : 'GET', mode: 'same-origin', credentials: 'omit',
      cache: 'no-store', redirect: 'error', signal: AbortSignal.timeout(120000),
      headers: {'X-Studio-Token': studio.token, ...(body ? {'Content-Type': 'application/json'} : {})},
      ...(body ? {body: JSON.stringify(body)} : {})
    });
    if (!response.ok) throw new Error('Local server unavailable');
    return response.json();
  }
  const selector = document.getElementById("chat-agent");
  const log = document.getElementById("chat-log");
  const input = document.getElementById("chat-input");
  const form = document.getElementById("chat-form");
  const promptBox = document.getElementById("chat-prompts");
  const clear = document.getElementById("chat-clear");
  const threads = new Map();
  let active = data.default_agent || "astra";

  function persona() { return data.agents.find(a => a.id === active); }
  function history() {
    if (!threads.has(active)) threads.set(active, [{who: "agent", text: persona().intro, source: studio && studio.generative ? "Assistant introduction · read-only tools" : "Local, rule-based guide · no generative model connected"}]);
    return threads.get(active);
  }
  // Markdown-lite rendered as DOM nodes only: bold, inline code, bullets, numbered
  // lists, headings, https links. Built with createElement only; model/news text is untrusted.
  function inline(text, target) {
    const pattern = /(\*\*[^*\n]+\*\*|`[^`\n]+`|https:\/\/[^\s<>()\]]+)/g;
    let last = 0, match;
    while ((match = pattern.exec(text)) !== null) {
      if (match.index > last) target.append(text.slice(last, match.index));
      const token = match[0];
      if (token.startsWith("**")) {
        const strong = document.createElement("strong");
        strong.textContent = token.slice(2, -2);
        target.append(strong);
      } else if (token.startsWith("`")) {
        const code = document.createElement("code");
        code.textContent = token.slice(1, -1);
        target.append(code);
      } else {
        const link = document.createElement("a");
        const clean = token.replace(/[.,;:!?]+$/, "");
        link.href = clean;
        link.textContent = clean.replace(/^https:\/\//, "").slice(0, 60) + (clean.length > 68 ? "\u2026" : "");
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.title = "Opens the publisher's site in a new tab";
        target.append(link);
        if (clean.length !== token.length) target.append(token.slice(clean.length));
      }
      last = match.index + token.length;
    }
    if (last < text.length) target.append(text.slice(last));
  }
  function renderBody(text) {
    const root = document.createElement("div");
    root.className = "message-body";
    let list = null, paragraph = null;
    for (const raw of String(text).split("\n")) {
      const line = raw.replace(/\s+$/, "");
      const bullet = /^\s*(?:[-*\u2022]|\d+[.)])\s+(.*)$/.exec(line);
      const heading = /^#{1,4}\s+(.*)$/.exec(line);
      if (bullet) {
        paragraph = null;
        if (!list) { list = document.createElement(/^\s*\d/.test(line) ? "ol" : "ul"); root.append(list); }
        const li = document.createElement("li");
        inline(bullet[1], li);
        list.append(li);
      } else if (heading) {
        list = null; paragraph = null;
        const h = document.createElement("p");
        h.className = "message-heading";
        inline(heading[1], h);
        root.append(h);
      } else if (line.trim() === "") {
        list = null; paragraph = null;
      } else {
        list = null;
        if (!paragraph) { paragraph = document.createElement("p"); root.append(paragraph); }
        else paragraph.append(document.createElement("br"));
        inline(line, paragraph);
      }
    }
    return root;
  }
  function stamp(date) {
    return date.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
  }
  function drawMessage(message) {
    const item = document.createElement("div");
    item.className = `chat-message ${message.who === "user" ? "from-user" : "from-agent"}`;
    const head = document.createElement("div");
    head.className = "message-head";
    if (message.who !== "user") {
      const face = document.createElement("span");
      face.className = "message-avatar";
      face.style.backgroundImage = `url("${persona().avatar}")`;
      face.setAttribute("aria-hidden", "true");
      head.append(face);
    }
    const label = document.createElement("span");
    label.className = "message-name";
    label.textContent = message.who === "user" ? "You" : persona().name;
    head.append(label);
    if (message.at) {
      const time = document.createElement("time");
      time.className = "message-time";
      time.textContent = stamp(new Date(message.at));
      head.append(time);
    }
    const body = message.who === "user" ? document.createElement("p") : renderBody(message.text);
    if (message.who === "user") body.textContent = message.text;
    item.append(head, body);
    // Chart payloads are isolated image documents, never DOM/SVG markup.
    if (Array.isArray(message.charts)) message.charts.slice(0, 6).forEach(svg => {
      if (typeof svg !== 'string' || svg.length > 200000) return;
      const image = document.createElement('img');
      image.alt = 'Chart from local runtime records';
      image.style.maxWidth = '100%';
      image.src = 'data:image/svg+xml;base64,' + btoa(Array.from(new TextEncoder().encode(svg), b => String.fromCharCode(b)).join(''));
      item.append(image);
    });
    if (message.source) {
      const source = document.createElement("small");
      source.className = "message-source";
      source.textContent = "Source: " + message.source;
      item.append(source);
    }
    log.append(item);
  }
  function drawHistory() {
    log.replaceChildren();
    history().forEach(drawMessage);
    if (pending && pendingAgent === active) drawThinking();
    log.scrollTop = log.scrollHeight;
  }
  function drawThinking() {
    const item = document.createElement("div");
    item.className = "chat-message from-agent thinking";
    item.setAttribute("aria-live", "polite");
    const face = document.createElement("span");
    face.className = "message-avatar";
    face.style.backgroundImage = `url("${persona().avatar}")`;
    const text = document.createElement("span");
    text.textContent = `${persona().name} is ${studio && studio.generative ? "reading records and data feeds" : "checking records"}`;
    const dots = document.createElement("span");
    dots.className = "dots";
    dots.textContent = "\u2022\u2022\u2022";
    item.append(face, text, dots);
    log.append(item);
  }
  function drawUsage(usage) {
    const meter = document.getElementById("chat-usage");
    if (!meter) return;
    if (usage && typeof usage.calls === "number") {
      meter.textContent = `${usage.calls} / ${usage.cap} model calls today`;
      meter.hidden = false;
    }
  }
  function selectAgent(id, focus = false) {
    if (!data.agents.some(a => a.id === id)) return;
    active = id;
    selector.value = id;
    const agent = persona();
    document.getElementById("chat-name").textContent = agent.name;
    document.getElementById("chat-role").textContent = agent.role;
    const image = document.getElementById("chat-avatar");
    image.src = agent.avatar;
    image.alt = agent.name + " avatar";
    promptBox.replaceChildren();
    agent.prompts.forEach(question => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "prompt-chip";
      button.textContent = question;
      button.addEventListener("click", () => ask(question));
      promptBox.append(button);
    });
    drawHistory();
    if (focus) {
      document.getElementById("agent-chat").scrollIntoView({behavior: "auto", block: "nearest"});
      input.focus({preventScroll: true});
    }
  }
  async function ask(raw) {
    const original = String(raw).trim().slice(0, 800);
    if (!original || pending) return;
    const question = redactSecrets(original);
    const agent = active, epoch = generation, messages = history();
    messages.push({who: 'user', text: question, at: Date.now()});
    input.value = '';
    let answer;
    if (question !== original) {
      answer = {text: "I hid a possible credential or long token. Don't paste secrets into chat; rotate a key if it was exposed. No message was transmitted or saved to disk.", source: 'Local privacy guard'};
    } else if (studio) {
      pending = true;
      pendingAgent = agent;
      form.setAttribute('aria-busy', 'true');
      document.getElementById('chat-send').disabled = true;
      drawHistory();
      try {
        answer = await request('/api/ask', {agent, text: question});
        if (typeof answer.text !== 'string' || typeof answer.source !== 'string') throw new Error('Invalid reply');
        if ('last_cycle' in answer) data.last_cycle = answer.last_cycle;
        if ('schedule' in answer) data.schedule = answer.schedule;
        if (answer.llm_usage) drawUsage(answer.llm_usage);
        connected = true;
      } catch {
        connected = false;
        answer = {text: 'Local server unavailable or request refused. Current state is unknown. No automatic retry was made; inspect local records before trying a diagnostic again.', source: 'Local connection status'};
      } finally {
        pending = false;
        pendingAgent = null;
        form.setAttribute('aria-busy', 'false');
        document.getElementById('chat-send').disabled = false;
        updateAge();
      }
    } else {
      answer = answerFor(question, agent, data);
    }
    if (epoch !== generation) { if (active === agent) drawHistory(); return; } // Clear means clear, including replies in flight.
    messages.push({who: 'agent', at: Date.now(), ...answer});
    if (messages.length > 41) messages.splice(1, messages.length - 41);
    if (active === agent) drawHistory();
  }

  form.addEventListener("submit", event => {event.preventDefault(); ask(input.value);});
  input.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault(); ask(input.value);
    }
  });
  selector.addEventListener("change", () => {
    const radio = document.getElementById("agent-" + selector.value);
    if (radio) radio.checked = true;
    selectAgent(selector.value);
  });
  document.querySelectorAll('input[name="agent"]').forEach(radio => {
    radio.addEventListener("change", () => {
      const id = radio.id.replace("agent-", "");
      if (id !== "all") selectAgent(id);
    });
  });
  document.querySelectorAll("[data-chat-agent]").forEach(button => {
    button.disabled = false;
    button.addEventListener("click", () => {
      const id = button.dataset.chatAgent;
      const radio = document.getElementById("agent-" + id);
      if (radio) radio.checked = true;
      selectAgent(id, true);
    });
  });
  clear.addEventListener("click", () => {
    generation++;
    threads.clear();
    if (studio && studio.generative) request('/api/clear', {}).catch(() => {});
    active = data.default_agent || active;
    selectAgent(active);
    input.value = "";
    input.focus();
  });

  function updateAge() {
    if (studio) {
      const stamp = Date.parse(data.last_cycle);
      const last = Number.isFinite(stamp) ? new Date(stamp).toISOString().replace('T', ' ').replace(/\.\d{3}Z$/, ' UTC') : 'unknown';
      document.getElementById('chat-snapshot').textContent = connected
        ? `Live local records · last cycle ${last} · ${data.schedule || 'schedule unknown'} · not live quotes`
        : 'Disconnected · current state unknown';
      return;
    }
    const stamp = Date.parse(data.rendered_at);
    const elapsed = Date.now() - stamp;
    const text = !Number.isFinite(stamp) || elapsed < 0 ? "Snapshot time unknown / check clock"
      : elapsed < 60000 ? "Snapshot rendered just now · not live"
      : `Snapshot rendered ${Math.floor(elapsed / 60000)} min ago · not live`;
    document.getElementById("chat-snapshot").textContent = text;
  }
  // Section links reveal their destination even when a role filter hid it.
  // These are local anchors, not routes or trading controls.
  function navigateSection(hash) {
    if (!/^#[a-z-]+$/.test(hash)) return;
    const target = document.getElementById(hash.slice(1));
    if (!target) return;
    if (target.matches('section[data-agent]')) document.getElementById('agent-all').checked = true;
    const details = target.querySelector(':scope > details');
    if (details && details.querySelector('summary').textContent === 'Show technical details') details.open = true;
    document.querySelectorAll('.sidebar nav a').forEach(link => {
      if (link.getAttribute('href') === hash) link.setAttribute('aria-current', 'location');
      else link.removeAttribute('aria-current');
    });
    if (hash === '#agent-chat') input.focus({preventScroll: true});
    else { target.tabIndex = -1; target.focus({preventScroll: true}); }
    target.scrollIntoView({behavior: 'auto', block: 'start'});
  }
  document.querySelectorAll('.sidebar a, .chat-jump, .skip-link').forEach(link => {
    link.addEventListener('click', event => {
      event.preventDefault();
      const hash = link.getAttribute('href');
      if (location.hash !== hash) location.hash = hash;
      navigateSection(hash);
    });
  });
  window.addEventListener('hashchange', () => navigateSection(location.hash));
  if (location.hash) navigateSection(location.hash);

  const expand = document.getElementById("chat-expand");
  if (expand) {
    expand.disabled = false;
    expand.addEventListener("click", () => {
      const wide = document.body.classList.toggle("chat-wide");
      expand.textContent = wide ? "Shrink chat" : "Expand chat";
      expand.setAttribute("aria-pressed", String(wide));
    });
  }
  input.disabled = false;
  document.getElementById("chat-send").disabled = false;
  clear.disabled = false;
  selectAgent(active);
  updateAge();
  setInterval(async () => {
    if (studio && !pending) {
      try {
        const current = await request('/api/snapshot');
        data.last_cycle = current.last_cycle;
        data.schedule = current.schedule;
        if (current.llm_usage) drawUsage(current.llm_usage);
        connected = true;
      } catch { connected = false; }
    }
    updateAge();
  }, 30000);
})();
