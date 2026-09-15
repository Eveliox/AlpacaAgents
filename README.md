# AlpacaAgents

Paper-only options swing-trading system, built safety-first.

**Status: milestone 1 complete; milestone 2 in progress — read-only paper client
and persistent closed-trade accounting foundation.**
No order submission, live configuration, scanner, scheduler, or dashboard exists
yet. An `approved: true` result is a validation decision, not an executable
authorization. Nothing in this repository places trades. Broker connectivity has
only been tested using fake responses, not real credentials.

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

**Not connected to trading authorization yet.** This version accounts only for
fully closed positions. A fill reconciler must handle partial-close realized P&L
on the actual realization day, allocations across spread legs, assignment,
exercise, expiry, corrections, and missing history before any use in `RiskState`.
Do not defer partial realized losses until the final close in a trading system.
Open positions, pending orders, settled cash, and history-completeness checks are
still absent. No import CLI is exposed to accept scanner-supplied accounting.

## Remaining milestones / execution prerequisites

1. Extend the read-only paper client into an executor in a separate
   credential-holding process. The URL is hardcoded to
   `https://paper-api.alpaca.markets`; no live path until owner sign-off. Verify
   options permissions and whether the broker actually supports the intended cash
   account behavior. Do not assume a paper account models settled cash correctly.
2. Trusted account, fills, contract metadata, fee estimates, and settlement
   reconciliation, integrating the ledger's persistent P&L and circuit-breaker
   latch with verified complete history and partial-realization accounting. Broker-backed
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
7. Extend transactional ledger events to all order/decision events, add notifications,
   read-only dashboard, technical scanner, backtesting, then scheduled paper runs.
8. Only after a paper track record and explicit owner approval: design a separate,
   default-off live configuration path. This repository has none.

Current JSONL logging is a single-process milestone-1 adapter, not concurrent
transactional storage. Process/container permissions must enforce the architectural
boundaries; Python modules alone are not a security sandbox. No paper orders should
be wired up until the trusted state and execution prerequisites above are ready.

Secrets belong only in the executor's environment or secret manager. Never put
credentials in ideas, thesis text, audit records, fixtures, or git. `.env` and
`runtime/` are ignored. No secrets are needed to run these tests.
