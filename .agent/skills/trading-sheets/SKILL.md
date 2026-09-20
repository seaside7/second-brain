---
name: trading-sheets
description: Read (never write) the Trading Brain's daily trade journal Google Sheet ("Daily Trade Journal", ID 1TVyrR6-bg4fDrcljInk_cWi0h8j_zD5AePRmbwz_rmo) so the second-brain can learn and analyze live paper-trade records. Use when asked about trading-brain trades, monthly strategy comparison (CURRENT vs ATR_D), balances, win rates, or to refresh a journal snapshot. Read-only by design.
---

# trading-sheets

Read-only connector to the Trading Brain journal sheet. The VPS trading-brain
writes trades; this side only reads + local snapshot into the personal workspace
state dir.

## Invariants
- READ ONLY. Never issue write/clear/update calls against the spreadsheet.
- Auth uses the personal Drive token `.agent/workspaces/personal/token_drive.json`
  (scope `drive`), NOT a new browser OAuth. Falls back to
  `TRADING_SHEET_ID` / `GOOGLE_TOKEN_PATH` env overrides.
- Snapshots and `seen.json` land in `.agent/workspaces/personal/state/trading_sheets/`
  (gitignored). Never print token contents.

## Usage
```
python scripts/sheets_reader.py --status
python scripts/sheets_reader.py --dump "ATR_D Sep 2026" --rows 1200
python scripts/sheets_reader.py --dump-all
python scripts/trading_brain.py learn          # diff + LLM summary (no-op when nothing new)
python scripts/trading_brain.py learn --force  # re-summarize regardless
python scripts/trading_brain.py status
python scripts/trading_brain.py history
```

The dashboard has a 📈 Trading tab (dashboard/trading_api.py +
dashboard/public/tab-trading.js) that serves the learned state via
GET /api/trading/overview and re-learns via POST /api/trading/learn. The
server also runs a 6h in-process scheduler (no arming without the personal
Drive token), and `trading-brain` is registered in JOB_RUN_MAP for crontab /
manual run-job triggering.

## Fast facts (learned, updated 2026-09-20, see knowledge store: "Trading Brain - journal sheet map")
- Tabs per month: `CURRENT {Mon YYYY}` (legacy ASTER fixed TP/SL),
  `ATR_D {Mon YYYY}` (active ATR14 exits, 2 pairs), `SUMMARY {Mon YYYY}`
  (python-computed comparison, updated manually - no cron).
- Columns A:R; dates/times are Excel serials; balances are per-symbol chains.
- Summary metrics + winner direction: Total Trades(higher), Win Rate%(higher),
  Profit Factor(higher), Net Profit$(higher), Avg Win$(higher), Avg Loss$(lower),
  Max Drawdown$(lower, running-PnL peak-to-trough), Expectancy(higher).
- Strategy values: `BB20_fixed_tp_sl` (TP 1.5%/SL 0.75%, PnL% flat +6/-3 on 4x),
  `BB20_atr_dynamic` (TP 2.0x ATR / SL 1.0x ATR, variable PnL%).
- Sep 2026 to date: ATR_D 19 trades, WR 57.9%, PF 3.26, net +$77.25;
  CURRENT 8 trades, WR 37.5%, PF 1.19, net +$7.16.