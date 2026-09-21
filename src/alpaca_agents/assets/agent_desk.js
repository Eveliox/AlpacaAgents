/* Agent Desk is a passive reader. All rendering uses DOM nodes and text.
   No order, approval, controller-run, market-data, or model endpoint is called. */
function initializeAgentDesk({state, request, live, canRead}) {
  const panel = document.getElementById('desk-panel');
  if (!panel || !state) return;
  const select = document.getElementById('desk-cycle');
  const follow = document.getElementById('desk-follow');
  const refreshButton = document.getElementById('desk-refresh');
  const connection = document.getElementById('desk-connection');
  const inspector = document.getElementById('desk-inspector');
  const cards = [...document.querySelectorAll('[data-desk-agent]')];
  const replayButton = document.getElementById('desk-replay');
  const swarm = document.querySelector('.desk-swarm');
  const core = document.querySelector('.desk-core');
  const links = [...document.querySelectorAll('.desk-connectors path')];
  let following = true, selected = state.cycles[0] || null, role = 'spotter';
  let reading = false, disconnected = false, replayToken = 0;
  // Mirror of TONES in agent_desk_view.py. Unknown statuses are amber, never green.
  const TONES = {passed_at_cycle: 'pass', recorded: 'pass', risk_pass: 'pass', submission_recorded: 'pass',
                 dry_run: 'pass', ARMED_PAPER: 'pass', EXITS_ONLY: 'warn',
                 blocked: 'stop', rejected: 'stop', error: 'stop', uncertain: 'stop', halted: 'stop'};
  const tone = status => TONES[status] || 'warn';
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  const node = (tag, text, cls) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (cls) element.className = cls;
    return element;
  };
  const shown = value => value === null || value === undefined ? 'Unknown' : typeof value === 'boolean' ? (value ? 'Yes (recorded)' : 'No (recorded)') : String(value);
  const human = value => shown(value).replace(/_/g, ' ');
  function fields(items) {
    const dl = node('dl');
    items.forEach(([label, value]) => dl.append(node('dt', label), node('dd', shown(value))));
    return dl;
  }
  function json(value) { return node('pre', JSON.stringify(value, null, 2)); }
  function freshness() {
    const when = Date.parse(selected && selected.completed_at);
    const delta = Date.now() - when;
    const age = !Number.isFinite(when) ? 'Completion time unknown' : delta < 0 ? 'Future timestamp — check clock' :
      delta > 60000 ? `Historical / stale cycle · ${Math.floor(delta / 60000)} min since completion` : 'Recently recorded cycle, not proof of a running controller';
    const retained = selected && !state.cycles.some(c => c.id === selected.id);
    document.getElementById('desk-freshness').textContent = `${age}. Last read: ${shown(state.read_at)}. ${retained ? 'Pinned snapshot is outside the current history window; its journal status is not refreshed.' : 'Journal status, when linked, is current at that read; cycle decisions remain historical.'}`;
  }
  function inspect(agent, focus = false) {
    inspector.replaceChildren();
    cards.forEach(card => card.setAttribute('aria-pressed', String(agent && card.dataset.deskAgent === agent.id)));
    if (!agent) {
      inspector.append(node('h3', 'No recorded cycle'), node('p', 'Unknown is not idle. Viewing the desk does not start a cycle.', 'dim'));
      return;
    }
    inspector.append(node('h3', agent.name), node('p', agent.summary), fields([
      ['Recorded state', human(agent.status)], ['Confidence', agent.confidence], ['Latency (ms)', agent.latency_ms],
      ['Model', agent.model], ['Input snapshot', agent.inputs === null ? 'Not captured in these reports' : shown(agent.inputs)],
      ['Source', agent.source || 'Not implemented / no cycle evidence'], ['Cycle', selected.id]
    ]), json(agent.outputs));
    if (focus) inspector.focus({preventScroll: true});
  }
  function drawProposals() {
    const box = document.getElementById('desk-proposals');
    box.replaceChildren(node('h3', 'Entry proposals & recorded risk'));
    if (!selected || !selected.entries.length) {
      box.append(node('p', 'No evaluated entry records in this cycle. This does not imply no opportunities or a completed scan.', 'desk-empty'));
      return;
    }
    selected.entries.forEach(entry => {
      const detail = node('details', undefined, 'desk-proposal');
      const heading = node('summary');
      heading.append(node('strong', `${shown(entry.symbol)} · ${shown(entry.playbook)}`),
        node('span', entry.risk_pass_at_reservation === true ? 'Risk passed at reservation · not human approval' : entry.risk_pass_at_reservation === false ? 'Rejected at reservation' : 'Risk verdict unknown'));
      detail.append(heading, fields([
        ['Reason', entry.reason], ['Claimed at cycle', entry.claimed_at_cycle], ['Claim reason', entry.claim_reason],
        ['Submission outcome at cycle', entry.submission.outcome], ['Broker status at submission', entry.submission.broker_status],
        ['Journal status at last read', entry.journal && entry.journal.journal_status_now]
      ]));
      const p = entry.journal && entry.journal.proposal;
      if (p) {
        detail.append(fields([
          ['Contract', p.contract], ['Strategy', p.strategy], ['Underlying bias (not order side)', p.underlying_bias],
          ['Call / put', p.right], ['Expiry', p.expiration], ['Strike', p.strike], ['Contracts', p.quantity], ['Multiplier', p.multiplier],
          ['Limit premium / share (USD)', p.limit_premium_usd], ['Estimated fees (USD)', p.estimated_fees_usd],
          ['Estimated maximum entry risk (USD)', p.estimated_max_risk_usd], ['Underlying entry', p.underlying_entry],
          ['Underlying stop', p.underlying_stop], ['Underlying target', p.underlying_target],
          ['Saved premium stop (%)', p.exit_plan.premium_stop_pct], ['Saved time stop (DTE)', p.exit_plan.time_stop_dte],
          ['Saved time stop (sessions)', p.exit_plan.time_stop_sessions], ['Saved underlying exit rule', p.exit_plan.underlying_stop_rule],
          ['Recorded reasoning', p.reasoning]
        ]), node('p', p.risk_basis, 'desk-source'), node('p', entry.journal.source, 'desk-source'));
      } else detail.append(node('p', 'Proposal economics unknown: no exact journal match. Current scans or quotes are not substituted.', 'desk-empty'));
      detail.append(node('p', `${entry.source} · ${entry.id}`, 'desk-source'));
      box.append(detail);
    });
    box.append(node('p', 'Claimed / unknown submission requires reconciliation, never a retry. A submitted order is not a verified fill. No approval or submission controls.', 'desk-source'));
  }
  function drawActivity() {
    const box = document.getElementById('desk-activity');
    box.replaceChildren(node('h3', 'Recorded activity'));
    box.append(node('p', 'Stage summaries in controller order. Individual event times were not captured; these are not live agent messages.', 'desk-source'));
    if (!selected) return;
    selected.activity.forEach(item => {
      const detail = node('details', undefined, 'desk-activity-item');
      const heading = node('summary');
      heading.append(node('strong', item.label), node('span', human(item.status)));
      detail.append(heading, node('p', `Report completed: ${shown(item.recorded_at)} · source: cycles.jsonl / ${item.source}`, 'desk-source'), json(item.detail));
      box.append(detail);
    });
  }
  function draw() {
    const list = state.cycles.slice();
    if (selected && !list.some(c => c.id === selected.id)) list.push(selected);
    select.replaceChildren();
    if (!list.length) select.append(node('option', 'No recorded cycles'));
    list.forEach(c => {
      const option = node('option', `${shown(c.completed_at)} · ${human(c.status)} · ${c.id.slice(0, 12)}`);
      option.value = c.id;
      select.append(option);
    });
    select.disabled = !list.length;
    if (selected) select.value = selected.id;
    follow.disabled = !state.cycles.length;
    follow.setAttribute('aria-pressed', String(following));
    follow.textContent = following ? 'Following latest' : 'Follow latest';
    document.getElementById('desk-cycle-title').textContent = selected ? `${selected.id} · ${shown(selected.control_mode_at_cycle)} · submission setting: ${shown(selected.submit_at_cycle)}` : 'No recorded cycle';
    const flow = document.querySelector('.desk-flow');
    flow.replaceChildren();
    if (selected) selected.flow.forEach(step => {
      const li = node('li'); li.dataset.tone = tone(step.status);
      li.append(node('strong', step.name), node('span', human(step.status))); flow.append(li);
    });
    else flow.append(node('li', 'No recorded flow. Unknown is not idle.'));
    cards.forEach(card => {
      const agent = selected && selected.agents.find(a => a.id === card.dataset.deskAgent);
      card.disabled = !agent;
      card.dataset.status = agent ? agent.status : 'unknown';
      card.dataset.tone = tone(card.dataset.status);
      card.querySelector('.desk-state').textContent = agent ? human(agent.status) : 'Unknown';
    });
    core.dataset.tone = selected ? tone(selected.status) : 'warn';
    document.querySelector('.desk-core strong').textContent = selected ? human(selected.status) : 'Unknown';
    replayButton.disabled = !selected;
    replay();
    inspect(selected && selected.agents.find(a => a.id === role));
    drawProposals(); drawActivity();
    document.getElementById('desk-evidence').textContent = selected ? JSON.stringify(selected, null, 2) : 'No saved cycle. This page does not start one.';
    const warnings = document.getElementById('desk-warnings');
    warnings.replaceChildren(...state.warnings.map(w => node('li', w)));
    if (selected && selected.details_truncated) warnings.append(node('li', 'Cycle details exceed the display bound; additional rows are omitted.'));
    document.getElementById('desk-history-note').textContent = `Up to ${state.history_limit} saved cycles from a bounded file tail. ${state.history_truncated ? 'Older history omitted.' : 'History may be incomplete.'} Research and fills are not joined by proximity.`;
    freshness();
  }
  // Replays the SAVED decision path: each stage lights in controller order and the signal only
  // advances past a stage the record shows as passed. It stops, amber or red, where the cycle
  // ended. Pure presentation of stored statuses; nothing is polled, predicted or executed.
  function replay() {
    const token = ++replayToken;
    const steps = [...document.querySelectorAll('.desk-flow li')];
    steps.forEach(li => li.classList.remove('lit', 'signal'));
    cards.forEach(c => c.classList.remove('lit'));
    core.classList.remove('lit');
    links.forEach(p => p.classList.remove('lit'));
    swarm.classList.remove('flowing');
    if (!selected) return;
    const instant = reducedMotion.matches;
    const at = (ms, fn) => instant ? fn() : setTimeout(() => { if (token === replayToken) fn(); }, ms);
    let delay = 0, open = true;
    steps.forEach((li, i) => {
      if (!open) return;
      at(delay, () => li.classList.add('lit'));
      if (li.dataset.tone === 'pass' && i < steps.length - 1) {
        at(delay + 250, () => li.classList.add('signal'));
        delay += 480;
      } else open = false;
    });
    delay += 350;
    const light = id => {
      const card = cards.find(c => c.dataset.deskAgent === id);
      const link = links.find(p => p.dataset.link === id);
      if (card) card.classList.add('lit');
      if (link && card) { link.dataset.tone = card.dataset.tone; link.classList.add('lit'); }
    };
    at(delay, () => swarm.classList.add('flowing'));
    ['spotter', 'prior', 'edge'].forEach((id, i) => at(delay + i * 160, () => light(id)));
    delay += 620;
    at(delay, () => core.classList.add('lit'));
    delay += 320;
    ['risk', 'entry', 'exit'].forEach((id, i) => at(delay + i * 160, () => light(id)));
    at(delay + 1800, () => swarm.classList.remove('flowing'));
  }
  async function refresh() {
    if (!live || reading || !panel.open || document.hidden || !canRead()) return;
    reading = true;
    refreshButton.disabled = true;
    connection.textContent = 'Reading local records… may wait behind controller work. Existing evidence is not a live status.';
    try {
      const next = await request('/api/agent-desk/state' + (state.cursor ? '?after=' + state.cursor : ''));
      if (next.unchanged === true && next.cursor === state.cursor) state.read_at = next.read_at;
      else {
        if (next.schema_version !== 1 || !Array.isArray(next.cycles) || !Array.isArray(next.warnings)) throw new Error('Invalid desk state');
        state = next;
        selected = following ? state.cycles[0] || null : state.cycles.find(c => selected && c.id === selected.id) || selected;
        draw();
      }
      disconnected = false;
      connection.textContent = 'Connected to local records · polls every 15s while open · not a live stage stream.';
    } catch {
      disconnected = true;
      connection.textContent = 'Disconnected / read refused — current state unknown. Showing retained evidence only; no cycle or order retry.';
    } finally {
      reading = false;
      refreshButton.disabled = false;
      freshness();
    }
  }
  cards.forEach(card => card.addEventListener('click', () => {
    role = card.dataset.deskAgent;
    inspect(selected && selected.agents.find(a => a.id === role), true);
  }));
  select.addEventListener('change', () => {
    const found = state.cycles.find(c => c.id === select.value);
    if (found) selected = found;
    following = false;
    draw();
  });
  follow.addEventListener('click', () => { following = true; selected = state.cycles[0] || null; draw(); });
  replayButton.addEventListener('click', replay);
  refreshButton.disabled = !live;
  refreshButton.hidden = !live;
  refreshButton.addEventListener('click', refresh);
  panel.addEventListener('toggle', () => { if (panel.open) refresh(); });
  connection.textContent = live ? 'Local recorded state · open the desk to refresh. Not a live stage stream.' : 'Offline snapshot · no network · rebuild to update.';
  draw();
  setInterval(() => {
    freshness();
    // A failed read needs an explicit refresh/reopen, not an automatic retry loop.
    if (!disconnected) refresh();
  }, 15000);
}
