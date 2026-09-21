# CLAUDE.md

Start by reading `HANDOVER.md`. It has the current state, account facts, audited
backtest numbers, the next milestones, and the invariants. `README.md` is the
full reference; `PLAYBOOK.md` is the operator routine. The current task spec is
`docs/TASK-chat-server.md`.

## Hard rules (do not relax without the owner's explicit, written decision)

1. **Paper only.** The broker URL is hardcoded to paper. Never add a live URL, flag, or env override.
2. **One broker boundary.** Only `executor/client.py` calls Alpaca; only `executor/submit.py` POSTs orders, and it sends the journal's stored body. Never add another path that reaches the broker — including from the dashboard, chat, or any future server.
3. **Rules stay pure.** `rules.py` and `executor/exits.py` are deterministic, no I/O, no LLM. Same inputs, same decision.
4. **Fail closed.** Missing, unknown, or malformed → rejected / DISABLED / Unknown. Never default to permissive, never show 0 for unknown.
5. **One attempt per order.** Unknown outcomes stay `claimed` for reconciliation. Never retry.
6. **Nothing enables itself.** Playbooks need `runtime/playbooks/<name>.approved` == `APPROVED` *and* `--enable-playbook`. Orders need `--submit`. No code path may create these.
7. **Backtests are underlying-level R, not option P&L, not dollars.** Keep `options_pnl_modelled: false` and the mean/median/profit-factor/worst-R presentation together. Never call a playbook "profitable" from these.
8. **Tests are the spec.** Run `python -m unittest discover -s tests` and `node --test tests/test_dashboard_chat.cjs` before every commit. Behaviour changes need tests; the full-lifecycle controller test must keep passing.
9. **Secrets never enter the repo, the dashboard, or chat.** The owner's keys were exposed in a chat transcript and must be rotated — remind them.
10. **Windows.** Create marker files with Python (`write_text(..., encoding="utf-8")`), never PowerShell `echo` (UTF-16).
11. **No model in a data gate.** `runtime/earnings.json` is written only by the owner via `python -m alpaca_agents.earnings set`. Never have an LLM, a chat tool, or any code path fill in earnings dates (or any other fact the scanner refuses to trade without). A model's recall is not a source; unknown stays unknown and the stock stays skipped. The owner asked for this once and was refused; refuse again.

The repo lives at `E:\AlpacaAgent` (moved from C: on 2026-09-21 when that drive filled). Use `.venv\Scripts\python.exe` there; `E:\AlpacaLaunchers\Start-QQQ-Paper.ps1` is the launch script. The old C: path's `runtime` is a junction to E:'s, so an old command still hits the same lock.

## Working style

- Be direct about what evidence does and doesn't show. The owner wants to trade; the system's job is to make that safe, not easy.
- When the real API disagrees with the fake, fix the code, never the check.
- Small commits with messages that say why the old behaviour was wrong.
- Write patch scripts to a file when they contain triple-quoted strings.
