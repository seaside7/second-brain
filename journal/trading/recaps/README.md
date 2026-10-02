# Trading Daily Recaps

Per-day markdown recap of the trading-brain paper traders (analysis owned by second-brain).

## Trigger

Whenever the user asks for a trading summary/check (e.g. "check my trading", "give me a summary", "how did trading go today", "recap trading"), answer in chat with insight AND write/update that day's recap file.

## Rules

- **One file per day:** `YYYY-MM-DD.md`. New day = new file.
- **Multiple asks on the same day:** append an `Update @ HH:MM WIB` section to the same file - do NOT create a second file.
- **Missing past days:** when a recap is discovered to be missing for a past conversation, backfill it and mark `_Backfilled <date>_` at the top.
- **Every entry captures:** Trigger, Status snapshot (per leg: capital, change vs config, position), today's trades (entries/exits/PnL), observations & insights, decisions & pending items, notes.
- Keep references to trading-brain docs (STRATEGY.md / BACKTEST_LOG.md) instead of duplicating their content; details/data live in the trading-brain repo.
- Each recap is committed + pushed to second-brain `main` and pulled on the VPS (`/home/ubuntu/projects/second-brain`).

## Files

| File | Day | Contents |
|---|---|---|
| 2026-09-24.md | Thu | Trade summary (ASTER/SUI/CURRENT) + "remove SUI, fold into ASTER" decision |
| 2026-09-25.md | Fri | SUI removal, $2017.89 ASTER fold, $120/$600 caps, docs update rule |
| 2026-09-30.md | Wed | Breakout day - swing-gate gap finding, SL-cooldown vs CURRENT re-entry |
| 2026-10-01.md | Thu | Variant study + ATR-E paper leg deployed (bb 0.015, TP 2.5, gate both, 04:00-10:00) |
| 2026-10-02.md | Fri | ATR legs removed, focus on CURRENT; improvement backtest |
