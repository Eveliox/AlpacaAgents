# AlpacaAgents

Paper-only options swing-trading system, built safety-first.

**Status: milestones 1-3 complete, paper only.** All four layers exist:
Layer 1 scanner (shadow-only until a playbook is approved), Layer 2 pure rules
engine, Layer 3 executor (reconciliation, order-intent journal, single-attempt
paper transport, exit manager, controller cycle), and Layer 4 static dashboard
with notifications. Every playbook starts disabled. Nothing is submitted unless
the controller is run with `--submit` **and** the control file says
`ARMED_PAPER` **and** the playbook has an approval marker **and** the state is
fully reconciled. There is no live configuration path of any kind.

Broker and market-data transports have only been exercised against fake
responses in tests; nothing here has been run against real credentials yet.
See the runbook below for the first-run sequence.

## Run tests

Python 3.11+; no runtime dependencies.

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
```

Without installation, in Git Bash/Linux:

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
```

## Modules

- `src/alpaca_agents/rules.py`: pure deterministic `evaluate()` and immutable
  `RiskState`. No I/O, LLM, environment access, clock reads, or broker calls.
- `src/alpaca_agents/gateway.py`: reads the control file on every evaluation,
  snapshots the JSON idea, and durably appends decisions to JSONL before returning.
  Missing/unreadable control files disable trading. Audit failure raises instead
  of releasing a decision. Use this boundary rather than calling the pure engine
  directly from a future controller.
- `src/alpaca_agents/executor/`: exclusive broker boundary. `client.py` is the paper
  transport: GET account/positions/clock/orders/activities plus exactly one write,
  `POST /v2/orders` for a journal-prepared body. Durable SQLite request traces;
  manual connectivity CLI.
- `src/alpaca_agents/executor/ledger.py`: duplicate-safe closed-trade accounting,
  daily loss latches, and transactional close/breaker events.
- `tests/test_ledger.py`: restart, replay, concurrency, rollback, and accounting tests.
- `src/alpaca_agents/executor/fills.py`: normalized long-option FIFO inventory and
  realized P&L on every closing execution, including partial closes.
- `tests/test_fills.py`: partial realizations, FIFO, rounding, ordering, and atomicity.
- `src/alpaca_agents/executor/history.py`: bounded, resumable-evidence staging of
  raw broker account activities with per-page Request IDs.
- `tests/test_history.py`: pagination, failure preservation, account scoping, and
  malformed-page rollback tests with fake responses.
- `src/alpaca_agents/scanner/`: Layer 1. `indicators.py` (EMA/SMA/RSI/momentum/
  pivots), `signals.py` (playbook signal logic), `contracts.py` (DTE/delta/liquidity
  structure selection), `scan.py` (filters, portfolio rules, Trade Idea assembly).
- `src/alpaca_agents/marketdata/`: isolated Polygon/Massive GET client, durable
  diagnostics, and provider-to-`SymbolSnapshot` normalization.
- `src/alpaca_agents/scanner/__main__.py`: manual ETF shadow scan and JSON report;
  there is no option to enable playbooks or place orders.
- `src/alpaca_agents/executor/normalize.py`: Alpaca activities -> `FillLedger`
  (FILL/FEE booked, cash movements ignored, everything else blocks).
- `src/alpaca_agents/executor/reconcile.py`: pure `reconcile()` cross-checks
  account, positions, open orders, clock and ledger; `build_risk_state()` wraps
  the read-only I/O. `eastern.py` gives tzdata-free ET session dates.
- `src/alpaca_agents/executor/orders.py`: transactional order-intent journal:
  entry reserve/claim, exit preparation, stored bodies, submission bookkeeping
  and broker-driven resolution. No HTTP.
- `src/alpaca_agents/executor/submit.py`: the only caller of the one write
  endpoint. Sends the journal's stored body once; maps 2xx / broker rejection /
  unknown outcome to journal transitions.
- `src/alpaca_agents/executor/exits.py`: pure exit rules for held long options
  (underlying stop, premium stop, target, time stops).
- `src/alpaca_agents/controller.py`: one cycle = reconcile -> resolve -> exits ->
  entries -> `runtime/cycles.jsonl`. Injected providers; `--submit` gate;
  playbook approval markers.
- `src/alpaca_agents/calendar.py`: pure NYSE session calendar (holidays, previous
  session, sessions between). Used by the reconciler (`NOT_A_SESSION`), exit
  time stops, and the controller's default session.
- `src/alpaca_agents/notify.py`: JSONL notifications plus optional https webhook.
- `src/alpaca_agents/dashboard.py`: static HTML dashboard from runtime files.
- `tests/test_controller.py`: full offline lifecycle against a stateful fake
  broker (entry -> fill -> hold -> premium stop -> exit -> loss -> breaker).
- `tests/test_submit.py`, `tests/test_exits.py`, `tests/test_notify.py`,
  `tests/test_dashboard.py`.
- `tests/test_orders.py`: concurrent reserve/claim, cash/slot/rate/expiry limits,
  kill-switch rechecks, replay rejection, restart binding, atomic-audit rollback.
- `tests/test_reconcile.py`: every failure mode -> unreconciled with a reason ->
  rules engine rejects; end-to-end read-only build with a scripted transport.
- `src/alpaca_agents/backtest/`: walk-forward replay of `signals.py` over daily
  bars (`replay.py`), multi-year history stitching / JSON cache (`history.py`),
  and a report CLI. Underlying-level R only; options are not modelled.
- `tests/test_backtest.py`: no-look-ahead, one-trade-at-a-time, close-based stop,
  end-of-data, summary math, chunk continuity, and CLI tests.
- `tests/test_marketdata.py`: fake data-to-shadow integration, freshness, malformed
  data, pagination security, and credential/audit isolation tests.
- `tests/test_scanner.py`: every playbook's output is fed through the real
  `rules.evaluate()` to prove schema compatibility.
- `tests/test_executor.py`: fake-transport tests; no network or real credentials.
- `examples/long_call.json`: synthetic input, not a current quote or recommendation.
- `tests/test_rules.py`: rejection, boundary, spread, state, kill-switch, and audit tests.

The idea contract extends the handover with explicit `action`, `holding_style`,
`strategy`, `quantity`, `legs`, `limit_debit`, and `estimated_fees`. An underspecified
idea is rejected, not guessed into an order. Numeric strings are supported for
money; JSON NaN/Infinity and boolean numeric fields are rejected.

Only standard 100-share long calls/puts and same-underlying, same-expiry 1:1
vertical debit spreads are accepted. Legs are ordered buy then sell. One spread
unit contains two contracts, one on each leg. Adjusted contracts, calendars,
credit spreads, naked shorts, and zero-day expirations are not supported.

Risk is **limit debit × 100 × quantity + estimated fees**, capped at $100.
`est_contract_cost` must agree with premium, excluding fees. Future executor must
verify fees and contract metadata using trusted data; scanner assertions are not
sufficient to place orders. Stops/targets refer to underlying prices. Reward:risk
is recomputed from them and is not a promise of an option's realized return.

Risk state must be reconciled and at most 60 seconds old. Open + pending entries
are capped at two. Settled cash must cover risk. Five attempts in the rolling
interval `(now - 1 hour, now]` block another attempt. Pending cash reservations
must already be subtracted from `settled_cash`.

Daily realized loss uses cumulative losing-trade amounts, conservatively **not
netted against winning trades**. $40 or a persisted breaker latch blocks entries.
The trusted controller supplies the exchange trading day; UTC midnight must not
reset the breaker. The one-unit cap remains indefinitely until manual review.

## Kill switch

`runtime/trading-control` is read on every decision. Exact trimmed contents:

| contents      | entries | exits | notes                                   |
|---------------|---------|-------|-----------------------------------------|
| `ARMED_PAPER` | yes     | yes   | the only mode that allows new entries   |
| `EXITS_ONLY`  | no      | yes   | wind-down: stops/targets still honoured |
| anything else | no      | no    | missing, unreadable, typo => DISABLED   |

```sh
mkdir -p runtime
printf 'DISABLED\n' > runtime/trading-control     # stop everything
printf 'EXITS_ONLY\n' > runtime/trading-control   # no new risk, still manage positions
printf 'ARMED_PAPER\n' > runtime/trading-control  # full paper operation
```

The journal re-reads the file before and after every state-provider call, so a
change made while a decision is in flight still wins.

Keep this path outside scanner write permissions. In production use atomic file
replacement. A kill switch cannot undo fills or automatically cancel existing
orders; cancellation needs a separately validated, audited executor workflow.

## Paper connectivity and Request IDs

After installing, inject `ALPACA_PAPER_API_KEY` and `ALPACA_PAPER_API_SECRET`
**only into the executor process environment**, using your secret manager or a
secure shell prompt. Do not paste secrets into commands, source files, or chat.
Generic/live credential environment names are deliberately ignored; there is no
URL override. `.env` files are not loaded automatically.

Manually run a read-only request (this contacts Alpaca when credentials are set):

```sh
python -m alpaca_agents.executor account
```

Inspect recent support traces without credentials or network access:

```sh
python -m alpaca_agents.executor traces
```

Default journal: `runtime/api-requests.sqlite3`; override with `--trace-db PATH`.
It records UTC start/end timestamps, local correlation ID, method, endpoint path,
HTTP status, `X-Request-ID`, and outcome. Include the broker Request ID in support
requests; it cannot be fetched later from another endpoint. Header lookup is
case-insensitive. Missing, malformed, or unavailable IDs are stored as null.
IDs are captured before reading bodies, including HTTP errors (403/429/5xx).

No request/response bodies, credential headers, or raw exception messages are
journaled. No retries or redirects are followed. Environment proxy settings are
ignored to avoid unexpectedly forwarding credentials. TLS verification remains
on. A journal start is committed before network access; failure prevents access.
Completion-persistence failure prevents a successful return. A `started` record
without completion means an unresolved attempt, not confirmed broker failure.

The journal currently retains all records; `traces` shows the latest 20. Restrict
filesystem access and back up this file. A future retention policy must preserve
unresolved attempts and incident records. Trade/decision/client-order correlation
are the journal's `paper-<authorization_id>` values; the executor CLI itself never submits.

`account()` and `positions()` return broker data only; they do not produce a
reconciled `RiskState`. In particular, cash and options buying power do not prove
settled cash or cash-account eligibility. The kill switch blocks trade validation,
not these read-only support/reconciliation requests.

## Persistent closed-trade ledger (offline foundation)

`TradeLedger(Path("runtime/trades.sqlite3"))` accepts normalized `ClosedTrade`
records from a **future trusted reconciler**, not scanner ideas. It computes:

```
realized P&L = total exit credit - total entry debit - actual entry/exit fees
```

Amounts are `Decimal` dollars per entire position lifecycle, not per-share option
prices. Each position lifecycle and close must have a stable unique ID. Equivalent
replays return `False` without changing totals; conflicting duplicates raise
`LedgerError`. Corrections/busts are not silently overwritten and need a future
explicit reconciliation workflow. Unknown fees must not be guessed as zero.

A single SQLite transaction writes the close, its event, and (when cumulative
losing-trade amounts reach $40) the day's breaker latch and trigger event. Audit
failure rolls back all of them. Concurrent writers are serialized. Profits never
offset the loss counter or clear a latch. Restarting preserves latches. Days have
separate records; querying a new day never erases the previous day's breaker.
Late records affect their supplied exchange trading day, not their ingestion day.
The ledger does not determine or verify exchange session dates itself.

`daily_summary(day)` returns net realized P&L, cumulative realized loss, latch
status, and closed-trade count. `events()` exposes ordered close/trigger history.
These are ledger summaries, **not evidence of completed broker reconciliation**;
an empty ledger means no imported records, not proof that the account has no losses.

**Not connected to trading authorization yet.** `TradeLedger` accounts only for
fully closed positions. Keep it as a standalone lifecycle-reporting prototype;
use the fill-level foundation below for future incremental accounting. **Never
sum the two ledgers' P&L or loss counters:** that would double-count realizations.
No automatic migration or connection between these stores exists.

## Fill-level accounting (offline, long options only)

`FillLedger(Path("runtime/fills.sqlite3"))` accepts normalized `OptionFill` records.
It is not an Alpaca activity importer. Each record requires a stable execution ID,
unique position-lifecycle ID, standard OCC contract, side (`buy_to_open` or
`sell_to_close`), integral quantity, total execution premium, actual fees,
timezone-aware execution timestamp, and trusted exchange session date.

- Buys create FIFO lots with premium plus entry fees as their remaining basis.
- Sells consume the oldest lots, allocate entry fees, and subtract actual exit
  fees. P&L is booked on **each sell execution's session date**, even when contracts
  remain open. FIFO is an internal accounting policy, not a verified match to
  Alpaca's tax-lot or displayed cost-basis methodology.
- Losing sell executions accumulate toward the $40 breaker; winning executions
  never offset them. This is deliberately more conservative than netting a whole
  lifecycle's profits and losses. The latch and its one-time event persist.
- Integer microdollars avoid float errors. Partial basis allocation rounds upward
  by less than one microdollar; the remainder stays on the lot so full-close totals
  conserve every fee. Inputs permit at most six fractional decimal places.
- Fill, inventory changes, realized P&L, latch, and audit events commit together.
  Identical replays are no-ops; conflicting IDs, unmatched closes, oversells,
  contract mismatches, and reuse of a closed lifecycle raise `LedgerError`.
- Events must be strictly increasing in time within each lifecycle. Out-of-order
  or equal-timestamp distinct executions are rejected rather than guessed into
  an order. A future importer needs verified ordering and controlled rebuilding
  for late history/corrections, preserving already-triggered live-day latches.

`inventory()` reports **remaining lots**, not a reconciled broker position count.
`daily_summary(day)` reports P&L/loss/latch and always includes `reconciled=False`.
`events(limit=100)` returns the latest accounting/trigger events, newest first;
premium and fee fields in these events are integer microdollars.

The ledger can account for historical multi-contract and same-day fills. This is
not trading permission: historical violations must not be silently omitted from
accounting, and the rules engine's one-unit/swing-entry restrictions still apply.

**Still blocked:** broker history pagination/import, complete opening history,
execution-to-order/lifecycle mapping, trusted contract metadata and calendar,
fees completeness, spread-leg grouping, shorts, assignment/exercise/expiry,
corrections, account scoping checks, and settled-cash reconciliation. Use one
ledger database per account; do not mix accounts. Shape-valid OCC symbols alone
do not verify contract eligibility or multiplier. No `RiskState` is generated,
no scanner accounting import is exposed, and no orders can be submitted.

## Reconciliation: broker + ledger + journal -> RiskState

```sh
python -m alpaca_agents.executor reconcile            # exit 0 reconciled, 2 not
```

`build_risk_state()` imports activities from the last contiguously-covered
instant (or account creation on first run), normalizes them into `FillLedger`,
fetches account / positions / open orders / clock, looks up the broker record
for every claimed journal intent, and calls the pure `reconcile()`. The result
is a `RiskState` plus a list of **reasons**; `reconciled=True` only when the
list is empty, and the rules engine rejects any unreconciled state before it
looks at an idea.

The four verifications that were hard-blocked in the previous revision are now
real checks. There is still no override flag for any of them.

- **Order provenance.** Every broker open order must be a single-leg option
  order whose `client_order_id` matches a *claimed* journal intent
  (`UNKNOWN_OPEN_ORDER` otherwise). Every claimed intent must have a broker
  record: none => `CLAIMED_INTENT_WITHOUT_BROKER_RECORD` (an incident - the
  body may have been sent and lost; the slot stays held until a human
  resolves it); open but not in the open list => `OPEN_ORDER_NOT_LISTED`;
  terminal at the broker but still claimed => `INTENT_UNRESOLVED` (run
  `journal.resolve()` with the broker status, then re-reconcile). Baseline
  `pending_entries` = matched open buy orders; the journal adds its own
  reserved intents at reserve/claim time.
- **History coverage.** `ActivityStore.covered_through(created_at)` walks the
  exhausted import runs and returns the latest instant reachable by a chain of
  *overlapping* windows starting at or before `account.created_at`
  (Alpaca's `after`/`until` are exclusive, so touching windows are a gap).
  Coverage must reach within 15 minutes of now: `HISTORY_COVERAGE_GAP`,
  `HISTORY_STALE`, `HISTORY_FROM_FUTURE`, `HISTORY_COVERAGE_UNANCHORED`.
  Fees billed later simply arrive in a later window and count then.
- **Settlement.** `settled_cash` = min(`cash`, `non_marginable_buying_power`,
  `options_buying_power`) minus a **ledger-derived unsettled bound**: gross
  sell proceeds booked on sessions within the last 4 calendar days
  (options settle T+1; 4 days covers weekends and a holiday, deliberately
  over-conservative). Floors at 0. This is computed from verified history,
  not trusted from the broker.
- **Cash account.** `multiplier == "1"`, no override. A margin paper account
  fails here until you obtain a cash-multiplier paper account.

The remaining checks, each its own reason: broker clock within 60s and
`is_open == True` (`MARKET_CLOSED`); trading day = **current Eastern date**,
never `next_open`, so the loss latch cannot be reset early; account `ACTIVE`,
not blocked/suspended, not PDT, `options_trading_level >= 2`; every broker
position a standard long option with `{contract: qty}` equal to the ledger's
lots; normalization blocked nothing; ledger day matches. Realized loss and
the breaker latch flow straight into `RiskState`.

Normalization (`normalize.py`): `FILL` activities are sorted by
`(transaction_time, id)` (equal timestamps from partial fills are fine), booked
as `buy_to_open` (new lifecycle or add to the open one) or `sell_to_close` on
the open lifecycle; a sell with no inventory blocks and **halts further fills**
so lifecycles are never misattributed. Session date is the ET date of the
execution. Per-execution fees are booked as 0 because Alpaca bills options
regulatory fees as separate `FEE` activities; those are booked on their activity
date and count **fully toward the daily loss counter** (conservative, cents).
`CSD/CSW/JNLC/INT/DIV` are ignored. `OPEXP` on a contract the ledger holds
long, dated on that contract's expiration, with zero value and exactly the held
quantity, is booked as a $0 `sell_to_close` (the whole basis becomes realized
loss and counts toward the breaker); any deviation blocks. `OPASN`, `OPEXC`,
equity fills, fee credits and unknown types block reconciliation until handled
explicitly.

A reconciled state is a 60-second snapshot and **authorizes nothing by itself**;
it is the baseline the order journal validates against.

## Order-intent journal and submission

`OrderJournal(path, account_id=..., control_file=...)` is the executor-owned
bridge between a decision and a paper order. It never talks HTTP itself.

```
reserve(decision_key, idea, state_provider, now, trading_day)
    -> {"approved", "reason", "idea", ["authorization_id", "expires_at"]}
claim(authorization_id, state_provider, now, trading_day)
    -> {"approved", "reason", "idea", ["authorization_id", "prepared_order", "submission_enabled": False]}
prepare_exit(decision_key, exit_decision, inventory, now, trading_day)
    -> {"approved", "reason", "idea", ["authorization_id", "prepared_order"]}
```

**Entries.** `state_provider` is supplied by the trusted controller and returns
the baseline `RiskState`. Because the provider runs *inside* the journal's
`BEGIN IMMEDIATE` transaction, it must not reopen the journal; providers that
accept `(live_intents, now)` receive the locked view and the decision's
timestamp. The journal adds unsent local intents to `pending_entries` (sent
ones are already in the broker's open-order list), subtracts reserved cost for
**all** live entries from `settled_cash` (conservative double-hold), appends
prior authorized attempts, and re-runs the full `rules.evaluate()`. Single-use
`decision_key`s, one live intent per underlying, `long_call`/`long_put` only,
30s reservation expiry, `claim` reloads the stored idea and revalidates, and a
`claimed` intent **never auto-expires**.

**Exits.** `prepare_exit` validates a deterministic decision from `exits.py`
against ledger inventory (`NOT_HELD`, one live exit per contract, cent-priced
positive limit, known `exit_reason`), requires `ARMED_PAPER` or `EXITS_ONLY`,
and journals it straight to `claimed`. A sell never adds exposure, so no
`RiskState` is consulted.

**Stored bodies.** At claim/prepare time the exact body is stored on the
intent. `submit.py` sends **the stored body**, and refuses if the caller's copy
differs (`BODY_MISMATCH`). A tampered claim result cannot change quantity,
price or contract.

**Submission (`submit_claimed(client, journal, result, now, submit=False)`).**
Only `submit=True` from the controller sends anything. One attempt, ever:

| broker outcome                              | journal transition                 | slot     |
|---------------------------------------------|------------------------------------|----------|
| 2xx echoing our `client_order_id`           | `mark_submitted` (stays `claimed`) | held     |
| 400/403/422 **with** an `X-Request-ID`      | `resolve_unplaced`                 | released |
| timeout, 5xx, no request id, echo mismatch  | `note_submit_outcome_unknown`      | held     |

An unknown outcome stays `claimed` until reconciliation finds the order by
`client_order_id`; if the broker has no record it is an incident
(`CLAIMED_INTENT_WITHOUT_BROKER_RECORD`) that halts every cycle until a human
resolves it. `resolve()` is the only other exit from `claimed` and accepts only
a broker-observed terminal status.

## Exit manager

`exits.evaluate_exit(HeldOption, idea, trading_day)` is pure. Priority:

1. `underlying_stop` - last completed session close through the idea's stop
2. `underlying_rule` - the playbook's discretionary rule when bars are supplied:
   trend ideas exit on a close through EMA20 against the position. The bounce
   and breakout rules are already the idea's stop (swing low / range high).
3. `premium_stop` - option mark x 100 <= basis x (1 - `premium_stop_pct`/100)
4. `underlying_target` - close through the idea's target
5. `time_stop` - DTE <= `time_stop_dte`, or NYSE sessions held >= `time_stop_sessions`

The exit is a **day limit sell at the fresh NBBO bid** (marketable by
definition in Alpaca paper), or at 95% of the broker mark when no fresh
two-sided bid is available (floored at $0.01).

**Same-session exits are protective only.** On the session a position was
opened, the underlying rules have no new information (their close is the
signal session's), so only the premium stop is evaluated that day: a -50%
collapse on day one is sold, never a target or time exit. Each same-session
exit is a day trade; a margin account under $25k gets 3 per rolling 5
sessions before PDT restrictions, so the controller counts them from the fill
ledger (`day_trades_since`) and the rule refuses the 4th, holding to the next
session with the loss bounded by the premium. Basis includes entry fees, so the
premium stop is slightly conservative. Session counting uses the NYSE calendar. Missing close => underlying rules are
skipped; missing mark => a fired rule is reported for manual attention but no
order is priced.

## Named agents and dashboard filters

The four architectural roles have display names (not separate trading accounts or playbooks):

| Name | Role | Avatar | Dashboard view |
|---|---|---|---|
| Houston | Executor | Wallet | Positions, trades, intents, journal events, broker requests |
| Star | Scanner | Spark/bomb | Playbook approvals, scan errors, shadow ideas and historical research |
| Moon | Rules Engine | Ghost | System-wide risk limits and latest reconciliation blockers |
| Astra | Dashboard / Notifications | Vinyl record | Cycle reports and notifications |

The supplied originals are preserved in `artwork/originals/`. Optimized avatars
are packaged and embedded in the generated HTML (no remote image services).

**Easiest on Windows:** double-click `Open-Dashboard.cmd` in the project folder.
It rebuilds the dashboard from local records and opens it. It does not need API
keys, start a controller, fetch data or submit orders. Python and the installed
package must already be available.

Or generate and open the dashboard in PowerShell:

```powershell
python -m alpaca_agents.dashboard
Start-Process runtime\dashboard.html
```

The dashboard uses a compact dark layout with sidebar links to **Overview,
Positions, Research, Scanner, Risk & limits, Activity, and Chat**. Account
status comes first; agent cards are shortcuts rather than the main content.
Research plots compare recorded mean and median on a shared zero-centered R
axis, alongside profit factor, worst R and sample counts. They are not equity
curves or option P&L. Missing values have no plotted mark. All assets remain
bundled; no remote fonts, chart libraries or additional network calls.

Choose **All agents / Houston / Star / Moon / Astra** to filter panels. Global
status stays visible in every view. The filters work offline without JavaScript;
use Tab and arrow keys to navigate them with a keyboard. Filtering does not arm
trading, approve playbooks, or allocate separate budgets. This remains a static
snapshot: regenerate it for updated data, then refresh the browser.

### Talk to your crew

#### Live local records (milestone 1)

```powershell
python -m alpaca_agents.controller --runtime runtime --serve
```

Open the `http://127.0.0.1:<port>/` URL printed in the terminal. Keep that
terminal open; Ctrl+C shuts down the server and releases the controller lock.
No keys are needed to open the workspace or ask about recorded data. **This
does not arm trading, approve playbooks, start cycles, or submit orders.**

- Chat rereads local journals/reports on each question. Refresh the page for
  updated dashboard panels. “Live local records” does **not** mean live quotes
  or a fresh broker check; the last-cycle timestamp can still be old/unknown.
- Ask Houston exactly **reconcile** to request one dry diagnostic controller
  cycle. Load rotated paper keys in the launching shell first for that check.
  Missing credentials produce an explicit unavailable/unknown answer. No chat
  diagnostic can submit orders, reserve entries or prepare exits. Diagnostics
  may import activities, resolve terminal intents and write cycle reports.
- `--every 300` adds scheduled controller cycles using the existing gates and
  halt conditions. A closed market or incident stops the schedule, not the web
  server; it never restarts itself. `--submit` is still an explicit CLI option
  for **scheduled cycles only**, and requires `--every` in served mode.
- One runtime lock and one bounded worker serialize all cycles and reads.
  There is no second broker client in the web server. No order/confirm routes
  exist. This is still rule-based chat; charts and trade drafts come later.
- Loopback binding only; per-launch session token; exact Host/Origin checks;
  no CORS, no caching, no framing. The initial same-origin page bootstraps the
  token; all APIs require it. Do not share the printed token or served HTML.
  Broker/data keys never belong in the browser. The snapshot API is an
  allowlisted display projection, **not** a raw journal dump.
- Chat messages are not logged or persisted. Requests have size/rate limits.
  Connection failures show Unknown; no silent offline fallback or automatic
  diagnostic retry. Other local processes are outside this security boundary.

#### Generative agents (optional, milestone 6)

Set an Anthropic key in the shell that launches `--serve` and the four agents
answer in natural language with their own personalities:

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."      # keep it in keys.ps1, never in the repo
python -m alpaca_agents.controller --runtime runtime --serve
```

The terminal prints `Generative chat: ON (model, daily cap)`. A fifth persona,
**Nova**, appears only in this mode and is the default: a general assistant that
answers broad questions from its own knowledge (options concepts, mechanics,
how this system works) and uses the same tools for today's market, news and
records. Same boundary as the crew: no trading powers exist for it. Ask things like
*"What happened today?"*, *"Why didn't we trade?"*, *"Compare my QQQ and SPY
backtests and tell me which one you'd trust less"*, *"Which position is closest
to its premium stop?"*, and follow-ups — each agent keeps the last 10 turns in
process memory.

What the model can do is decided by code, not by the prompt. It has exactly
six **read-only tools**: `read_snapshot` (the same sanitized projection the
page shows), `read_backtest(symbol)`, `read_cycles(limit)`, `explain_rule(topic)`,
and — when `MASSIVE_API_KEY` is also loaded — `read_market(symbols)` (today's
stock snapshot: last, OHLCV, previous close, change, data timestamp) and
`read_news(symbol?, limit)` (headlines with publisher, time, URL). So *"What
happened in the market today?"* gets real index moves and attributed
headlines from the licensed feed, then what the system did. Headlines are
third-party text: the model is told to attribute them, never verify them, and
the page renders their URLs as plain links you choose to click. Market
narration is never an input to any trading decision.
There is no tool for orders, journal, controls, approvals, keys, shell or file
writes; an attempt to call one returns an error to the model. Tool results and
replies pass through the credential redactor; anything that looks like a key in
*your* message is blocked before it leaves the machine. Houston's exact
`reconcile` command stays deterministic and never goes to the model.

- **Data leaves the machine.** Your questions plus sanitized local records go to
  Anthropic's API (host pinned, stdlib HTTPS, no SDK). Nothing else does.
- **Cost bound.** `ALPACA_AGENTS_LLM_DAILY_CAP` (default 100 calls/day, tool
  rounds count) and 1024 output tokens per reply. Past the cap, and on any
  provider error, the rule-based answer is returned with a source note saying so.
- **Model.** `ALPACA_AGENTS_LLM_MODEL` (default `claude-sonnet-4-5`).
- **Clear chat** also erases the server-side memory (`POST /api/clear`).
- Replies are text about *saved records*; the model is told to say "I don't have
  that record" rather than guess, and gives no trade recommendations. Verify
  anything that matters against the panels and the broker UI.
- Tests use a scripted fake transport and a fake data client; CI never calls
  either API. Hostile-prompt tests assert no tool outside the six exists and
  no runtime file changes.
- The chat panel renders bold, lists, headings and https links (DOM nodes,
  never HTML injection), shows who is "thinking", stamps messages, has an
  **Expand chat** toggle, and a model-calls-today meter.

#### Offline snapshot (existing launcher)

Click **Talk to Houston / Star / Moon / Astra** on an agent shortcut, or use the
agent dropdown in the chat panel. **Open chat** at the top jumps directly to
the composer, including on a phone. Filters also switch the conversation persona.

Try:
- Houston: **Show my positions**, **Why aren't we trading?**
- Star: **Compare my backtests**, **Explain QQQ results**, **Why are scans failing?**
- Moon: **Explain my risk limits**, **Can I trust these results?**
- Astra: **Give me a briefing**, **What should I do next?**

**This is a local, rule-based snapshot guide, not generative AI.** Each answer
selects a supported topic and cites saved runtime sources or system guidance.
These are display personas in Layer 4, not conversational access to the actual
executor or rules engine. Unsupported questions return a capabilities message;
the guide does not invent market facts or forecasts.

- No API keys or subscription are needed for chat. It makes **no network calls**.
- It cannot place/cancel orders, approve playbooks, change limits or run shell commands.
- Messages stay in browser memory, with a separate thread per agent (latest
  20 exchanges each). **Clear chat** clears all threads; refreshing starts over.
- Suspected credentials/long tokens are masked, but this is only a precaution:
  **never paste secrets** into chat.
- Replies are snapshot-based, not live. The chat snapshot-age label advances;
  it does not fetch new records. Rebuild the dashboard to update the sources.
- JavaScript enables chat and card interactions. With JS disabled, chat is
  inert and the read-only dashboard and CSS filters still work.
- A Content Security Policy permits only the bundled script, blocks network
  connections and form navigation, and prevents remote code/assets from loading.

An open-ended LLM-backed conversation is **not connected**. If added later,
keep provider credentials on a local backend, limit it to sanitized read-only
snapshots, and never give it broker tools or a rules-engine override.

### Mission Control: recommended routine

1. **Overview first.** Read “What to do next,” the control mode, snapshot age,
   daily cumulative loss and outstanding intents. Missing/unreadable journals
   show **Unknown**, not zero. A recent snapshot is not proof a controller is
   running. Cycle freshness is evaluated at render time; the chat's clock only
   shows how old the rendered page is, not whether new cycles have occurred.
2. **Star for research.** Scan errors distinguish missing access (e.g. options
   HTTP 403) from no setups. `runtime/bt-*.json` reports appear in Research lab
   automatically. These are underlying-price R results, **not option P&L**.
   A sample-count threshold is not statistical significance or proof of an edge.
3. **Houston + Moon before trading.** Review positions, risk checks and any
   reconciliation blockers. A `DISABLED` system does not manage exits either.
   The dashboard never modifies controls or approves playbooks.
4. **Astra after a session.** Review notifications and cycle history. Expand
   technical order/journal/request details only when investigating a problem.

Standalone `executor reconcile` output does not populate the dashboard's cycle
history. With the necessary credentials loaded, one controller cycle **without
`--submit`** records a dry-cycle snapshot:

```powershell
python -m alpaca_agents.controller --runtime runtime --dashboard
```

A disabled control mode skips scanning and exits. A shadow scan is a separate
command and requires options-data access. Refreshing the browser alone never
fetches broker data. No web server or additional UI dependencies are required.

Developer checks for the interactive layer:

```sh
python -m unittest discover -s tests
node --test tests/test_dashboard_chat.cjs
# Optional: Node 22 and Chrome/Chromium; set CHROME_PATH if needed.
python -m alpaca_agents.dashboard
node tests/dashboard_browser.cjs
# Real HTTP + browser path, isolated synthetic runtime; no credentials:
node tests/dashboard_browser.cjs --served
# Generative UI with a scripted fake model (no API key / provider requests):
node tests/dashboard_browser.cjs --generative
```

The browser smoke test uses an isolated temporary profile and checks avatars,
filters, keyboard submission, chat safety, in-memory threads, mobile layout,
no external requests and the no-JavaScript fallback. It writes screenshots
beside the generated HTML; Node and Chrome are test tools, not app dependencies.

## Alpaca paper environment: what is and is not simulated

From Alpaca's paper-trading documentation, and how each fact is handled:

| Paper behaviour | Consequence here |
|---|---|
| Paper accounts are **margin** accounts (`multiplier` 2 or 4); a cash multiplier is not offered | Reconciliation blocks `NOT_CASH_ACCOUNT` until the owner creates `runtime/paper-margin-acknowledged` containing exactly `ACKNOWLEDGED`. With it, cash semantics are enforced **locally**: spendable cash = broker `cash` (never buying power) minus unsettled sale proceeds minus reservations; long-only; same-session exits only as protection, within the PDT budget. Unknown multipliers still block. |
| Default balance is $100k (any amount on reset) | `capital_cap` ($2,000): spendable cash never exceeds cap minus open basis, whatever the broker shows. Create the paper account at $2,000 anyway so the dashboard matches. |
| Fills only when **marketable** against NBBO; a sell limit fills only when limit <= best bid | Entries are priced at the ask by the scanner. Exits are priced **at the fresh NBBO bid** (single-contract Massive snapshot, realtime, two-sided, <= 120s old); if no such bid, 95% of the broker mark as a fallback. |
| 10% of eligible fills are random partials | Every order is qty 1, so partials cannot occur; the ledger handles them anyway. |
| Regulatory fees are **not** simulated | `FEE` activities never arrive in paper. `estimated_fees` are still reserved before entry (conservative); realized loss and the $40 breaker are premium-only in paper and will be slightly worse live. |
| Dividends not simulated; borrow fees n/a | Irrelevant: long options only. |
| Pre/after-hours available | Orders are `day` and only placed when the broker clock says `is_open`; options trade RTH only. |
| Network problems, disconnects | One attempt per order, ever. Unknown outcomes stay `claimed` until matched by `client_order_id`; see `release-intent`. |
| "Paper is only an approximation" | Slippage, queue position and market impact are absent. Treat paper results as an upper bound. |

A margin paper account under $25k is subject to PDT rules, which is one more
reason the exit manager refuses to close a position on the session it was
opened.

## Controller cycle and runbook

```sh
python -m alpaca_agents.controller                       # one dry-run cycle: reconcile, evaluate, reserve, expire
python -m alpaca_agents.controller --enable-playbook trend_directional --submit
python -m alpaca_agents.controller --every 900 --dashboard --enable-playbook trend_directional --submit
python -m alpaca_agents.dashboard                        # -> runtime/dashboard.html
```

One cycle:

1. **Reconcile** (`build_risk_state`). Any claimed intent whose broker order is
   terminal is `resolve()`d and the cycle reconciles again. Unreconciled =>
   the cycle halts; nothing is evaluated (position truth is unknown).
   Incident-class reasons notify at `incident` level.
2. **Exits.** For every ledger lot with a journaled entry idea: fetch the
   broker mark and the last completed session close, run `evaluate_exit`,
   `prepare_exit`, `submit_claimed`. Under `EXITS_ONLY` this still runs.
3. **Entries.** Only under `ARMED_PAPER`. The controller re-applies the
   playbook gate and refuses any idea whose underlying is already held or
   pending, regardless of what the scanner filtered (Layer 1 is untrusted).
   Best idea by score -> `reserve` -> `claim` -> `submit_claimed`. Max one
   entry per cycle.
4. **Report** appended to `runtime/cycles.jsonl`; notifications to
   `runtime/notifications.jsonl` (and `ALPACA_AGENT_WEBHOOK_URL` if set).

**Dry run** (no `--submit`) reserves and lets the reservation expire; it never
claims, because a claimed-but-unsent body would be an incident next cycle.
Exit decisions are reported but not journaled.

**Enabling a playbook** requires `runtime/playbooks/<name>.approved` containing
exactly `APPROVED`. Create it only after reviewing that playbook's backtest
(`python -m alpaca_agents.backtest`) and shadow-scan record. Delete the file to
disable. `--enable-playbook` without the marker is refused and logged.

**First-run sequence** (none of this has been done yet):

1. Create a **$2,000** paper account in the Alpaca dashboard and generate keys
   for it. `python -m alpaca_agents.executor account` - confirm
   `options_trading_level >= 2`, status `ACTIVE`, note the `multiplier`.
   It will be 2 or 4: read the paper-environment section above, then
   `printf 'ACKNOWLEDGED
' > runtime/paper-margin-acknowledged`.
2. `python -m alpaca_agents.executor reconcile` - first run imports activities
   from account creation; expect `MARKET_CLOSED` outside RTH and nothing else.
3. `python -m alpaca_agents.scanner --session <last session>` - shadow scan;
   review `runtime/shadow-scan.json`.
4. `python -m alpaca_agents.controller` during RTH with `ARMED_PAPER` and no
   playbook - proves the cycle reconciles live without submitting.
5. Backtest, review, approve one playbook, then `--submit`. Watch the
   dashboard and `cycles.jsonl` for the first entry, its fill, and its exit.

**Human-in-the-loop commands** (`python -m alpaca_agents.executor ...`):

| command | what it does |
|---|---|
| `intents` | list reserved/claimed intents |
| `release-intent <auth_id> --note "..."` | for a `CLAIMED_INTENT_WITHOUT_BROKER_RECORD` incident: looks the order up by client id **again**; if the broker has it, resolves from broker status (or refuses if it is live); only on a fresh 404 for a never-acknowledged intent does it release the slot, auditing the 404 trace id and your note |
| `flatten <OCC> [--submit]` | manual exit of a held contract at 95% of the broker mark; dry run prepares and releases, `--submit` sends |

There is deliberately no command that releases an intent the broker acknowledged.

**Loop mode.** `--every N` (N >= 60) runs a cycle every N seconds and stops
with exit 0 when the only reasons are `MARKET_CLOSED` / `NOT_A_SESSION`, or
with exit 3 when a reason implies broken state (claimed intent without a
broker record, unknown open order, position mismatch, account/history
problems) so a scheduler can alert. `--dashboard` re-renders the page after
every cycle. Start it once per session (cron / Task Scheduler).
`runtime/controller.lock` makes a second controller on the same directory
refuse to start; after a crash, delete the lock only once you have confirmed
the old process is gone.

## Raw activity-history staging (read-only)

```sh
python -m alpaca_agents.executor import-activities \
  --after 2026-09-01T00:00:00+00:00 --until 2026-09-15T00:00:00+00:00
```

This pages through `GET /v2/account/activities` for an explicit, timezone-aware
window using `direction=asc`, `page_size=100`, and `page_token` = last activity ID
(the documented Alpaca cursor). Every page is stored verbatim with its local trace
ID and broker `X-Request-ID` before the next request. Nothing is normalized into
`FillLedger`; fees, corrections, assignments, exercises, expirations, and unknown
future activity types are preserved rather than dropped.

The store is bound to the authenticated account identity from `GET /v2/account`;
reusing the file for another account fails before any activity request. `--until`
must not be in the future. Runs stop, with a failed record, on repeated cursors,
malformed/duplicate/oversized pages, HTTP or transport failures, persistence
errors, or the page cap (default 100 pages). Failed and interrupted runs keep
their partial evidence but are never reported as exhausted. Re-running creates
a new run, so changed broker records leave separate evidence.

The raw activity file contains account history; keep it out of git with
executor-only permissions. **Exhausted pagination is not reconciliation**: the
window may miss activity created earlier/later, Alpaca filters by creation time
rather than trade date, and no completeness/settled-cash verification exists.
Reports therefore always carry `reconciled: false`.

## Layer 1 scanner and strategy playbooks

`scan(snapshots, ScanConfig(...), as_of=<last completed session>, open_symbols=...)`
takes per-symbol `SymbolSnapshot`s (>=200 ascending daily `Bar`s, an option
`chain` of `OptionQuote`s, `next_earnings`, optional `iv_rank`) and returns:

- `proposals`: ideas from **enabled** playbooks after portfolio filtering. These
  are what get handed to the rules engine.
- `shadow`: every candidate idea from every playbook, enabled or not, for
  per-playbook stats without trading them.
- `skipped`: `{symbol, playbook, reason}` for everything not proposed. Nothing is
  dropped silently.

**All playbooks are disabled by default.** `ScanConfig.enabled_playbooks` is
empty; a playbook only proposes once you add it after backtesting. Unknown names
raise. This is a default-off configuration switch, **not an evidence-verifying
backtest gate** yet. No strategy has been backtested here. The shadow CLI never
sets this switch; a future execution path must require reviewed backtest evidence.
Strategy 5 (credit spreads) is not implemented and cannot be enabled.

Field naming: the idea's `strategy` is the *structure* the rules engine validates
(`long_call`, `long_put`, `call_debit_spread`, `put_debit_spread`). The playbook
tag is the separate `playbook` field (`trend_directional`, `trend_debit_spread`,
`oversold_bounce`, `breakout_continuation`). `exit_plan` and `session` are
informational; the rules engine ignores unknown keys.

Cross-cutting filters, in order: universe allowlist (default SPY/QQQ/IWM;
extend deliberately); one open idea per symbol (`open_symbols` must come from
executor state); stock earnings date required and must fall after the chosen
expiration (unknown => rejected); supported index ETFs can explicitly declare
`earnings_not_applicable=True` without a fabricated date. That exemption cannot
be applied to individual stocks. Bars must end on `as_of`; option legs need
OI >= 500, bid > 0, bid-ask <= 10% of mid, 30-45 DTE; premium + estimated fees
<= $100 (`fee_per_contract` defaults to $0.65 as an unverified placeholder, NOT
a guaranteed overestimate). Signed delta, finite values, standard strike format,
and cent-denominated quotes are validated. `valuation_day` is the quote/DTE date,
which may differ from the last completed bars session.

Signals (`signals.py`), all pure functions over bars:

- **trend** (Strategies 1 & 2): close > EMA20 > EMA50 with positive 20-day
  momentum (mirrored for shorts via puts). Entry is the close on a pullback
  within 1% of EMA20, otherwise the prior bar's high/low; skipped if price already
  ran >1.5% past the trigger. Stop = EMA20. Target = nearest pivot high/low in
  the trade direction over 60 sessions, or a projected 1.5R when no level exists
  (thesis says which). Both structures are built from the same signal; the
  spread is preferred when `iv_rank` >= 50 or unknown, the long when lower.
  Only the higher-scoring one per symbol is proposed.
- **oversold_bounce** (Strategy 3): close > SMA200 and (RSI14 < 35 or three down
  closes into a rising EMA50). Long call only. Stop = 10-session low; target =
  nearer of EMA20 or nearest pivot high.
- **breakout** (Strategy 4): 10-session range < 5% wide, top within 3% of the
  60-session high, close above it on > 1.5x 20-day volume, not >1.5% extended.
  Bull call spread only. Stop = range top; target = measured move.

Every signal recomputes reward:risk from its own levels and skips below 1:1; the
rules engine recomputes it again from the JSON and does not trust either.

Structures (`contracts.py`): long = delta 0.35-0.45 closest to 0.40, limit at
the ask. Spread = buy ~0.40 / sell ~0.25 same expiry, debit = buy ask - sell bid,
requires `width - debit >= debit` and `debit * 100 <= max_risk`.

Portfolio rules (`scan.py`): highest score per symbol; index ETFs (SPY/QQQ/IWM/
DIA) share one correlation bucket, other symbols are their own bucket; a bucket
already held or already proposed blocks further proposals; proposals are capped
to free slots (`max_open_positions - len(open_symbols)`).

`score` is a placeholder (signal strength, capped reward:risk, liquidity pass,
structure preference) for ranking only. Tune it with backtest evidence.

**What Layer 1 does not do yet:** fetch stock earnings calendars or historical IV
rank, backtest, or manage exits. Current implied volatility is not IV rank; the
new adapter leaves rank unknown rather than inventing it. `exit_plan` records the
playbook's premium/time/underlying stop rules for a future position manager;
today nothing closes positions. The scanner never holds broker credentials and
calls no LLM.

## Polygon/Massive data and manual shadow scans

The current Polygon documentation redirects to Massive. This adapter pins
`https://api.massive.com` (no URL override) and uses:

- [Daily OHLC aggregates](https://massive.com/docs/rest/stocks/aggregates/custom-bars):
  `/v2/aggs/ticker/{symbol}/range/1/day/{from}/{to}`, split-adjusted, ascending.
- [Option chain snapshots](https://massive.com/docs/rest/options/snapshots/option-chain-snapshot):
  `/v3/snapshot/options/{symbol}`, 30-45 DTE, paginated with same-origin/path
  validation, duplicate/cursor detection and a page cap.

Provide `MASSIVE_API_KEY` (or legacy name `POLYGON_API_KEY`) **only in the market
scanner process environment**, via your secret manager or a secure prompt. The
client uses an Authorization header; no key enters URLs, audit records or git.
No Alpaca key is read. There is no fallback to broker network access. No retries,
redirects, or environment proxies are followed. HTTP failures and provider
request IDs are durably audited before any successful data return.

After installing, run during the options trading session with a data subscription
that includes **real-time option quotes, Greeks and open interest**:

```sh
python -m alpaca_agents.scanner --session YYYY-MM-DD --symbols SPY QQQ IWM
```

Replace `YYYY-MM-DD` with the last completed US trading-session date. Until
calendar integration, the adapter conservatively requires it to be 1-4 calendar
days before today's UTC date; it does not load a same-day partial bar. This check
is not a holiday/session-completeness verifier. Daily bars are provider ET-day
aggregates, not necessarily regular-session-only candles. It requests 450
calendar days and requires >=200 ascending, consistent bars ending on the given
date. Unexpected pagination, duplicate dates, malformed prices or stale final
bars fail the symbol rather than shortening history silently.

The shadow CLI permits only SPY/QQQ/IWM/DIA, whose corporate earnings filter is
explicitly inapplicable. The Python adapter supports individual stocks only when
the caller supplies a verified future earnings date; fetching/validating that
calendar remains unfinished.

Option rows must have matching underlying/OCC contract identities, standard
100-share deliverables, Greeks, OI, positive bid/ask sizes and a `REAL-TIME` quote
no older than 120 seconds. Delayed, missing, stale or malformed records produce
filter diagnostics; no valid quotes means a rejected snapshot. Quote age is
checked again after multi-symbol retrieval. Pre-market or after-hours runs will
normally reject stale quotes; they are **not** made permissive to force signals.
Fresh quotes do not prove the separate Greeks calculation is fresh, and OI is
the previous trading day's number per the provider contract.

Outputs (both under ignored `runtime/` by default):

- `market-data.jsonl`: sanitized request lifecycle, contract filters, snapshot
  failures, shadow ideas and scan skips. Single-process writer; no raw responses
  or secret headers. Audit failure stops the run.
- `shadow-scan.json`: atomically replaced report, source timestamps, diagnostics,
  shadow candidates and `proposals: []`. Override with `--output` / `--audit`.
  Nonzero exit status indicates a snapshot or persistence error; successful
  symbols may still have shadow results in the report.

This is a **current-market preview**, not point-in-time historical option data or
a backtest. Do not pair current chain snapshots with historical bars to claim
historical performance. Reports are not executable authorizations. The engine
has no verified backtest results, automatic enablement, portfolio reconciliation,
or trading connection. Protect these report/audit files and keep them out of git.

## Backtest replay (underlying-level evidence, not options P&L)

```sh
# fetch + cache several years of daily bars, then replay all playbooks
python -m alpaca_agents.backtest --symbol SPY --start 2019-01-01 --end 2026-09-12 \
  --save-bars runtime/bars-SPY.json
# repeatable, offline re-runs
python -m alpaca_agents.backtest --symbol SPY --bars-file runtime/bars-SPY.json
```

`replay()` walks the bars session by session. At each session it hands the
signal functions only the bars up to and including that session (no look-ahead),
keeps at most one open trade per playbook per symbol, and then simulates the
underlying forward:

- **Fill**: next session's open for close-based entries; for trigger entries,
  the first of the next 3 sessions whose high/low touches the trigger, filled at
  the worse of open or trigger. No touch => `no_fill`.
- **Stop**: close-based, matching the playbook's "closes back through" wording.
  Trend uses the *current* EMA20 each session; bounce and breakout use the fixed
  swing-low / range-top level. A gap through the stop exits at the worse close.
  Same-session stop and target: the stop is assumed to have hit first.
- **No trade**: a fill already through the stop, or at/beyond the target, is
  `no_trade` (`fill_beyond_stop` / `fill_beyond_target`). A rational executor
  would not enter; these are counted and excluded from every statistic. The
  old replay booked the first as a synthetic -1R and the second as a "win".
- **Noise-level stops are refused** at the signal level (`MIN_RISK_ATR`: risk
  must be >= 0.5 x ATR14), live and in replay. Before this, the trend signal's
  EMA20 stop could sit cents from entry and a bounce's "swing low" was often
  the current bar's low: an 8-cent stop turned a 1% gap into -38R and cent-wide
  stops produced +13R "wins", inflating QQQ trend's mean 2.5x.
- **Target**: intraday touch (high >= target for longs).
- **Time stop**: approximated in sessions (`HOLD_LIMIT`: trend/breakout 15,
  bounce 10); exits at that session's close as `timeout` with its actual R.
- Trades that cannot resolve before the data ends are `unresolved` and excluded.

`summarize()` keeps two questions apart. **Outcome** is the sign of R (win /
loss / flat). **Exit reason** is why the trade ended (`exits`: stop / target /
timeout). A trend position closed by its rising EMA20 in profit is a win that
exited on the stop rule; the old code called it a loss and could call a
negative-R target touch a win, so `win_rate` was not what it claimed. Per
playbook: signals, `no_fill`, `no_trade` (with reasons), resolved, wins/losses,
`win_rate` (sign of R), `target_hit_rate`, `expectancy_r` (mean), `median_r`,
`profit_factor`, average and max win/loss R, median sessions held,
`failed_breakout_rate`, and the gates `sample_sufficient` (>= 30 resolved) and
`negative_expectancy`. **Read the mean with the median and profit factor**:
gaps through stops are open-ended in R, and a positive mean over a negative
median (IWM trend after the audit: mean +1.0, median -0.4, one -8R gap) means
a few trades carry the result.

**What this does and does not prove.** An R-multiple here is on the underlying.
A 1.5R underlying win can still be a losing option trade after IV crush, theta,
bid-ask and fees; a -1R underlying loss is roughly the option's -50% premium stop
only by coincidence. Establishing *options* expectancy needs point-in-time option
quotes, which this repo does not have. Treat these reports as a necessary filter
(a playbook that loses on the underlying will not be saved by the option), not as
sufficient evidence. The report says `options_pnl_modelled: false` and carries
its caveats inline. Nothing in the backtest can enable a playbook; enablement
remains a manual, reviewed edit to `ScanConfig.enabled_playbooks`.

History is fetched in <= 700-day chunks via the same audited market-data client,
checked for chunk continuity, and validated as one ascending series. Provider
daily aggregates are split-adjusted ET-day bars; dividend adjustment and
survivorship are not handled. Keep cached bars under `runtime/` (ignored).

## Remaining gaps

Done: paper client, ledgers, activity staging, reconciliation with real
provenance/coverage/settlement checks, order journal, single-attempt paper
transport, exit manager, controller, notifications, dashboard, full offline
lifecycle test.

Still open, roughly in priority order:

1. **Nothing has touched a real paper account.** Run the first-run sequence
   above and fix whatever the real API disagrees with (field names, activity
   shapes, order echo, fee timing).
2. **Session calendar** is rule-based (`calendar.py`: NYSE holidays incl.
   weekend observance, Good Friday, Juneteenth). Ad-hoc closures are not
   modelled; the broker clock's `is_open` still gates every cycle.
3. **Spreads.** `trend_debit_spread` and `breakout_continuation` ideas are
   validated by the rules engine but refused by the journal
   (`UNSUPPORTED_EXECUTION_STRUCTURE`): multi-leg orders, ledger lifecycles,
   position matching and assignment/expiration policy do not exist.
4. **Assignment / exercise** (`OPASN`, `OPEXC`) block reconciliation; only
   worthless expiration (`OPEXP`) is handled. Its activity shape is from the
   API docs, not observed: a mismatch blocks rather than guesses.
5. **Unfilled orders.** Entry and exit orders are day limits. An unfilled exit
   is re-evaluated next session with a fresh decision key; there is no
   cancel/replace and no "chase" logic. An unfilled entry simply expires.
6. **Options-level backtest.** The replay is underlying-level R only. Enable
   a playbook only after reviewing it, and cut any playbook with negative
   expectancy over 30+ triggered ideas.
7. **Live trading**: not designed, not planned in this repository. It would
   need a separate default-off configuration path and explicit owner approval
   after a paper track record.

Current JSONL logging is a single-process milestone-1 adapter, not concurrent
transactional storage. Process/container permissions must enforce the architectural
boundaries; Python modules alone are not a security sandbox. Paper orders are wired up
behind the `--submit` flag, the control file, playbook approval markers and full
reconciliation; keep all four in place.

Alpaca secrets belong only in the executor's environment or secret manager.
Market-data keys belong only in the separate scanner's environment. Never put
credentials in ideas, thesis text, audit records, fixtures, or git. `.env` and
`runtime/` are ignored. No secrets are needed to run these tests.
