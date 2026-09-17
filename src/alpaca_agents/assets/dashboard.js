/* Offline Layer 4 guide. No network, storage, command execution or broker access. */
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
  const selector = document.getElementById("chat-agent");
  const log = document.getElementById("chat-log");
  const input = document.getElementById("chat-input");
  const form = document.getElementById("chat-form");
  const promptBox = document.getElementById("chat-prompts");
  const clear = document.getElementById("chat-clear");
  const threads = new Map();
  let active = "astra";

  function persona() { return data.agents.find(a => a.id === active); }
  function history() {
    if (!threads.has(active)) threads.set(active, [{who: "agent", text: persona().intro, source: "Local, rule-based guide · no generative model connected"}]);
    return threads.get(active);
  }
  function drawMessage(message) {
    const item = document.createElement("div");
    item.className = `chat-message ${message.who === "user" ? "from-user" : "from-agent"}`;
    const label = document.createElement("span");
    label.className = "message-name";
    label.textContent = message.who === "user" ? "You" : persona().name;
    const body = document.createElement("p");
    body.textContent = message.text;
    item.append(label, body);
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
    log.scrollTop = log.scrollHeight;
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
  function ask(raw) {
    const original = String(raw).trim().slice(0, 800);
    if (!original) return;
    const question = redactSecrets(original);
    const answer = question !== original
      ? {text: "I hid a possible credential or long token. Don't paste secrets into chat; rotate a key if it was exposed. No message was transmitted or saved to disk.", source: "Local privacy guard"}
      : answerFor(question, active, data);
    const userMessage = {who: "user", text: question};
    const response = {who: "agent", ...answer};
    const messages = history();
    messages.push(userMessage, response);
    // Bound both memory and rendered history: latest 20 question/answer pairs.
    if (messages.length > 41) {
      messages.splice(1, messages.length - 41);
      drawHistory();
    } else {
      drawMessage(userMessage);
      drawMessage(response);
      log.scrollTop = log.scrollHeight;
    }
    input.value = "";
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
    threads.clear();
    selectAgent(active);
    input.value = "";
    input.focus();
  });

  function updateAge() {
    const stamp = Date.parse(data.rendered_at);
    const elapsed = Date.now() - stamp;
    const text = !Number.isFinite(stamp) || elapsed < 0 ? "Snapshot time unknown / check clock"
      : elapsed < 60000 ? "Snapshot rendered just now · not live"
      : `Snapshot rendered ${Math.floor(elapsed / 60000)} min ago · not live`;
    document.getElementById("chat-snapshot").textContent = text;
  }
  input.disabled = false;
  document.getElementById("chat-send").disabled = false;
  clear.disabled = false;
  selectAgent(active);
  updateAge();
  setInterval(updateAge, 30000);
})();
