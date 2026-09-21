# Scalp / swing profiles

Owner request: continuously watch stocks/ETFs for option scalps and switch between
scalp and swing styles. Paper only. Existing QQQ swing authorization is not an
approval of a new scalp execution path or an expanded trading universe.

## Delivered first stage: underlying research (not scalp trading)

`python -m alpaca_agents.research_scan --style scalp|swing` is an independent,
GET-only research runner. It does not import a broker client, reserve an intent,
change controls, approve a playbook, or submit. It can coexist with the armed
controller because it owns only `runtime/research-scans/<style>.lock` and reports.
It never deletes the controller lock. Invalid/unknown profiles are refused.

- `--every 15` (scalp minimum) or `--every 60` (swing minimum) repeats **after each
  completed pass**, not at guaranteed wall-clock intervals. No overlapping scans.
  Ctrl+C stops research, not the trading controller. Three consecutive data
  failures stop a sweep and the loop with a partial report, not "no setups".
- `--symbols` or `--watchlist` (JSON array, 1–505 explicitly named underlyings).
  There is no built-in/current S&P membership list. The existing data adapter
  accepts uppercase alphabetic symbols of 1–6 letters; class-share punctuation
  is unsupported and rejected, not silently dropped. Large lists are sequential,
  can take minutes, and do not constitute a synchronized 500-stock live scanner.
- Stocks can have technical observations **without** pretending their earnings
  dates are known: every observation is non-executable and non-index-ETF names
  carry an earnings/instrument-verification blocker. No earnings exemption added.
- Reports retain per-symbol observation/bar timestamps. Neither the latest-pass
  timestamp nor a setup label proves current quote freshness or trade readiness.
- Dashboard **Scan profiles** selects which saved research report to view. It is
  NOT a running-process/trading-mode toggle; refresh the page for new reports.
  Both reports remain readable without JS. No HTTP action route added.

### Scalp hypotheses (unvalidated starting parameters, not universal standards)

- One-minute underlying bars, **completed only**, regular session from 09:30 ET.
  Full contiguous session required for session VWAP; do not interpolate missing
  minutes. **Session VWAP is (high+low+close)/3 × volume** — the standard chart
  approximation. An earlier version of this doc said not to substitute it for the
  provider's per-bar `vw`; the first live session (2026-09-21) showed why that
  was wrong: Massive's `vw` includes late-reported block prints assigned to the
  wrong minute (QQQ 10:33, 1.46M shares at 721.8 against a bar low of 735.1).
  One such bar moves the session VWAP by dollars and fabricates reclaim signals.
  The provider `vw` is now ignored entirely.
- Need 22 completed minutes for EMA9/21 and the preceding 20-minute volume mean.
  Latest completed bar ends no more than 90 seconds before evaluation. Reject
  future, duplicate, unordered, nonfinite, malformed, incomplete or stale bars.
- Observe entries only 09:50–11:30 and 13:30–15:30 ET on scheduled sessions.
  Actual warmup lasts until 09:52. This is a research schedule, **not a verified
  exchange-open status**. Ad hoc closures and early closes are not modeled here;
  stale-data checks remain required. No broker requests from research.
- Underlying price >= $5 and preceding 20-minute average dollar volume >= $1m.
- Confirmation-bar volume >= 1.5x preceding 20 minutes (not same-time-of-day RVOL).
- Bullish: close > EMA9 > EMA21 and above session VWAP; bearish mirrored.
- Fresh close through the first 15-minute opening range, or fresh VWAP
  reclaim/loss. Prior bar must still be on the other side (no repeated chase).
- Structural stop at last five minutes' low/high; distance must be 0.5–1.5 ATR14.
  Projected target 1.5 times underlying stop distance: **not option return**, not
  a measured resistance target, not a probability. No performance claims.

### Swing research

Reuses existing daily trend logic on the previous completed scheduled session,
including EMA20/50, momentum, underlying target/risk and ATR noise gates. It does
not fetch option contracts or loosen existing swing scanner/rules/exits.

## Not delivered: automated scalp orders / trading-mode switch

A faster swing loop still scans daily candles. A UI selector must never relabel
existing swing positions as scalps. Before scalp execution can be implemented and
enabled, require all of the following:

1. Explicit exit-policy decision: current system only permits protective same-day
   exits. Scalp profit/time/session-end exits are a separate policy change, not
   a hidden relaxation. Keep the local three-day-trades/five-sessions budget
   unless the owner explicitly revises it. Reserve/check an intraday-exit slot
   **before entry**, including outstanding intents; don't strand a scalp overnight.
2. Verified market-open, earnings/event/halts inputs; stocks fail closed when
   unknown. Options selection at entry with fresh two-sided realtime quotes,
   usable sizes, spread/OI/delta/DTE limits, round-trip cost/slippage model, and
   premium-plus-fees within existing $100/$2k limits. No 0DTE default.
3. Per-position immutable profile/version in the journal. Switching future-entry
   profiles requires no open/pending positions in v1; outstanding claims and
   unknown outcomes block switching. Existing positions keep their original exit
   rules, even after a requested profile change.
4. Deterministic scalp exits, premium stop, time stop, no-new-entry cutoff,
   session-end liquidation attempt through the sole stored-body submitter.
   Protective execution is not guaranteed: disconnects, halts, wide spreads and
   non-fills require explicit incidents; unknown submissions never retry.
5. Full fake-broker lifecycle tests for fills, partials, all intraday exits,
   day-trade budget exhaustion, switching/restarts, stale quotes and loss breaker.
   Separate replay with point-in-time option quotes/fees/slippage and out-of-sample
   evaluation; underlying R is not option P&L.
6. New playbook approval and launch enablement. Keep $100 premium risk, two
   positions, $2k capital cap and $40 cumulative daily loss breaker unchanged.

Current rules, exits, controller and submitter remain unchanged by this stage.
Research does not manage positions. Do not stop the position manager to run it.
