# Operating Playbook

How to run the paper trading agents day to day. Every command below is run
from the project root in PowerShell. Nothing here places a live-money order:
the broker URL is hardcoded to paper.

The system has four agents, and you are the fifth:

| Agent | What it does | Trusts |
|---|---|---|
| Scanner | Reads bars + option chains, proposes ideas | nothing (no broker access) |
| Rules engine | Accepts or rejects each idea deterministically | nothing (no I/O) |
| Executor | Reconciles with the broker, places/exits orders | the journal only |
| Dashboard / notifier | Shows state, writes alerts | read-only |
| **You** | Approve playbooks, set the control mode, resolve incidents | evidence in `runtime/` |

All four are locked down by default. Your job is to turn things on **only when
you have evidence**, and to read the evidence every evening.

---

## 0. One-time setup

### Keys (every new PowerShell window)

```powershell
cd C:\Users\eveli\OneDrive\Desktop\AlpacaAgent
$env:ALPACA_PAPER_API_KEY    = "PK..."        # Alpaca paper key
$env:ALPACA_PAPER_API_SECRET = "..."          # Alpaca paper secret
$env:MASSIVE_API_KEY         = "..."          # Massive/Polygon market-data key (scanner + exits need it)
```

The Massive key comes from https://massive.com (formerly Polygon). The free
tier is delayed data; the scanner **refuses** delayed quotes, so you need a plan
with realtime options quotes (Options Starter or above).

Save these in a `keys.ps1` file *outside* the repo and dot-source it
(`. C:\Users\eveli\keys.ps1`) so you never paste them into chat again.

### Files that must exist (create with Python so they are UTF-8, not PowerShell `echo`)

```powershell
python -c "from pathlib import Path; Path('runtime/paper-margin-acknowledged').write_text('ACKNOWLEDGED', encoding='utf-8')"
python -c "from pathlib import Path; Path('runtime/trading-control').write_text('DISABLED', encoding='utf-8')"
```

`runtime/trading-control` is the master switch. Exact contents:

| contents | entries | exits |
|---|---|---|
| `ARMED_PAPER` | yes | yes |
| `EXITS_ONLY` | no | yes |
| `DISABLED` (or missing/garbled) | no | no |

It is read on **every** evaluation, so changing it takes effect at the next
cycle without a restart. `DISABLED` while holding a position means the system
will not manage the exit — use `EXITS_ONLY` for that.

### Health check

```powershell
python -m alpaca_agents.executor account      # ACTIVE, options level >= 2
python -m alpaca_agents.executor reconcile    # only reason should be MARKET_CLOSED off-hours; none during RTH
```

---

## 1. Phase A — backtest (do this before anything else, off-hours is fine)

The playbooks are hypotheses. Test each one against 3 years of daily bars:

```powershell
python -m alpaca_agents.backtest --symbol SPY --start 2023-01-01 --playbooks trend --save-bars runtime/bars-SPY.json --output runtime/bt-SPY-trend.json
python -m alpaca_agents.backtest --symbol SPY --bars-file runtime/bars-SPY.json --playbooks oversold_bounce --output runtime/bt-SPY-bounce.json
python -m alpaca_agents.backtest --symbol SPY --bars-file runtime/bars-SPY.json --playbooks breakout --output runtime/bt-SPY-breakout.json
```

Repeat for QQQ, IWM, DIA (`--save-bars` once per symbol, then `--bars-file`).

What to read in each output:

| metric | keep if | delete playbook if |
|---|---|---|
| resolved trades | >= 50 across all symbols | < 30 (no conclusion possible) |
| mean R (`expectancy_r`) | > 0.15 | <= 0 |
| **median R** | > 0 | < 0 while the mean is positive: outliers carry it, do not trust the mean |
| profit factor | > 1.3 | < 1.1 |
| worst R (`max_loss_r`) | you could take it twice in a week | you couldn't |
| `no_trade` count | small | large: the signal fires into gaps it cannot trade |

`win_rate` is the fraction of trades with positive R; `exits` says why trades
ended. They are independent: a trade can exit on the stop rule in profit.

This is **underlying-level R** (did price reach target before stop). Option
P&L will be worse: spread, theta, and a 30-45 DTE contract that runs out of
time. Treat a marginal backtest as a fail.

Pick the **single best** playbook. Write down why in `runtime/decisions.txt`.

---

## 2. Phase B — shadow mode (2-4 weeks, RTH only)

Nothing enabled. The system reconciles, scans, evaluates, and refuses every
idea. You're testing the plumbing against the real API and checking whether
the ideas look sane.

Each trading morning, ~9:45 ET (let the open settle):

```powershell
. C:\Users\eveli\keys.ps1
python -m alpaca_agents.controller --runtime runtime --every 300 --dashboard
```

Leave it running. It stops on its own at the close (exit 0) or on an
incident (exit 3). Open `runtime\dashboard.html` in a browser and refresh
whenever you like.

Each evening, read the last few lines of `runtime/cycles.jsonl`:

```powershell
Get-Content runtime\cycles.jsonl -Tail 3 | ConvertFrom-Json | ConvertTo-Json -Depth 6
```

You're looking for:
- `stages.reconcile.ok: true` every cycle during RTH. If not, the reason
  tells you what the real API disagreed with — report it and we fix the code,
  never the check.
- `stages.ideas` — would you have taken that trade by hand? Note it.
- `stages.entries[*].reason` — every idea should say the playbook is not
  enabled. Anything else is a bug.

Shadow mode is done when you've had **10 clean sessions** with zero
reconciliation failures and the ideas look like the playbook you tested.

---

## 3. Phase C — one playbook, armed

```powershell
python -c "from pathlib import Path; Path('runtime/playbooks').mkdir(exist_ok=True); Path('runtime/playbooks/trend_directional.approved').write_text('APPROVED', encoding='utf-8')"
python -c "from pathlib import Path; Path('runtime/trading-control').write_text('ARMED_PAPER', encoding='utf-8')"
```

Then the daily loop gains two flags:

```powershell
python -m alpaca_agents.controller --runtime runtime --every 300 --dashboard --submit --enable-playbook trend_directional
```

Everything else stays the same. The guardrails you should **not** touch:

| limit | value | why |
|---|---|---|
| risk per trade | $100 | one bad idea is 5% of capital |
| concurrent positions | 2 | attribution: you can tell which trade did what |
| entries per cycle | 1 | never two orders on one signal burst |
| daily loss breaker | $40 cumulative losses | latches for the day; wins don't reset it |
| same-session exits | premium stop only, max 3 day trades per 5 sessions | protection on day one; PDT rules on a margin account under $25k; never same-day profit-taking |
| capital cap | $2,000 | the $100k paper balance is not your budget |

If you find yourself wanting to widen these because paper "isn't real", stop.
You'd be testing a different system from the one you'll trade live.

---

## 4. Daily routine (Phase C)

**Morning, ~9:45 ET**
1. Dot-source keys, start the loop (command above).
2. Glance at the dashboard: positions match what you expect from yesterday?

**Midday** — nothing. Don't watch it. The exit manager checks every cycle:
underlying stop > EMA20 rule > premium stop (-50%) > target > time stop (21 DTE
or max sessions held). Exits are priced at the live bid so they fill.

**Evening, after 4:15 ET**
1. Read `cycles.jsonl` tail (command above).
2. If a trade closed, add one line to `runtime/journal.txt`: entry reason,
   exit reason, did the rule fire when you expected, anything surprising.
3. Check `runtime/notifications.jsonl` for anything flagged.

**Weekly**
- Count closed trades. Below 30, no conclusions. At 30+: is expectancy
  positive after fees you'd pay live (~$1.30/contract round trip)?
- Open positions older than 10 sessions with no exit: is the time stop
  configured the way you think?

---

## 5. When something goes wrong

The loop exits with code 3 and the last cycle's `halt_reason` says why.
These require **you**, and the system will not trade again until resolved:

| reason | meaning | what to do |
|---|---|---|
| `CLAIMED_INTENT_WITHOUT_BROKER_RECORD` | we sent an order and got no answer; broker has no record | check the Alpaca web UI for the order. If truly absent: `python -m alpaca_agents.executor release-intent <auth_id> --note "checked UI at HH:MM, no order"`. If present, run `reconcile` — it will match it. |
| `POSITION_MISMATCH` | broker positions != ledger | did you trade manually in the Alpaca UI? Don't. If an expiry/assignment happened, report it. |
| `ACCOUNT_*` | account status / flags changed | look at the Alpaca dashboard |
| `HISTORY_GAP` | activity import has a hole | `python -m alpaca_agents.executor import-activities` then `reconcile` |
| `BREAKER_TRIPPED` | -$40 on the day | nothing. It resets at the next session. Read what lost. |

To get out of a position manually (e.g. you're going away for a week):

```powershell
python -m alpaca_agents.executor flatten SPY261016C00500000            # dry run: shows the order
python -m alpaca_agents.executor flatten SPY261016C00500000 --submit   # sends it at the bid
```

To pause entries but keep managing exits: write `EXITS_ONLY` to
`runtime/trading-control`. To stop everything: `DISABLED` (and flatten first
if holding).

Never fix a state problem by editing the SQLite files. If reconciliation
can't be satisfied, that is information about a bug — report it.

---

## 6. What "good" looks like after 60 days

- Zero reconciliation failures you couldn't explain.
- 30+ closed trades on one playbook.
- Every exit fired for the reason you'd predict from the rules.
- Expectancy positive after modeled fees, drawdown within backtest range.

Only then: enable a second playbook (repeat Phase B/C for it), or talk about
spreads (`trend_debit_spread` exists in the scanner but needs multi-leg
execution built), or talk about live — which is a separate design
conversation, not a flag.

## 7. What "bad" looks like, and what it isn't

Bad: reconciliation failures you ignored; limits loosened mid-test; a
playbook enabled without a backtest; rules changed after a losing week.

Not bad: a losing month inside backtest drawdown; the breaker tripping;
ideas refused for liquidity. Those are the system working.
