---
description: "Investment Analyst - real IDX quotes, fundamentals, and watchlist screening (Yahoo Finance, personal workspace)"
argument-hint: "<ticker(s) or screen=sector>"
---

Real-time IDX market data via the Investment Analyst skill (personal workspace).

1. To quote one or more tickers:
   `python .agent/skills/investment-analyst/scripts/investment_analyst.py quote --symbols BBCA,ASII`
2. To screen a sector (e.g. low PBV among banks):
   `python .agent/skills/investment-analyst/scripts/investment_analyst.py screen --sector bank --sort pbv`
3. To screen the whole watchlist by dividend yield:
   `python .agent/skills/investment-analyst/scripts/investment_analyst.py screen --sort dividend_yield --desc`

Stock Deep Dive (full 18-section report):
4. `python .agent/skills/investment-analyst/scripts/investment_analyst.py deep-dive BMRI --json`
   - builds the structured data package (market snapshot, 3-5yr annual trend
     with YoY, quarterly when available, peer comparison, valuation, ownership,
     missing fields, existing documents, sources, market-vs-reporting timestamps)
5. `python .agent/skills/investment-analyst/scripts/investment_analyst.py deep-dive BMRI`
   - human digest + explicit missing-data document requests
   In the dashboard chat, `/deepdive BMRI` runs this pipeline and then ONE chat
   call writes the full report from the carrier + the fixed template.

Presentation rules for the answer:
- Separate RAW facts (price, ROE, book value, dividend yield, shares outstanding, float shares, institutional %) from CALCULATED metrics (PBV = price / book value, float % = float / outstanding) and your interpretation - label the layers.
- Report floatShares as a value, never as "shares sold to the public vs held back"; state that interpretation belongs to the reader.
- Institutional ownership is an indicator, not a "whale signal".
- Never invent a number the script did not return; list any tickers whose data is unavailable.
- For a deep dive: separate the market snapshot (live) from the financial reporting period; keep REPORTED vs CALCULATED (with formulas) vs INTERPRETED layers apart; mark the conclusion PROVISIONAL when key data (e.g. quarterly statements, bank NPL/CAR/NIM) is missing; request the specific IDX/annual-report/IR document that fills each gap; close with "not investment advice, no buy/sell".
- Frame fundamentals through the user's personal knowledge notes (search the knowledge store for PBV/ROE lessons) when relevant.

$ARGUMENTS