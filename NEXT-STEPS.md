# Next Steps for AlpacaAgent

**Current state:** Commit `4d76cb4` - All systems built and tested, no trading yet

**Dashboard opened:** `runtime/dashboard.html` (refresh to see it)

---

## Three Paths Forward

### PATH A: Security + Features (RECOMMENDED - safest learning curve)
1. **Rotate API keys** (10 min) ⚠️ URGENT
2. **Live agent chat milestones 1-4** (3-5 days) - read-only + dry-run
   - Kickoff: paste prompt from `docs/TASK-chat-server.md` into Claude Code
3. **Readiness checklist** (0.5 day) - system health panel
4. **Trade review journal** (1 day) - learn from execution

**Result:** Better UX, zero new trading risk, deep system understanding

---

### PATH B: Fast to Paper Trading
1. **Rotate API keys** (10 min) ⚠️ URGENT
2. **Readiness checklist** (0.5 day)
3. **Trade review journal** (0.5-1 day)
4. **Options Advanced subscription** ($199/mo)
5. **Review** `runtime/bt-QQQ.json` trades (1 hour)
6. **Approve & arm:**
   ```powershell
   python -c "from pathlib import Path; Path('runtime/playbooks/trend_directional.approved').write_text('APPROVED', encoding='utf-8')"
   python -c "from pathlib import Path; Path('runtime/trading-control').write_text('ARMED_PAPER', encoding='utf-8')"
   ```
7. **First session:**
   ```powershell
   . C:\Users\eveli\keys.ps1
   python -m alpaca_agents.controller --runtime runtime --every 300 --dashboard --enable-playbook trend_directional --submit
   ```

**Result:** Paper trading in ~2-3 days, current dashboard UX

---

### PATH C: Everything in Order
Stage 1 → 2 → 3 → 4 → 5 → 6 from the summary above

**Timeline:** ~2 weeks

---

## Current Blockers

| Blocker | Impact | Fix |
|---------|--------|-----|
| **Keys exposed** | Security risk | Regenerate Alpaca + Massive keys, update `keys.ps1` |
| **No Options Advanced** | Scanner + exits can't price options | Upgrade subscription |
| **No playbooks approved** | Controller skips all ideas | Review backtest, create `.approved` file |
| **Control mode DISABLED** | Controller refuses entries | Create `trading-control` file |

---

## What the Dashboard Shows Now

- ✓ Control mode: DISABLED (safe)
- ✓ Account reconciled (tested live)
- ✗ Scanner: 3 snapshot errors (needs Options Advanced)
- ✓ Backtest: Audited QQQ/SPY/IWM numbers
- ✓ Chat: Offline rule-based replies work
- ✗ Positions: None
- ✗ Orders: None

---

## To Start PATH A (recommended)

```powershell
cd C:\Users\eveli\OneDrive\Desktop\AlpacaAgent
claude
```

Then paste the kickoff prompt from the bottom of `docs/TASK-chat-server.md`

---

**See also:**
- `HANDOVER.md` - full system state
- `PLAYBOOK.md` - daily operation guide
- `README.md` - complete reference
- `docs/TASK-chat-server.md` - chat server spec

**Last updated:** After backtest audit (commit 4d76cb4)
