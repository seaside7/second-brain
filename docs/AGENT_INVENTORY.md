# Agent Inventory

All the agents and automation that support Said's work, one place. This is a
living reference - update it whenever a subagent, skill, command, workflow, or
hook is added or removed.

Last updated: 2026-08-30

---

## 1. Subagents (`.claude/agents/`)

Delegate mechanical work via the Task tool; model matches the WORK, not the session tier.

| Agent | Model / effort | Job |
|---|---|---|
| `harvester` | haiku / low | Bulk read-and-extract, raw facts, no synthesis |
| `meeting-harvester` | haiku / low | Fathom recording -> raw meeting facts |
| `draft` | sonnet / medium | First-pass writing from a clear source |
| `draft-reviewer` | haiku / medium | Pre-send PASS/ISSUES checklist |
| `dev-report` | sonnet / medium | Polished dev session reports |
| `report-auditor` | sonnet / high | Rubric audit of daily/weekly reports |
| `hyperplan-critic` | sonnet / high | Single-lens hostile critic |

Routing table lives in `CLAUDE.md` (Subagents section) and `docs/harness_reference.md`.

---

## 2. Architectural agents (dashboard Agents tab, `AGENTS_ARCHITECTURE`)

Payload served by `/api/agents-map`; node detail by `/api/agents-skill`.

### Active (skill implemented, registered)
| Node | Level | Skill |
|---|---|---|
| Orchestrator (🎯) | 0 | `executive-orchestrator` |
| Executive PM (📋) | 1 | `executive-pm` |
| Transformation Strategy (🗺️) | 1 | `transformation-strategy` |
| Transformation Research (🔎) | 1 | `transformation-research` |
| Data / BI (📊) | 1 | `data-agent` |
| Governance & Standards (🛡️) | 2 | `approval-queue` |
| Drive Indexer (📇) | 2 | `drive-indexer` |
| Drive Search (🔍) | 2 | `drive-search` |
| Embedding Index (🧠) | 2 | `embedding-index` |
| Memory Recall (🕸️) | 2 | `memory-recall` |

### Planned (concept only, no skill yet)
Process Excellence, Enterprise Integration, Business Case, Risk / Audit /
Security, Executive Advisor, Communication.

Join date `2026-08-18`. Status = active when skill is in `SKILL_REGISTRY` and
its script file exists; unavailable when the skill dir exists but is not
registered; planned when no skill exists. The Agents tab is Samudera-primary
(hidden on the combined dashboard via `SAMUDERA_ONLY_TABS`).

---

## 3. Orchestrator-registered skills (`SKILL_REGISTRY`, `.agent/scripts/orchestrator.py`)

| Name | Category | Description |
|---|---|---|
| dev-tracker | code_review | Start/log/complete dev task sessions |
| timesheet-writer | draft_boilerplate | Append timesheet entries to Google Sheets |
| knowledge-store | simple_lookup | Store and search long-term knowledge |
| meeting-intelligence | complex_synthesis | Extract tasks/decisions from transcripts |
| gitlab-connector | simple_lookup | Query GitLab: commits, MRs, pipelines, issues |
| gmail-connector | simple_lookup | Read/search/send Gmail |
| fathom-connector | simple_lookup | Sync Fathom meeting recordings |
| google-calendar-connector | simple_lookup | Read/manage Google Calendar |
| mattermost-connector | simple_lookup | Read/send Mattermost messages |
| trello-connector | simple_lookup | Read Trello cards and boards |
| document-intelligence | complex_synthesis | Search/read/ask about Drive documents |
| personal-finance | complex_synthesis | Financial analysis, forecasting, cash flow |
| executive-pm | simple_lookup | Workspace-scoped executive digest |
| approval-queue | simple_lookup | Human-approval gate for external actions |
| executive-orchestrator | complex_synthesis | Samudera executive router + synthesis |
| data-agent | simple_lookup | Read-only Data/BI answers from real sources |
| transformation-research | complex_synthesis | Grounded Samudera research (news + archive) |
| transformation-strategy | complex_synthesis | Samudera DT strategy framing |
| drive-indexer | simple_lookup | Index Samudera Drive tree to local JSON |
| drive-search | simple_lookup | Search local Drive index + read files |
| memory-recall | simple_lookup | Unified memory recall (FAISS + Drive + state) |
| embedding-index | simple_lookup | Build/query FAISS embedding index |
| investment-analyst (NEW) | trading_analysis | IDX stock data; quote/screen/watchlist + Stock Deep Dive (`deep-dive`, `/deepdive`) |

### All skill dirs (`.agent/skills/`)
80 dirs, 78 with a SKILL.md. Beyond SKILL_REGISTRY these include connectors
(slack, whatsapp, gmail, google-calendar, google-drive, figma, ga4, metabase,
mixpanel, clickup, jira, google-ads), content builders (prd-pipeline,
user-story-writer, marketplace-product-manager, diagram-gen, make-pdf),
inbox/reply engines (inbox-hub, reply-queue, approval-queue, waiting-watchdog,
commitment-ledger, decision-log), news-intelligence, no-emdash, no-title-change,
invoice-generator, dashboard-updater, harness-health, meetbot, and others.
SKILL.md files are the single source of truth; the dashboard Agents editor
writes back to them.

---

## 4. Slash commands (`.claude/commands/`)

branch, capture-note, connect-tools, daily-update, dev, evening-update,
follow-ups, glm, hyperplan, inbox-sweep, **invest** (NEW), learn, meeting-notes,
meeting-prep, meetings, mom, morning-update, organize-inbox, prd, recall, recap,
remember, search, setup, slack-draft, sync-fathom, timesheet, ulw,
update-harness, weekly-planning, weekly-reflect, weekly-report, workspace.

Dashboard chat slash commands (server `_run_slash_command`): `/focus`, `/risk`,
`/brief`, `/approvals`, `/orchestrate`, `/invoice`, **`/invest`** (NEW),
**`/watchlist`** (NEW), **`/deepdive`** (NEW, server-internal before the generic
slash handler; `/deepdive --list`), `/help`.

---

## 5. Workflows (`.agent/workflows/`)

daily-update, evening-update, manage-action-items, morning-update,
organize-inbox, remote-listener, seo-audit-gogogo, sync-fathom,
upload_to_drive, weekly-planning (+ `inbox_sweep.py`).

---

## 6. Hooks (`.claude/hooks/`)

dashboard_context, dev_activity_log, drive_verify, emdash_guard, glm_mode,
routing_mode, session_git_sync, slack_send_guard, upstream_check, wib_clock
(each has a `.py` + `.sh` pair).

---

## 7. Workspaces / personas (`.agent/workspaces/workspaces.json`)

| Workspace | Role | Mode | Tools |
|---|---|---|---|
| catalyze | Backend Engineer / CMS | developer | Mattermost, Trello, GitLab/GitHub, Gmail |
| samudera (active) | Head of Digital Transformation | executive | Gmail, Drive, Calendar, Meet, Fathom |
| personal | Founder / Builder | builder | Finance, Drive (via shared), **Investment Analyst (quotes + Stock Deep Dive)** |

---

## 8. Model routing (`config/model_routing.json`)

Default `deepseek` (deepseek-chat), $50/mo cap. Categories: simple_lookup and
draft_boilerplate -> local; code_review, strategy_analysis, trading_analysis,
complex_synthesis -> deepseek. OpenAI tiers gpt-5.6-luna/terra/sol, fallback
chain deepseek-chat -> luna -> terra -> sol, escalation thresholds.

## 9. MCP servers (`config/mcporter.json`)

- `exa` -> `https://mcp.exa.ai/mcp` (web research; not yet configured with a key).

---

## 10. The Second Brain assistant

The dashboard chat is a GLOBAL assistant: memory recall searches every
workspace's knowledge + notes + drive index at once, with every snippet tagged
`[source: workspace]`, while conversation topics stay isolated per chat. The
workspace tag (Personal / Samudera) drives persona + suggestions only.