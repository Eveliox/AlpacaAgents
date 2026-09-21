# Agent Desk — proposed integration scope

Status: owner approved the read-only first build. Phase A implemented; Phase B
remains deferred. This scope does not authorize new order, approval, control,
risk-sizing, WebSocket or broker paths.

Sources checked: CLAUDE.md, HANDOVER.md, PLAYBOOK.md,
docs/TASK-chat-server.md, controller.py, studio.py, rules.py, scanner/scan.py.
The hard rules override the generic swarm blueprint.

## Goal

Add Agent Desk as a module in the existing dashboard, not a replacement UI or
new trading system. Show recorded evidence, stage decisions, refusals, proposals
and subsequent outcomes. Do not present chat personas as independent trading
models or fabricate agreement between deterministic pipeline stages.

Keep the existing stdlib Python backend, loopback server, token/Origin/Host
guards, single runtime owner, worker queue, local journals and bundled JS/CSS.
No React/FastAPI/Redis/PostgreSQL migration. No broker/data/LLM calls on opening
the desk. Static mode stays offline; served mode reads updated local records.

## Six honest views, not six invented models

| Blueprint role | Proposed view | Existing source / limitation |
|---|---|---|
| Spotter | Star / Setups | scanner signals, contract selection, skipped ideas |
| Prior | Research / Evidence | saved underlying backtests; not a calibrated probability |
| Edge | Research / Validation gaps | options expectancy is not modeled; do not infer it from underlying R |
| Kelly | Moon / Risk budget | existing premium + fees, cash and position checks; no Kelly sizing |
| Taker | Houston / Entry | recorded candidate, reserve/claim/submission states; no new execution path |
| Closer | Houston / Exit | existing deterministic exit decisions and refusals |

Astra/Nova explain sanitized facts outside the decision path. Research summaries
must keep mean, median, profit factor, worst R and sample counts together and
retain options_pnl_modelled: false. Missing probability, confidence, latency,
inputs or outcomes are null/Unknown/Not implemented, never synthetic zeroes.

An options candidate must distinguish underlying bias from the option purchase:
a bearish setup may buy a put; it does not authorize short stock or naked options.
Show OCC contract, call/put, expiry, strike, contract quantity, multiplier 100,
limit premium, estimated fees and maximum entry risk separately from underlying
entry/stop/target levels. Stop distance is not the maximum-loss formula.

## Existing behavior that must remain visible

- run_cycle already creates a cycle_id. Reuse it; add candidate IDs within a cycle
  rather than treating an entire multi-symbol cycle as one trade.
- Actual order: reconcile -> manage existing exits -> entry-mode gate -> scan ->
  candidate gates -> reserve/risk -> optional claim/revalidation/submission.
  Existing positions link back to their entry proposal across subsequent cycles.
- DISABLED skips entry scanning. EXITS_ONLY does not scan for new entries either.
  A halted/skipped stage is not analyzing, neutral, or a completed no-setup result.
- cycles.jsonl currently saves a completed report with stage summaries. It is not
  a live per-agent event stream or a complete frozen market-input archive.
- Standalone shadow-scan.json is not automatically part of the latest cycle.
  Never join it by symbol/time proximity and pretend it influenced that cycle.
- Correct the shadow-mode wording in PLAYBOOK.md in a follow-up implementation:
  the documented DISABLED loop does not currently exercise the scanning stages.
  This plan does not change the mode gates to make that documentation true.

## Phase A — read-only desk using existing records

1. Add an Agent Desk sidebar destination inside the existing dashboard.
2. Provide cycle selection/history, six stage cards, a central dependency view,
   a details panel and a clickable evidence/activity list.
3. Show what was evaluated versus skipped, with recorded reasons and timestamps.
   Show no consensus probability: 'setup found; risk refused' is a gate outcome,
   not a disagreement between two predictors.
4. Display sanitized candidate and exit details only where records support them.
   Show Unknown when historical reports lack inputs/proposals/linkage.
5. Add guarded read-only state/history/detail endpoints in studio.py, reusing its
   display projection and serialized worker. Use bounded cursor polling initially.
   Label this as refreshed recorded state, not a live intra-cycle stream. Reads
   may wait behind controller work. No /run, /approve, /reject or order routes.
6. Use a static SVG/CSS flow with a text equivalent. No idle animation implying
   activity; no edge weights without defined recorded meaning. Connection loss
   becomes stale/disconnected, never six agents 'live'.

Keep raw broker/account IDs, authorization capabilities, stored order bodies,
credentials and private traces out of HTTP responses and LLM inputs. Internal
references may link journal records; expose separate sanitized display IDs.

### Phase A implementation notes

- `agent_desk.py`: bounded cycle-file reader, fixed-field projection, exact
  controller decision-key join to read-only SQLite. Native controller UUIDs are
  retained; older records get hashed display IDs and cannot join to the journal.
- `agent_desk_view.py` / `assets/agent_desk.js`: additive collapsible module with
  six cards, flow, details, proposals, activity and cycle selection. Static
  latest-cycle evidence remains accessible without JS.
- Studio GET routes: `/api/agent-desk/state?after=<content revision cursor>`,
  `/api/agent-desk/cycles`, `/api/agent-desk/cycles/<id>`. Same auth/worker as
  existing reads; no new action routes. Poll every 15s only while open, not behind
  a pending chat request. Unknown detail IDs return 404.
- 20 records / 512 KiB tail / 64 KiB per record / 12 rows per detail list.
  Omitted/corrupt/duplicate/incomplete records have explicit warnings. This is a
  bounded view, not full historical replay. Cursor is content-based, not a live
  event sequence. Selection stays pinned; aged-out snapshots are labeled.
- Journal status is observed at read time and never replaces the historical
  cycle's risk/submission verdict. Entry linkage across future exit cycles and
  fill-level/P&L attribution remain unknown rather than guessed.
- Existing rules/controller/submitter remain unchanged. PLAYBOOK.md's incorrect
  claim that DISABLED performs scanning is corrected.

## Phase B — first-class event capture and frozen evidence

Instrument the existing orchestration boundaries, not rules.py or exits.py.
Use a passive event recorder, not a second orchestrator or autonomous agent bus.

Envelope: schema_version, event_id, cycle_id, candidate_id (when relevant),
sequence, recorded_at, event_type, stage, typed allowlisted payload and provenance.
Persist append-only bounded payloads beside the existing runtime audit records.
The existing order/fill journals remain authoritative; events reference them
rather than create duplicate positions, executions or balances.

Capture actual starts/completions, input failures, rejected/skipped decisions,
candidates, reserve/claim/submission outcomes and later verified fills/exits.
Do not emit 'order.filled' merely because submission was acknowledged. Unknown
submission outcomes remain claimed for reconciliation; no retry.

Freeze the inputs actually consumed: completed bar session/timeframe and relevant
bars/indicators, underlying price, option contract and quote timestamp, bid/ask,
freshness validation, fees, cash/risk state, mode/playbook gates and rule/config
version. Record both market observation and receipt times, with timezone and
units. Preserve missing evidence as missing. Historical inspection must never
fetch today's quotes to explain yesterday's decision.

Producer publishes immutable sanitized observations. Streaming consumers never
access the broker or SQLite concurrently with the controller worker. Use bounded
buffers/cursors with explicit gaps on overflow. Observer failures must not
rewrite rule verdicts, imply a completed cycle, create permissive defaults or
cause an order retry; retain the established controller error behavior and mark
telemetry incomplete. Specify and test recorder-failure handling before hooks.

Current chat-server spec explicitly says no WebSockets. True push transport is a
separate reviewed change; do not silently add a WebSocket server, relax auth, or
put the launch token into URLs. Polling is enough for Phase A. Phase B transport
must demonstrate reconnect/gap handling and non-blocking observation first.

## Approval and risk boundary

The blueprint's approve -> execute path is OUT of this initial scope. A review
annotation could be considered later, clearly labeled non-executable and stored
separately from playbook approvals and order authorizations. Do not present an
'Approve trade' button that does nothing or appears to arm trading.

Actual human-confirmed submission remains the separately gated M4/M5 work in
TASK-chat-server.md: dry-run review first, then explicit written authorization
for confirmation capability design and the existing claim/submit boundary.

Preserve $2,000 capital cap, $100 maximum entry risk including estimated fees,
2 concurrent positions, $40 cumulative daily loss breaker, existing liquidity,
playbook and reconciliation checks, and same-session/PDT exit policy. Generic
2%/5% examples and Kelly sizing do not replace these rules. Moon wins; LLM text
cannot influence permissions. Paper only; no new live endpoint or flag.

## Acceptance criteria

- Existing dashboard, static mode, chat and all controller lifecycle tests pass.
- Opening/polling the desk makes no broker requests, runs no cycles, changes no
  control/approval files and does not reserve, claim, submit or cancel anything.
- Token, Origin, Host, CSP, redaction and bounded reads apply to all new endpoints.
- Explicit cases: missing/corrupt/stale records, disabled, exits-only, halted
  reconciliation, no option entitlement, missing model, empty inventory, rejected
  idea, expired dry-run reservation, unknown submission outcome, late fill.
- Future event tests: cross-cycle isolation, immutable snapshots, exact provenance,
  duplicate/out-of-order handling, cursor gaps, restart, latency measured rather
  than invented, no observer effect on rules or submission count.
- Diagram and details agree on sources and states; risk pass, human review,
  authorization, submission and verified fill cannot collapse into 'approved'.
- Browser coverage: keyboard details/navigation, mobile layout, reduced motion,
  untrusted strings rendered safely, disconnected state, static/offline fallback.
- Run python -m unittest discover -s tests, node --test tests/test_dashboard_chat.cjs
  and static/served/fake-generative browser tests before implementation commits.

## Deferred

Calibrated prior, option-level expectancy, Kelly sizing, agent accuracy metrics,
new market-data feeds, weighted Sankey, automated trade approval, multi-leg
execution, live-money trading and new broker boundaries. These are not UI tasks.
