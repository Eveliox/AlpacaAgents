# AlpacaAgents

Paper-only options swing-trading system, built safety-first.

**Status: milestone 1 complete; milestone 2 in progress — read-only paper client,
persistent accounting foundations, raw activity staging, and a Layer 1 scanner
with a Polygon/Massive market-data adapter and shadow-only CLI.**
No order submission, live configuration, scheduler, or dashboard exists yet. An `approved: true` result is a validation decision, not an executable
authorization. Nothing in this repository places trades. Broker connectivity has
only been tested using fake responses, not real credentials. The market-data
adapter is also fake-transport tested; provider entitlements/connectivity have
not been tested with a real data key.

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
- `src/alpaca_agents/executor/`: exclusive broker boundary, read-only paper client,
  durable SQLite request traces, and manual connectivity CLI.
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

Pass an owner-controlled path as `control_file` to `validate_and_log()`.
Only the exact trimmed contents `ARMED_PAPER` allow validation. Any other contents,
missing file, or read error block it. To stop validation without a redeploy:

```sh
mkdir -p runtime
printf 'STOP\n' > runtime/trading-control
```

Arming the standalone validator (does **not** enable an executor):

```sh
printf 'ARMED_PAPER\n' > runtime/trading-control
```

Keep this path outside scanner write permissions. In production use atomic file
replacement. A kill switch cannot undo fills or automatically cancel existing
orders; cancellation needs a separately validated, audited executor workflow.

## Read-only paper connectivity and Request IDs

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
will be added with order authorization; no such IDs exist in this read-only flow.

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

## Remaining milestones / execution prerequisites

1. Extend the read-only paper client into an executor in a separate
   credential-holding process. The URL is hardcoded to
   `https://paper-api.alpaca.markets`; no live path until owner sign-off. Verify
   options permissions and whether the broker actually supports the intended cash
   account behavior. Do not assume a paper account models settled cash correctly.
2. Verified activity normalization (window continuity, order/leg mapping, fee
   matching, expirations/assignments), feeding `FillLedger`, then trusted account,
   position and settlement reconciliation with persistent P&L and breaker latch. Broker-backed
   market/account data must go through the executor; scanner has no broker keys.
3. Transactional decision IDs, single-use authorization, position/cash/rate
   reservations, and kill-switch/risk revalidation immediately before submission.
   JSON `approved: true` from a caller must never be sufficient authorization.
4. Limit-only debit entries; spreads submitted atomically as multi-leg orders,
   never independent legs. Verify entry trigger, quote freshness/liquidity,
   option eligibility, contract multiplier, and fees before authorization.
5. Idempotent client order IDs, acknowledgment/fill/partial-fill tracking, and
   uncertain-outcome reconciliation. Never automatically resubmit a failed or
   timed-out request. A new attempt must return through rules validation.
6. Separate validated close/cancel flow, trusted entry dates, no same-trading-day
   round trips, stop/target monitoring, and expiration/assignment controls.
   **The current engine rejects all closes. It does not yet implement position
   management or enforce holding duration on broker positions.** Overnight gaps
   can cross stops. Spread assignment can create stock/cash obligations; do not
   enable spread execution without an expiration/assignment policy.
7. Verified session calendar, stock earnings, IV history and data fallbacks;
   backtesting with point-in-time historical option quotes/fees/exits (a pure
   underlying-bar replay cannot establish options expectancy), using
   vectorbt/backtrader per the playbook; measure per-playbook expectancy and
   failed-breakout rate before enabling any playbook; extend
   transactional ledger events to all order/decision events; notifications;
   read-only dashboard; then scheduled paper runs. Cut any playbook showing
   negative expectancy over 30+ triggered ideas.
8. Only after a paper track record and explicit owner approval: design a separate,
   default-off live configuration path. This repository has none.

Current JSONL logging is a single-process milestone-1 adapter, not concurrent
transactional storage. Process/container permissions must enforce the architectural
boundaries; Python modules alone are not a security sandbox. No paper orders should
be wired up until the trusted state and execution prerequisites above are ready.

Alpaca secrets belong only in the executor's environment or secret manager.
Market-data keys belong only in the separate scanner's environment. Never put
credentials in ideas, thesis text, audit records, fixtures, or git. `.env` and
`runtime/` are ignored. No secrets are needed to run these tests.
