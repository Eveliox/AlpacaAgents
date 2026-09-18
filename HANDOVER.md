# Handover: AlpacaAgent

Paper-only automated options swing-trading system with four named roles and a
read-only dashboard. Milestone 1 of the local chat server is now implemented
(see §9; earlier trading/backtest baseline: `33b43de`). Read this file, then `CLAUDE.md` (hard rules), then
`README.md` (full reference) and `PLAYBOOK.md` (operator routine).

Owner: individual trader, Windows 11, PowerShell, Python 3.13, Node 22.
Repo: https://github.com/Eveliox/AlpacaAgents

---

## 1. What exists (Python + Node tests, static and served browser smoke tests)

| Layer | Module | Role name | Status |
|---|---|---|---|
| 1 Scanner | `scanner/` | **Star** | 4 playbooks (`trend_directional`, `trend_debit_spread`, `oversold_bounce`, `breakout_continuation`), all disabled by default. Massive/Polygon data adapter. Refuses delayed quotes. |
| 2 Rules | `rules.py` | **Moon** | Pure, deterministic. $100 max risk incl. fees, 2 positions, $40 daily loss breaker, cash semantics, fail-closed. No I/O, no LLM. |
| 3 Executor | `executor/` | **Houston** | Only broker boundary. Paper URL hardcoded. Journal with reserve→claim→submit, single-use auth, stored bodies. Reconciliation with real checks. Exit manager. Manual `release-intent` / `flatten`. |
| 4 Dashboard | `dashboard*.py`, `assets/` | **Astra** | Static HTML, charcoal/gold. Agent avatars (owner's artwork). Offline rule-based chat (NOT an LLM). CSP, no network, no forms. |
| Orchestration | `controller.py` | — | One cycle or `--every N` loop. Runtime lock (one controller per dir). Never submits without `--submit`. |
| Generative agents | `llm_chat.py` | all four | Optional. Anthropic Messages API via stdlib HTTPS. Tools: read_snapshot/read_backtest/read_cycles/explain_rule only. Falls back to the rule router. Off without `ANTHROPIC_API_KEY`. |
| Local chat server | `studio.py` | — | `controller --serve`, loopback-only, fresh local records per question, token/Origin/Host guards. Exact Houston `reconcile` requests a dry diagnostic. No order routes. |
| Backtest | `backtest/` | — | Underlying-level walk-forward replay. **Audited** (see §4). Options P&L NOT modelled. |

Support: `calendar.py` (NYSE sessions/holidays), `gateway.py` (control modes), `notify.py` (JSONL + optional webhook), `executor/eastern.py` (ET dates).

## 2. Owner's environment and account facts

- **Alpaca paper account**: ACTIVE, options level 3, `multiplier=4` (margin — all paper accounts are), `pattern_day_trader=true`, $100k balance (owner did not create it at $2k; `capital_cap` bounds spendable cash to $2,000 regardless).
- `runtime/paper-margin-acknowledged` contains `ACKNOWLEDGED` (required for reconciliation to pass on a margin paper account).
- **Massive/Polygon subscription: Stocks Advanced.** Stock bars work (200). **Options snapshots return HTTP 403.** The scanner and the exit bid-pricing need **Options Advanced** ($199/mo). Owner said they'll upgrade later. Until then: backtests work, shadow scan fails with 3 snapshot errors, live trading is impossible.
- Reconciliation verified live: `reconciled: true` during RTH, only `MARKET_CLOSED` after hours.
- **Keys were pasted into chat by the owner (Alpaca key/secret and Massive key). They must be rotated.** Remind the owner. Keys live in `C:\Users\eveli\keys.ps1` (outside repo); load with `. C:\Users\eveli\keys.ps1`. Env vars: `ALPACA_PAPER_API_KEY`, `ALPACA_PAPER_API_SECRET`, `MASSIVE_API_KEY` (or `POLYGON_API_KEY`).
- `runtime/` is gitignored except nothing — it holds SQLite journals, bars caches (`bars-SPY/QQQ/IWM.json`), backtest reports (`bt-*.json`), `dashboard.html`. Do not commit it. Do not edit the SQLite files by hand.

## 3. Runtime files and switches

| File | Meaning |
|---|---|
| `runtime/trading-control` | exactly `ARMED_PAPER` / `EXITS_ONLY` / `DISABLED` (missing/garbled = DISABLED). Currently DISABLED. |
| `runtime/playbooks/<name>.approved` | exactly `APPROVED`. None exist yet. Controller also needs `--enable-playbook <name>`. |
| `runtime/paper-margin-acknowledged` | exactly `ACKNOWLEDGED`. Exists. |
| `--submit` | CLI flag; the only way orders are POSTed. |

**Windows gotcha:** PowerShell `echo X > file` writes UTF-16. Always create marker
files with `python -c "from pathlib import Path; Path('runtime/x').write_text('X', encoding='utf-8')"`.
This bit us once already.

## 4. Backtest audit results (commit 33b43de) — do not undo

Three defects were fixed and they all flattered results:
1. `outcome` (sign of R) is now independent of `exit_reason` (stop/target/timeout). Old code labelled every stop exit a loss and every target touch a win.
2. Fills already through the stop / at or beyond the target are `no_trade`, excluded from stats.
3. `MIN_RISK_ATR = 0.5` in `signals.py`: stops inside 0.5×ATR14 are refused, live and in replay. Cent-wide stops had produced +13R "wins" and a −38R loss.

Audited (Jan 2023–Sep 2026, 929 bars):

| | mean R | median R | PF | resolved | worst R |
|---|---|---|---|---|---|
| QQQ trend | **+0.25** | +0.19 | 1.53 | 37 | −1.8 |
| IWM trend | +1.01 | **−0.42** | 2.60 | 35 | −8.2 (outlier-driven) |
| SPY trend | −0.12 | −0.47 | 0.80 | 44 | −2.9 |
| bounce (all) | negative or tiny samples (2–9 trades): stop design is structurally flawed (swing low is usually today's low) |
| breakout | 1 trade per symbol: no conclusion |

Only QQQ trend is a candidate, and it is thin. Nothing is approved. The earlier
"+0.63R / $63 per $100" figure was wrong and has been retracted in docs.

## 5. Exit rules (executor/exits.py) — current behaviour

Priority: `underlying_stop` › `underlying_rule` (EMA20 close-through, trend only) › `premium_stop` (−50%) › `underlying_target` › `time_stop` (21 DTE or sessions held).

**Same-session (entry day):** only the premium stop is evaluated (underlying rules have no new close). It's allowed as protection within a PDT budget: `FillLedger.day_trades_since(window_start)` counts same-session round trips over 5 sessions; `PDT_DAY_TRADE_LIMIT = 3`; the 4th is refused and the position holds to the next session (loss bounded by premium). Never same-day target or time exits.

Exit pricing: fresh NBBO bid (single-contract Massive snapshot, ≤120s, two-sided) else 95% of broker mark.

## 6. Commands

```powershell
. C:\Users\eveli\keys.ps1
python -m alpaca_agents.executor account
python -m alpaca_agents.executor reconcile
python -m alpaca_agents.executor intents | release-intent <id> --note "..." | flatten <OCC> [--submit]
python -m alpaca_agents.scanner --session YYYY-MM-DD --symbols SPY QQQ IWM      # needs Options Advanced
python -m alpaca_agents.backtest --symbol QQQ --bars-file runtime\bars-QQQ.json --playbooks trend oversold_bounce breakout --output runtime\bt-QQQ.json
python -m alpaca_agents.controller --runtime runtime [--every 300] [--dashboard] [--enable-playbook X] [--submit]
python -m alpaca_agents.dashboard        # or double-click Open-Dashboard.cmd
python -m alpaca_agents.controller --serve # local records; no keys needed until a diagnostic cycle
```

Tests:
```sh
python -m unittest discover -s tests
node --test tests/test_dashboard_chat.cjs       # includes Python/browser router parity
python -m alpaca_agents.dashboard && node tests/dashboard_browser.cjs   # Chrome headless smoke test
node tests/dashboard_browser.cjs --served      # isolated synthetic runtime, real HTTP, no keys
```
CI: `.github/workflows/tests.yml` runs Python 3.11–3.13 + Node tests.

## 7. Invariants the next session must preserve

- Paper URL hardcoded; no env/config override to live. Live is a separate design conversation, not a flag.
- Only `executor/client.py` talks to the broker; only `submit.py` POSTs orders; it sends the **journal's stored body**, never a caller's dict.
- `rules.py` stays pure and deterministic. No LLM anywhere in the decision path.
- Fail-closed: missing/unknown → rejected/DISABLED. Unknown submission outcome → stays `claimed` for reconciliation, never retried.
- One attempt per order, ever.
- The dashboard is read-only for trading. Chat is Layer 4; it cannot place, cancel, approve, arm, or change limits. Served Houston's exact `reconcile` request invokes a dry diagnostic through the controller, never submits or reserves entries. Static CSP: `connect-src 'none'`; served CSP: `'self'`; both block forms and allow only the bundled script hash.
- Dashboard shows **Unknown**, never 0, when a journal is missing/unreadable.
- Backtest reports say `options_pnl_modelled: false`; never present R as dollars.
- Tests are the spec. Add tests for behaviour changes; the full-lifecycle controller test (`tests/test_controller.py`) runs against a stateful fake broker and has caught 4 real bugs.

## 8. Known gaps (honest list)

- Spreads: `trend_debit_spread` is scanned but multi-leg execution is not implemented.
- `OPASN`/`OPEXC` (assignment/exercise) block reconciliation; only worthless `OPEXP` is handled.
- Unfilled entry day-limit orders simply expire; no re-pricing.
- Entry can happen after an overnight gap through the stop/target (replay now calls this `no_trade`; live has no intraday underlying check).
- Bounce playbook stop design needs rework before it's worth retesting.
- Options-level backtest does not exist (needs point-in-time option quotes).
- Generative chat is optional and read-only; it narrates saved records and cannot see live quotes. Milestones 2–5 (charts, outlook, manual drafts, confirm) are not built.

## 9. Chat blueprint — milestone 1 complete; stop for review

**Executable spec with acceptance criteria and implementation clarifications: `docs/TASK-chat-server.md`.**

- [x] Milestone 1: localhost server + fresh-runtime chat. Implementation commit
  is recorded in `git log` (message: “Serve fresh local chat without adding an order path”).
- [ ] Milestones 2–4: charts, Houston outlook, manual dry-run drafts.
- [ ] Milestone 5: separately authorized human-confirmed paper submission.
- [x] Milestone 6: optional generative agents (`llm_chat.py`). Enabled only by
  `ANTHROPIC_API_KEY` in the `--serve` process; four read-only tools enforced in
  code; deterministic fallback; daily cap; fake transport in tests. Built before
  2–5 at the owner's request; charts/outlook/drafts still pending.

Start: `python -m alpaca_agents.controller --serve`; open its printed URL.
No background cycles without `--every`; no browser auto-open. The per-launch
session token is local-only, never saved by the app. Keep the terminal open.
`reconcile` needs rotated paper keys in that process's environment; ordinary
questions just read current local reports. Inspect timestamps, not a “live”
badge, for freshness. The worker serializes reads and cycles and holds the
runtime lock until shutdown finishes. Approval/control files are untouched.

Goal: ask Star "give me a rundown of today with charts" and ask Houston "what
are you looking for / place this trade" **from the chat**.

Prerequisite for both: a local backend (now built; static file mode remains).
`controller --serve`: HTTP server on `127.0.0.1`, random port, per-launch
secret token, **run inside the controller process** so one process owns the
broker client, runtime lock and journal. Nothing off-machine, no keys in browser.

Milestones, in order:

| # | Deliverable | Risk |
|---|---|---|
| 1 | `--serve`: localhost server + token; chat backed by live data instead of a baked snapshot | low |
| 2 | Star rundown: server-side SVG charts (price + EMA20/50 + entry/stop/target bands; option mark vs premium-stop line; cycle timeline) + deterministic template narrative. Intraday underlying bars work with Stocks Advanced. | low |
| 3 | Houston read-only: current proposals and why refused, pending intents, exit preview at current marks | low |
| 4 | Manual idea → same idea schema as scanner → `rules.evaluate` (Moon, unchanged) → `journal.reserve` → show the **journal's stored body** + one-time token (authorization_id, 60s TTL). **Dry-run only** at this milestone: body shown, then expired. | low |
| 5 | Confirm token → the existing `submit_claimed`. Gated by launch flag `--allow-chat-orders` (default off) AND `trading-control == ARMED_PAPER` AND reconciled ≤60s. Intent `kind="manual_chat"` with the user's original text audited. Extend the full-lifecycle tests. | **medium** — new path for orders to leave the machine |
| 6 | Optional LLM narrator/parser. Tools: read sanitized snapshot, draft idea. **Never** the submit route, never sees the token. | medium |

Non-negotiables for milestone 5: chat is an *idea source* joining at the front
of Layer 1→2→3, never a shortcut. No "just do it" without the token round trip.
"Execute what you're looking for" = Star's current proposal fed into step 4,
still confirmed by token. Chat can never change limits, control mode, approvals,
keys, release claimed intents, or place multi-leg.

Also queued (owner agreed, lower priority): readiness checklist panel (separate
checks: broker reconciled / options data access / controller heartbeat /
playbook approved+enabled / submission permitted) and a per-trade review journal
(idea → entry → exit reason → fills → costs → rule deviations).

## 10. Conventions

- Python stdlib only at runtime (`dependencies = []`). Pillow was used once to prepare avatars; Node/Chrome are test tools only.
- Money is `Decimal`, cent-quantized; indicator math is float.
- Timestamps UTC; trading days are ET (`eastern_date`). NYSE calendar in `calendar.py`.
- Write patches as files when they contain triple-quoted strings (bash heredocs collide).
- Commit messages: what changed and *why it was wrong before*. Push to `origin main`.
- Windows: LF/CRLF warnings from git are normal. Close SQLite connections explicitly in tests (temp-dir cleanup fails otherwise).
