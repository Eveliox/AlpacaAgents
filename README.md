# AlpacaAgents

Paper-only options swing-trading system, built safety-first.

**Status: milestone 1 implemented — standalone rules engine and tests.** No broker
client, credentials, order submission, live configuration, scanner, scheduler, or
dashboard exists yet. An `approved: true` result is a validation decision, not an
executable authorization. Nothing in this repository places trades.

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

## Remaining milestones / execution prerequisites

1. Paper executor in a separate credential-holding process. Hardcode
   `https://paper-api.alpaca.markets`; no live path until owner sign-off. Verify
   options permissions and whether the broker actually supports the intended cash
   account behavior. Do not assume a paper account models settled cash correctly.
2. Trusted account, fills, contract metadata, fee estimates, and settlement
   reconciliation, with persistent P&L and circuit-breaker latch. Broker-backed
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
7. Transactional audit storage, circuit-breaker transition events, notifications,
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
