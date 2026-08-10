# Personal Finance Brain

Financial analysis, forecasting, and cash flow management. Google Sheets is the source of truth. The Second Brain provides analysis and recommendations.

## Safety Rules

- NEVER execute bank transfers or financial transactions
- NEVER automatically borrow money
- NEVER treat uncertain income as guaranteed
- Financial WRITES require explicit user approval
- Clearly distinguish FACTS from ESTIMATES and SCENARIOS

## Usage

```bash
# Full financial analysis
python .agent/skills/personal-finance/scripts/finance_engine.py analyze

# Cash flow forecast
python .agent/skills/personal-finance/scripts/finance_engine.py forecast --days 30

# Answer a specific question
python .agent/skills/personal-finance/scripts/finance_engine.py ask --query "Can I afford to pay Vandi 5M?"

# Daily briefing section
python .agent/skills/personal-finance/scripts/finance_engine.py briefing

# Read current sheet data
python .agent/skills/personal-finance/scripts/finance_engine.py read --tab cash

# Record a transaction (requires approval)
python .agent/skills/personal-finance/scripts/finance_engine.py record --type income --amount 18000000 --source catalyze --note "August payment"
```

## Configuration

`.agent/workspaces/personal/finance.json` — sheet ID, income sources, scenarios, critical obligations.

## Architecture

Google Sheet (source of truth) → finance_engine.py → DeepSeek (analysis) → answer with calculations shown
