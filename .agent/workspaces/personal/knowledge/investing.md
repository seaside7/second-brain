# Investing Knowledge (personal)
<!-- source: personal/investing -->

### Stock fundamentals - screen pillars (PBV, ROE, dividend)
- **Date**: 2026-08-30
- **Tags**: #fundamental, #stock, #pbv, #roe, #dividend, #saham, #invest
- **Source**: user_note
- **Confidence**: medium

Fundamental analysis lesson (learned 30 Aug 2026):
- **PBV (Price to Book Value)** = price / book value per share. A low PBV relative to its industry may mean the stock is cheap relative to assets - but compare within the same industry (banks vs banks, miners vs miners). PBV below ~1 can signal undervaluation OR that the market distrusts the book - never read it alone.
- **ROE (Return on Equity)** = net profit / equity. Higher ROE means the company earns more per rupiah of shareholder equity - a key profitability screen. Combine with PBV: high ROE + low PBV is the classic value combo.
- **EPS (Earnings Per Share)** = net profit / number of shares. Trailing EPS underpins the P/E ratio.
- **Dividend yield** = dividend per share / price. Useful for income investing; check dividend per share from last financial year.
- **Shares structure**: shares outstanding = total shares; float shares = shares held by the public and available for trading. Float % = float / outstanding. A small float generally means less supply; do not over-translate numbers into a buy/sell verdict without checking the company's shareholder structure.
- Screening method: pick an industry, rank its stocks by PBV (lowest first), then filter by ROE and dividend yield, then validate on a full quote. Data is read-only and for analysis; this is not financial advice.

---

### Investment Analyst - what data it returns
- **Date**: 2026-08-30
- **Tags**: #investment-analyst, #skill, #yahoo, #idx
- **Source**: manual
- **Confidence**: high

The personal workspace has an Investment Analyst skill (`.agent/skills/investment-analyst/`). It fetches real IDX market data from Yahoo Finance public endpoints (no API key) and separates RAW facts (price, ROE, dividend yield, shares outstanding, float shares, institutional %) from CALCULATED metrics (PBV = price / book value, float % = float / outstanding, change %). Every quote carries ticker, company name, currency, market timestamp, source, and data timing. Requests are cached (~10 min) and paced; partial results list which tickers failed. Ask it anything: "which bank has the lowest PBV?", "what was last year's dividend for BBCA?", "BBCA vs ASII which is cheaper on PBV?". Use `/invest` in the chat, or type the ticker naturally and it auto-fetches.

---