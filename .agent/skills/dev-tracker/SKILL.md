# Dev Tracker Skill

Track development tasks from start to finish — source capture, activity logging, and report generation for a software engineer's workflow.

## Overview

A "dev session" is a tracked unit of work: one task, from the moment you pick it up to the moment you ship it. During the session, every file edit, command run, test result, and git commit is automatically logged. On completion, a development report is generated.

## Capabilities

- **start** — create a new task session (from any source: Mattermost, Trello, Gmail, Fathom, manual)
- **log** — append a development event (analysis, file change, command, test, bug, fix, commit)
- **status** — show the current active task and its activity log
- **complete** — close a task session and generate a development report
- **pause** / **resume** — pause/resume time tracking (lunch, context switch)
- **list** — show all tasks (active, completed, filtered by project/date)
- **report** — regenerate or view the development report for a completed task

## Usage

```bash
# Start a new task session
python .agent/skills/dev-tracker/scripts/dev_tracker.py start \
  --title "Fix authentication bug in API gateway" \
  --source mattermost \
  --project "ABNJ" \
  --repo "catalyzecommunications/abnj-bet" \
  --requester "Andi" \
  --priority high

# Start from a GitLab issue directly
python .agent/skills/dev-tracker/scripts/dev_tracker.py start \
  --title "Implement user export endpoint" \
  --source gitlab \
  --source-ref "https://gitlab.com/catalyzecommunications/abnj-bet/-/issues/42" \
  --project "ABNJ" \
  --repo "catalyzecommunications/abnj-bet" \
  --priority medium

# Log development events (auto-logged by the PostToolUse hook, but can also be manual)
python .agent/skills/dev-tracker/scripts/dev_tracker.py log --type analysis --note "Root cause: JWT token not refreshed on 401 retry"
python .agent/skills/dev-tracker/scripts/dev_tracker.py log --type file --note "Modified src/auth/middleware.ts"
python .agent/skills/dev-tracker/scripts/dev_tracker.py log --type command --note "npm run test -- --grep auth"
python .agent/skills/dev-tracker/scripts/dev_tracker.py log --type test --note "PASS: 12/12 auth tests passing"
python .agent/skills/dev-tracker/scripts/dev_tracker.py log --type bug --note "Race condition when two refresh calls fire simultaneously"
python .agent/skills/dev-tracker/scripts/dev_tracker.py log --type fix --note "Added mutex lock around token refresh"
python .agent/skills/dev-tracker/scripts/dev_tracker.py log --type commit --note "abc1234 - fix: add mutex to token refresh"
python .agent/skills/dev-tracker/scripts/dev_tracker.py log --type branch --note "fix/auth-refresh-race"

# Check current task status
python .agent/skills/dev-tracker/scripts/dev_tracker.py status

# Pause/resume (lunch break, context switch)
python .agent/skills/dev-tracker/scripts/dev_tracker.py pause
python .agent/skills/dev-tracker/scripts/dev_tracker.py resume

# Complete the task (generates report)
python .agent/skills/dev-tracker/scripts/dev_tracker.py complete --summary "Fixed JWT refresh race condition"

# List tasks
python .agent/skills/dev-tracker/scripts/dev_tracker.py list
python .agent/skills/dev-tracker/scripts/dev_tracker.py list --status completed --project ABNJ
python .agent/skills/dev-tracker/scripts/dev_tracker.py list --since 2026-07-01

# View/regenerate a report
python .agent/skills/dev-tracker/scripts/dev_tracker.py report DEV-0001
```

## State

- State file: `journal/state/dev_sessions.json`
- Reports: `journal/dev_reports/DEV-NNNN_<slug>.md`
- IDs: monotonic `DEV-0001`, `DEV-0002`, ...

## Session Schema

```json
{
  "id": "DEV-0001",
  "title": "Fix authentication bug in API gateway",
  "source": "mattermost|trello|gmail|fathom|gitlab|manual",
  "source_ref": "URL or ID of the source item",
  "project": "ABNJ",
  "repo": "catalyzecommunications/abnj-bet",
  "requester": "Andi",
  "priority": "high|medium|low",
  "status": "active|paused|completed",
  "branch": "fix/auth-refresh-race",
  "started_at": "2026-08-03T14:30:00+07:00",
  "paused_at": null,
  "completed_at": null,
  "total_paused_seconds": 0,
  "working_duration_minutes": null,
  "summary": null,
  "events": [
    {"ts": "...", "type": "analysis|file|command|test|bug|fix|commit|branch|note", "note": "..."}
  ],
  "report_path": null
}
```

## Integration

- **PostToolUse hook** (`.claude/hooks/dev_activity_log.py`): auto-logs file edits and bash commands into the active session
- **`/dev` command**: the conversational entry point for starting/managing sessions
- **`dev-report` agent**: generates the final development report from the event log
- **Downstream**: reports feed into daily updates, weekly reports, and timesheets

## Event Types

| Type | What it captures |
|---|---|
| `analysis` | Technical analysis, root cause investigation |
| `file` | File created, modified, or deleted |
| `command` | Shell command executed |
| `test` | Test run results |
| `bug` | Bug discovered during development |
| `fix` | Bug fix applied |
| `commit` | Git commit made |
| `branch` | Branch created or switched |
| `note` | Free-form development note |
