# Trading bot handoff (as of 2026-10-31)

Read this first on a new PC. No secrets here: tokens/keys live only in each machine's `.env` (VPS has the real ones).

## Repos and where things run
- Bot code: `trading-brain` (GitHub `seaside7/trading-brain`, branch `master`, last commit `b175386`). Local `D:\Coders\trading-brain`, VPS `/home/ubuntu/projects/trading-brain`.
- Notes/specs: this repo (`second-brain`, branch `main`). VPS copy `/home/ubuntu/projects/second-brain`.
- VPS `43.157.241.209`, user `ubuntu`. systemd services: `strategy-aster`, `strategy-uni`, `telegram-bot`, `paper-trader-weekend` (all active). Restart with `sudo systemctl restart <name>`, never nohup.
- Deploy flow: commit + push, then `ssh ubuntu@43.157.241.209 "cd /home/ubuntu/projects/trading-brain && git pull && sudo systemctl restart <services>"`. Ask the owner before every push/deploy.
- Tests: `python -m unittest discover -s tests -t .` (215 pass locally; 2 research tests cannot run on the VPS, no pandas, harmless).

## Strategy No.6 (automatic, live, real money, LIVE_MODE=live)
15m close outside BB(20,2) sample-std, BB width >= 1.5%, swing gate 20 bars, wait 4 candles after a crash candle, stop 2.5 ATR from the signal close, NO take-profit, ratchet (activate +2 ATR, lock +1 ATR, trail 1 ATR, software ratchet in `live_executor._ratchet`), one trade per WIB day. ASTERUSDT $6,000 all hours; UNIUSDT $4,000 Mon-Thu 05:30-11:00 WIB with margin auto-fit (`LIVE_AUTOFIT_SYMBOLS=UNIUSDT`). Details: trading-brain `STRATEGY.md`, `BACKTEST_LOG.md`.

## Owner's manual trades (Telegram bot)
Confirmation cards with Yes/Cancel. Protections: stop capped at `MANUAL_MAX_LOSS_USD` (60), IOC limit entry inside `MANUAL_FILL_BAND_PCT` (0.10%), stop re-anchored to the real fill, half size on extreme volatility / fresh crash (PAKSA RISIKO overrides), plain-word "aktifkan trailing stop" (manual trailing, distance 0.5-3 ATR).
New 2026-10-31: **profit give-back exit** for manual positions: market close when open profit falls to 50% of the peak, once the peak is >= $20 gross (`MANUAL_GIVEBACK_PCT`, `MANUAL_GIVEBACK_MIN_PEAK_USD`, 0 = off). Exit reason `profit giveback exit`.

## Telegram alerts
BB touch and "almost touch" (0.10%) alerts, `/bbalert all|strategy|near X|off` (state set to `all`). New 2026-10-31: Fed (FOMC) reminder at 09:00 WIB the day before and 1 hour before; dates in `src/notify/macro_events.py` (next: 9 Dec 2026 19:00 UTC = 10 Dec 02:00 WIB; add 2027 dates when published; CPI/jobs not included).

## Google Sheet journal
LIVE month tabs (21 columns incl. Duration, Duration (min), Exit Date) and `DAILY LIVE <Mon YYYY>` summary by close date (formulas read Exit Time column G). Scripts `scripts/sheet_add_duration.py`, `scripts/sheet_daily_summary.py`.

## Open items
1. Crash-inflated ATR starves the manual ratchet (open ASTER LONG 10,381 @0.67461 had ATR about 4x normal, trailing never activated). Proposed fix, owner has NOT approved: cap the ATR the manual ratchet uses (about 1.5x normal 24h ATR) and allow manual trailing distance down to about 0.15% of price (default min(1 ATR, 0.5% of price)).
2. Exit-reason label for plain manual market closes still says "trailing stop hit" (give-back exit has its own label). Owner has not answered.
3. Not committed in trading-brain: `scripts/research_2026-10-23/` (A/B/C and VWAP studies, results) and the BACKTEST_LOG.md correction note. Not approved to push.
4. 10-07 'paper' row (-$63.19) differs between Telegram monthly total and the daily summary tab. Owner undecided.
5. Watch: UNI auto-fit at its next signal; Bybit rejecting limit IOC entries (fallback `MANUAL_FILL_BAND_PCT=0` in VPS .env); first live use of the give-back exit.
6. Optional ideas offered, not built: pause the bot around Fed events; warn on manual cards inside the Fed window; backtest the sideways-candle exit.

## Owner preferences (also in memory files)
Short plain replies (casual Indonesian for chat), no fixed take-profit, raise the stop as profit grows, backtest reports Aug+Sep only. Every change is logged in `docs/requirements/trading.md` and the day's `journal/trading/recaps/YYYY-MM-DD.md`, then commit + push `main`, pull on the VPS. Stage only intended files; `debug_*.py` and `test_reports.py` in the repo root are untracked scratch files, do not commit.
Lessons: 10-30 crash losses (-$80/-$70) came from fills worse than the card price and stop slippage; 10-30 22:44 trade was +$30 at the peak then lost $49 with no trailing, which led to the give-back exit.
