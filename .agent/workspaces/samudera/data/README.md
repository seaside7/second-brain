# Samudera data drop folder

Place actual Samudera data exports here **after you join (2026-08-18+)** so the
Data/BI agent can use them. Nothing in this folder is assumed to exist before
then - if a file is not here, the agent reports the data as unavailable.

## What to drop

- Read-only exports only (CSV / XLSX / JSON / PDF reports). Never place
  credentials, private keys, or anything that must not be read by the AI.
- Prefer one file per domain so the agent can auto-detect it by filename:

| Domain | Filename hint (include in the name) |
|---|---|
| fleet/ops | `fleet_`, `vessels_`, `ops_`, `terminals_` |
| finance/budget | `finance_`, `budget_`, `p&l_`, `capex_` |
| HR | `hr_`, `people_`, `headcount_` |
| procurement/vendors | `procurement_`, `vendors_`, `suppliers_` |
| KPIs/metrics | `kpi_`, `metrics_`, `scorecard_` |
| BI exports | `bi_`, `dashboard_` |

Example: `fleet_vessels_2026-08.csv`

## How the agent uses it

- `data-agent availability` lists this folder and what is usable today.
- `data-agent query --question "fleet size"` answers only if a matching file
  actually exists here; otherwise it reports "data unavailable" with the reason
  and what to provide. It never fabricates numbers.
- Research and the executive orchestrator consult the same registry
  (`.agent/scripts/availability_registry.py`).
