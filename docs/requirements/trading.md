# Trading - Logic & Requirements (skeleton)

Bridge doc for the trading domain. **Strategy/backtest detail lives in the trading-brain repo** (`STRATEGY.md`, `DEPLOYMENT.md`, `BACKTEST_LOG.md`, branch `master`) - do not duplicate it here; this doc holds the index, the live config summary, and the second-brain-side change log. Daily recaps: `journal/trading/recaps/`. Operational reference skill: `.agent/skills/trading-sheets/SKILL.md` (kept in place).

## 1. Purpose & scope
- Paper-trading experiments (ASTERUSDT), two engines live since 2026-10-02: **CURRENT** (Mon-Thu BB-fade, ATR exits, 8x, 50% bet) and **WEEKEND** (Sat+Sun volume-confirmed momentum breakouts, $1000, 25% bet x 8x). ATR-D / ATR-E legs and the SUMMARY comparison were removed 2026-10-02; SUI leg removed 2026-09-25.
- Analysis and records live here (second-brain); data/logs live on the VPS in trading-brain.

## 2. Inputs / sources
- trading-brain repo on VPS (`/home/ubuntu/projects/trading-brain`), branch `master`.
- State/logs: `journal/state/paper_trader_state.json` + `journal/paper_trader.log` (CURRENT), `journal/state/paper_trader_weekend_state.json` + `journal/paper_trader_weekend.log` (WEEKEND).
- Sheet tabs per month: `CURRENT <Mon YYYY>`, `WEEKEND <Mon YYYY>` (created on the first closed trade of the month).
- Live config CURRENT: window 05:30-11:00 WIB, Mon-Thu, BB width >= 0.015, ATR exits TP 2.5 / SL 1.0, 8x, bet 50%, caps $120/$600. WEEKEND: Sat+Sun, BB width >= 0.05, volume >= 1.5x avg, break of 20-bar high/low + BB band, ATR 2.5/1.0, max 2 trades/day, SL cooldown. Full parameters: trading-brain STRATEGY.md.

## 3. Logic & rules
- CURRENT no longer uses the swing gate / SL cooldown (ATR-D features; gate on both sides backtests PF 2.49 on 72 trades but is not enabled).
- WEEKEND trades only when ALL conditions hold, so many weekends are empty by design (about 0.5 trades per weekend). The edge depends on the volume filter (without it the edge was only the Sep-Oct 2025 ASTER launch weeks).
- A quiet day is normal: on 2026-10-06 CURRENT saw one close outside the BB (07:30) but BB width was 0.0141 < 0.015, so it was filtered.
- Known issues: CURRENT's OPENED/CLOSED log banners are stdout block-buffered (lag hours; use the state file or sheet); Bybit rate-limit errors (about 55 logged) are benign but both traders share the VPS IP; WEEKEND evaluates entries only on the latest completed candle, so a fetch outage of 15+ min could skip a signal (fix: evaluate all unprocessed candles).

## 4. Outputs
- Journal recap `journal/trading/recaps/YYYY-MM-DD.md` (per "Trading Daily Recap" checklist in CLAUDE.md).

## 5. Data storage
- Recap journal: `journal/trading/recaps/` (tracked).
- trading-brain repo (VPS + local): `STRATEGY.md`, `DEPLOYMENT.md`, `BACKTEST_LOG.md`, state + logs.

## 6. Acceptance criteria
- TODO: fill.

## 7. Change Requests
- `2026-09-25 | remove SUI, fold into ASTER, bigger amount | SUI leg dropped; ASTER capital $2017.89 anchored in service; caps scaled to $120/$600; CURRENT untouched | changed: trading-brain code + .md docs (see recaps/2026-09-25.md)`
- `2026-09-30 | recap journal every time a trading summary is asked | daily recap convention established | changed: CLAUDE.md checklist + journal/trading/recaps/*`
- `2026-10-01 | analyze recaps vs strategy, extend time, avoid -7 losses, more leverage | backtest study; ATR-E filtered variant built as a 3rd paper leg | changed: trading-brain (paper_trader_atrd --variant E, docs, scripts/atr_variants) - superseded 10-02`
- `2026-10-02 | delete the other legs, focus on CURRENT, how to improve | ATR-D/ATR-E + summary_updater removed (services, code, state, logs); sheet data archived to journal/trading/archive/ (gitignored); CURRENT upgrade study | changed: trading-brain 16858b1 (+docs); sheet tabs deleted by owner`
- `2026-10-02 | apply all upgrades to CURRENT | BB width 0.002->0.015, fixed TP/SL -> ATR 2.5/1.0, 4x->8x, caps $30/$150->$60/$300 | changed: trading-brain 51ea9c6 (paper_trader.py main(), ATR support)`
- `2026-10-02 | keep window 05:30-11:00, test bigger bet | window 05:30-09:00 -> 05:30-11:00; bet-size backtest logged | changed: trading-brain e90c4c7`
- `2026-10-02 | CURRENT paper: try 50% bet | position_size_pct 25->50 (4x exposure), caps -> $120/$600 | changed: trading-brain 33f7058 (backtest: PF 1.40, about $19/day, 6.4% chance of -50%)`
- `2026-10-02 | find a weekend strategy (trade only when all parameters pass) | WEEKEND momentum paper leg: Sat+Sun, BB width >= 0.05, volume >= 1.5x, 20-bar breakout + BB band, ATR 2.5/1.0, $1000, 25% bet x 8x; launch-week dependence found and fixed with the volume filter | changed: trading-brain 422edaf (paper_trader_weekend.py, paper-trader-weekend.service, scripts/weekend)`
- `2026-10-06 | why no trade record today | diagnosis only: CURRENT filtered by BB width (0.0141 < 0.015), WEEKEND no qualifying breakout 10-03/04; CURRENT did trade Mon 10-05 (SHORT TP +$41.77, balance $1094.11) | changed: none (docs + recaps)`
- TODO: candidate fixes - WEEKEND evaluate all unprocessed candles; CURRENT log flush; review both legs after 3-4 weeks (CURRENT) / 10-15 weekends (WEEKEND).
- `2026-10-06 | go live with real money on Bybit: $100 test for 1 day, then more; same CURRENT strategy | live-execution layer built (dry-run by default), NOT yet live: mirrors CURRENT paper entries with a market order + exchange TP/SL; sizing from real equity x 50% bet x 8x capped at $450 notional; LIVE_MODE off/dry/live in .env; kill switch journal/LIVE_STOP; daily loss cap 10%; repair-or-flatten if TP/SL missing; 24 unit tests; dry probe against real Bybit data OK; fees cut CURRENT backtest PF 1.53 -> 1.29 | changed: trading-brain 8201ead (broker/bybit.py, engine/live_executor.py, paper_trader hooks, tests, docs); VPS .env got LIVE_* = dry; Bybit key stored only in VPS .env (600)`
- TODO: owner go-ahead for the $6 plumbing test (real order + TP/SL + close), then LIVE_MODE=live; scale beyond $450 notional only after live matches paper over several trades; new section 'Live execution' belongs in section 3 above once live.

- `2026-10-06 | run the $6 plumbing test | real long 9 ASTER with TP/SL attached then closed: PASS, cost $0.0062; LIVE_MODE still dry | changed: none in code`
- `2026-10-06 | find where Aug-Sep losses came from and avoid them; follow-the-trend idea; combine "do not trade while trending, wait for a pause, trade any hour" with CURRENT | loss clusters = 12 of 17 Aug-Sep losers on 5 trend days; swing gate both sides (20 bars) lifts PF 1.21 -> 1.59 (setahun, costs 0.15%); follow-the-trend entries LOSE (PF 0.80, win rate drops 35% -> 27%); EMA200 filter HURTS (PF 0.89); extra pause filters (ER, bars since breakout) unstable; CURRENT now = gate both sides + window 00:00-23:59 Mon-Thu, everything else unchanged; 24h+gate backtest PF 1.33 (recent 7 months 1.18, thin edge); Aug+Sep at $12k notional: 05:30-11:00 +$756, 24h +$1,604 | changed: trading-brain 559aaf5/70cd187 (paper_trader main(), BACKTEST_LOG, STRATEGY, scripts/regime); live layer still LIVE_MODE=dry`

