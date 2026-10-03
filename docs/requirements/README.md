# Requirements & Logic Docs

Single source of truth for what each menu of this system does and how. One `*.md` per menu, following `_template.md`, each with its own append-only `## Change Requests` log.

## Standing rule

> **Any change request in any menu requires a doc update before the task is done**: update `docs/requirements/<menu>.md` (spec sections if logic/data changed + append a Change Request entry with date, the request, the decision, and what changed/files), then commit + push `main` and pull on the VPS (`ssh ubuntu@43.157.241.209 "cd /home/ubuntu/projects/second-brain && git pull"`).

No exceptions. A request that lands without its doc update is not finished. This is enforced via the "Requirement Logs" checklist in `CLAUDE.md`.

Rules that hold across all menus:

- One menu = one file here. Skill `SKILL.md` files are operational runtime specs and stay in `.agent/skills/*/`; these docs are the human-facing requirements and link to them.
- Docs that belong to another repo (e.g. trading-brain `STRATEGY.md`/`BACKTEST_LOG.md`) are bridged/indexed here, never duplicated.
- Daily recaps (e.g. trading) are chronologies; requirement docs are the specs where recap decisions get logged as change requests.

## Menu index

| Menu | Doc | Status | Related runtime / refs |
|---|---|---|---|
| Transactions | [transactions.md](transactions.md) | Complete (2026-10-01) | `.agent/skills/transactions/SKILL.md`, `personal-finance`, `.agent/workspaces/personal/state/transactions.db`, finance.json |
| News | [news.md](news.md) | Skeleton | `.agent/skills/news-intelligence/SKILL.md`, `journal/news_briefings/` |
| Trading | [trading.md](trading.md) | Skeleton w/ seed change log | trading-brain `STRATEGY.md`/`DEPLOYMENT.md`/`BACKTEST_LOG.md`, `journal/trading/recaps/` |
| Chat / AI Circle | [chat.md](chat.md) | Skeleton | `journal/state/chats.json` |
| Coding / tasks | - | Not created yet | `dashboard/coding_agent.py`, `tasks_agent.py`, `journal/dev_reports/` |
| Meetings | - | Not created yet | `journal/meetings/`, `journal/state/meeting_decisions.json` |
| Investments | - | Not created yet | `.agent/skills/investment-analyst/SKILL.md`, `journal/state/investment_cache.json` |
| Inbox | - | Not created yet | `.agent/skills/inbox-hub/SKILL.md` |
| Approvals | - | Not created yet | `.agent/skills/approval-queue/SKILL.md`, `journal/state/approval_queue.json` |
| Memory / recall | - | Not created yet | `.agent/skills/memory-recall/SKILL.md`, `knowledge-store` |
| Model router | - | Not created yet | `dashboard/server.py` `/api/models*` |

Create a doc for a new menu by copying `_template.md`, then request it to be added to this table.

## Backlog / open requirement work

- `transactions.md` Appendix 1: Rekap spreadsheet (shared 2026-10-01) - under study only, no action taken until approved.
- `trading.md`: gate tightening backtest + ATR-D log flushing fix (pending decisions, will be logged when executed).