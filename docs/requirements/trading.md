# Trading - Logic & Requirements (skeleton)

Bridge doc for the trading domain. **Strategy/backtest detail lives in the trading-brain repo** (`STRATEGY.md`, `DEPLOYMENT.md`, `BACKTEST_LOG.md`, branch `master`) - do not duplicate it here; this doc holds the index, the live config summary, and the second-brain-side change log. Daily recaps: `journal/trading/recaps/`. Operational reference skill: `.agent/skills/trading-sheets/SKILL.md` (kept in place).

## 1. Purpose & scope
- Paper-trading experiments (ASTERUSDT), two engines live: ATR-D (8x, swing gate + SL cooldown) and CURRENT (4x control). SUI leg removed 2026-09-25.
- Analysis and records live here (second-brain); data/logs live on the VPS in trading-brain.

## 2. Inputs / sources
- trading-brain repo on VPS (`/home/ubuntu/projects/trading-brain`), branch `master`.
- State: `journal/state/paper_trader_atrd_state.json`, `journal/paper_trader_atrd.log`, `journal/paper_trader.log`.
- Live config: ATR-D `--capital 2017.89`, daily caps $120/$600, 8x.

## 3. Logic & rules
- Swing gate: block SHORT when close >= 20-bar swing high (wick-based). Known gap: spike wicks inflate the high - candidate tightening (0.5x ATR buffer / close-based highs), backtest pending.
- SL-cooldown: ATR-D blocks same-direction re-entry after an SL for the day (CURRENT has no cooldown).
- Known issue: OPENED/CLOSED/COOLDOWN log banners are stdout block-buffered (lag hours); state file + periodic Cap lines are the reliable real-time source - flush=True fix pending.

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
- TODO: append pending decisions once executed (gate tightening, log flushing).