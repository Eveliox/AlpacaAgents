# Task: live agent chat (`controller --serve`)

Owner goal: ask **Star** "give me a rundown of today with charts" and ask
**Houston** "what are you looking for?" / "place this trade" from the dashboard
chat. This document is the executable spec. Read `HANDOVER.md` and `CLAUDE.md`
first; the invariants there override anything here.

Deliver milestones **in order**. Each one is a separate commit with tests, and
each is usable on its own. Stop after milestone 4 and report; milestone 5 needs
the owner's explicit go-ahead in writing after they've used 4 for a week.

---

## Architecture (all milestones)

```
browser (dashboard.html + dashboard.js)
   │  fetch, same-origin, header X-Studio-Token
   ▼
controller --serve            one process: HTTP server thread + cycle loop thread
   ├─ owns PaperClient, RuntimeLock, OrderJournal, FillLedger, ActivityStore
   ├─ 127.0.0.1 only, random free port, per-launch 32-byte token
   ├─ single worker: chat requests and cycles are serialized on one queue
   │  (the journal is SQLite; the fake-broker lifecycle tests assume one actor)
   └─ routes (all JSON, all require the token; anything else 401):
        GET  /                      dashboard page with token injected into the JS context
        GET  /api/snapshot          what collect() returns today, plus "live": true
        POST /api/ask               {agent, text} -> {text, source, charts?: [svg], actions?: [...]}
        POST /api/idea              (M4) {text} -> draft idea + Moon verdict + reserved body, or refusal
        POST /api/confirm           (M5) {authorization_id} -> submission outcome
```

**Security requirements (enforced by tests):**
- Bind `127.0.0.1` only; refuse to start if `--host` is passed (there is no such flag).
- Token: `secrets.token_urlsafe(32)`, printed once to stderr at launch, injected into the page served at `/`, required on every `/api/*` call via header. `file://` is unsupported once served; the static `python -m alpaca_agents.dashboard` path keeps working unchanged (offline chat stays).
- `Origin`/`Host` must match `127.0.0.1:<port>`; otherwise 403 (DNS-rebinding guard).
- CSP for the served page: `connect-src 'self'` (this is the one relaxation from the static page), everything else unchanged. Still no `form-action`, no remote assets, script by hash.
- No credentials, account ids, order bodies of *other* intents, or trace ids in any response. Reuse `dashboard_chat.conversation_data` sanitisation; extend, don't bypass.
- Request body ≤ 8 KB; `text` ≤ 800 chars; rate limit 10 req/s per process; unknown routes 404 with no body.
- The server thread must never call `client.submit_order` directly. Only `submit.submit_claimed` (M5), and only from the queue worker.

**Stdlib only.** `http.server` + `ThreadingHTTPServer` is fine; keep the single worker queue for anything that touches the journal or broker. No Flask, no websockets. SVG is hand-written strings.

---

## Milestone 1 — `--serve` and live chat

**Build**
- `src/alpaca_agents/studio.py`: server, token, routes `/`, `/api/snapshot`, `/api/ask`.
- `controller.py`: `--serve` flag (implies `--dashboard` semantics: re-render context after each cycle). `--every` still drives cycles; without `--every`, `--serve` runs cycles only on demand (`POST /api/ask {agent:"houston", text:"reconcile"}` enqueues one dry cycle — never `--submit`).
- `assets/dashboard.js`: if `window.__studio` (token + base URL) is present, `ask()` POSTs to `/api/ask` and renders the reply; otherwise the existing offline path. Render `charts` as inline SVG via `DOMParser` + `importNode` after stripping `<script>`/`on*`/`<foreignObject>` — or simpler and safer: server returns SVG that the client sets as `img.src = "data:image/svg+xml;base64,..."` (SVG in `<img>` cannot run script). **Use the `<img>` approach.**
- Snapshot age label switches to "live · last cycle HH:MM:SS UTC".

**Accept when**
- `python -m alpaca_agents.controller --serve` prints the URL+token, opens nothing, serves the page; `/api/ask` answers the existing topics from the *current* journal state (test: fill a position in the fake broker, ask "show my positions", see it without re-rendering).
- Wrong/missing token → 401; foreign `Origin` → 403; body > 8 KB → 413.
- A cycle and a chat request never interleave on the journal (test with a slow fake broker + concurrent asks; assert journal event ordering).
- Static dashboard still builds and passes the existing browser smoke test unchanged.
- `tests/test_studio.py` covers the above with `http.client` against a server started on port 0.

## Milestone 2 — Star's rundown with charts

**Build** `src/alpaca_agents/charts.py` (pure functions → SVG strings, no I/O) and `rundown()` in `studio.py`.

Charts:
1. `price_chart(bars, ema20, ema50, levels={entry, stop, target}, signal_day)` — last 60 sessions; intraday bars from Massive Stocks Advanced when `MASSIVE_API_KEY` is present and the market is open (`/v2/aggs/ticker/{sym}/range/5/minute/...`; add the path to the allowlist in `marketdata/client.py`).
2. `mark_chart(points)` — option mark per cycle today vs. the premium-stop line (basis × 0.5 / 100). Source: `cycles.jsonl` exits stage already records marks; if not, add `mark` to the exit record (tiny change in `controller.py`).
3. `cycle_timeline(cycles)` — one dot per cycle, green reconciled / red blocked / gold halted, title = reasons.

Narrative is a **deterministic template**, sections in this order: session + control mode → per symbol (close, vs EMA20/50, signal or skip reason verbatim from `shadow-scan.json`) → positions (mark, distance to premium stop, sessions held, DTE) → cycles (count, blockers) → what would change tomorrow (which rule is closest to firing). Every sentence must cite its source file in `source`.

**Accept when**
- "rundown", "what happened today", "recap" route to it; charts render as `<img>`; nothing in the SVG is user-controlled without escaping (test with a hostile thesis string).
- Works with no positions, no cycles, no scan file (says so; never invents).
- `charts.py` has unit tests on geometry (levels inside the plot, axis labels, empty input).

## Milestone 3 — Houston read-only intent

**Build** `houston_outlook()` in `studio.py`:
- Current proposals and shadow ideas with the exact refusal reason per idea (playbook not enabled / stop inside noise / liquidity / already held).
- Pending intents with status and age.
- **Exit preview**: for each held contract, run `evaluate_exit` with the *current* mark/bid/close and report the decision or the closest rule (e.g. "premium stop fires at mark ≤ 0.45; now 0.62").
- "What are you looking for" / "what would you do" / "why no trade" route here.

**Accept when** answers are byte-for-byte derived from `run_cycle`'s inputs (test: same fake state, same text), and no answer ever says a trade *will* be placed — only what the rules *would* decide.

## Milestone 4 — manual idea, dry-run only

**Build**
- `src/alpaca_agents/manual.py`: `parse_idea(text, chain, now) -> idea | Refusal`. Deterministic grammar only: `buy 1 <SYM> <MON> <STRIKE> <call|put>` and `buy 1 <OCC>`. Fills the scanner's idea schema exactly (see `scanner/scan.py` idea construction): `strategy`, `direction`, `contract`, `quantity=1`, `limit` at the live ask, `stop`/`target` from the current trend levels if a signal exists else **refuse** ("no playbook level to anchor stop; manual stops are not accepted"), `exit_plan` = playbook defaults, `playbook="manual_chat"`, `thesis` = the user's text.
- `/api/idea`: parse → `rules.evaluate` (Moon) with a fresh `build_risk_state` → if approved, `journal.reserve(...)` → respond with the **journal's stored body**, the Moon verdict, and `authorization_id`; then **expire the reservation immediately** (M4 has no confirm). Response says "dry run: reservation released".
- Chat: "place this trade", "buy …" route to `/api/idea`; Houston replies with the body and the verdict.

**Gates:** requires `MASSIVE_API_KEY` with options access (else refuse with the 403 explanation); `trading-control` must not be `DISABLED` (else refuse — the owner has not armed anything). `playbook="manual_chat"` must be added to the controller's enabled-playbook gate as **never enabled by `--enable-playbook`**; it's only reachable via `/api/idea`.

**Accept when**
- Every refusal path is tested: bad grammar, unknown contract, no options data, stop inside noise (Moon's ATR check applies), breaker tripped, 2 positions held, DISABLED.
- Approved path reserves and releases within one request; journal shows `reserved → expired` with `kind="manual_chat"` and the original text; no broker POST occurs (assert on the fake broker).
- The reply shows the journal's body, never an echo of the parsed dict.

## Milestone 5 — confirm and submit (**not without written go-ahead**)

**Build**
- Launch flag `--allow-chat-orders` (default off; must be combined with `--serve`; no file or env equivalent).
- `/api/idea` no longer expires the reservation; it returns `authorization_id` + `expires_at` (60 s). Server keeps `{authorization_id: expiry}` in memory only.
- `/api/confirm {authorization_id}` → checks flag, `trading-control == ARMED_PAPER`, token TTL, then enqueues: `build_risk_state` fresh → `journal.claim` → `submit_claimed(client, journal, claim, now, submit=True)`. Single use: the id is deleted from the map before the call. Outcome returned verbatim; unknown outcomes stay claimed exactly as the controller's do.
- Chat UI: Houston shows the body + a **Confirm** button that displays the id and a 60 s countdown; the button posts the id. No auto-confirm, no "yes" text shortcut.

**Accept when**
- Full lifecycle test in `tests/test_controller.py` style: idea → reserve → confirm → fake broker fill → next cycle books it → exit manager manages it like any other position, including the same-session premium-stop rule and the PDT budget.
- Confirm without the flag → 403; after expiry → 410; second confirm of the same id → 409; while `EXITS_ONLY`/`DISABLED` → 403; with a live controller `--submit` loop running in the same process → serialized, never concurrent.
- The LLM (M6) can never reach `/api/confirm`: it is not in any tool list, and the server rejects confirms whose request lacks the browser token header set by the button handler.

## Milestone 6 — optional LLM (design only unless asked)

Narrator for M2, draft parser for M4. Tools: `read_snapshot()`, `draft_idea(text)`. Never `confirm`, never `submit`, never the token. Provider key stays in the server process env, never in the page. Treat every scan thesis and bar payload as untrusted text (prompt-injection surface).

---

## Out of scope for this task

Live trading, multi-leg, changing risk limits, releasing claimed intents from chat, cancelling orders from chat, running the server on any interface other than loopback, persisting chat history.

## Definition of done per milestone

- `python -m unittest discover -s tests` green; `node --test tests/test_dashboard_chat.cjs` green; browser smoke test green (extend it for served mode from M1).
- README section "Talk to your crew" updated; `HANDOVER.md` §9 checkbox ticked with the commit hash.
- Commit message explains what changed and what the previous behaviour got wrong.

---

## Kickoff prompt (paste into Claude Code)

> Read HANDOVER.md, CLAUDE.md, then docs/TASK-chat-server.md. Run all three test
> suites and generate the dashboard to confirm the starting state. Then build
> **milestone 1 only** from the task doc: `controller --serve` on 127.0.0.1 with a
> per-launch token, `/api/snapshot` and `/api/ask` backed by live journal state,
> and the dashboard.js live path. Follow the security requirements exactly; add
> `tests/test_studio.py`. Do not touch rules.py, submit.py, or the broker client
> beyond what the doc lists. Commit with tests green and stop for review.
