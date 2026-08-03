---
description: Start, manage, or complete a development task session with automatic activity tracking
argument-hint: "[start/status/complete/list, or describe the task]"
---

Development task session manager. You track every development task from start to finish.

## Behavior based on $ARGUMENTS:

- **No arguments or "status"**: Show current active session status via `python .agent/skills/dev-tracker/scripts/dev_tracker.py status`
- **"list"**: Show recent sessions via `python .agent/skills/dev-tracker/scripts/dev_tracker.py list`
- **"complete"**: Complete the active session. Ask for a summary if not provided, then run `dev_tracker.py complete --summary "..."`
- **"pause" / "resume"**: Pause or resume the active session
- **A task description or "start"**: Start a new task session (see below)

## Starting a new task

When starting a task, gather these from the user or context:

1. **Title** (required) — what is being done
2. **Source** — where did this task come from? (mattermost, trello, gmail, fathom, gitlab, manual)
3. **Source reference** — URL or ID of the original item (GitLab issue URL, Mattermost message link, etc.)
4. **Project** — which project does this belong to
5. **Repository** — the GitLab repo path
6. **Requester** — who asked for this
7. **Priority** — high, medium, or low

Then start it:
```bash
python .agent/skills/dev-tracker/scripts/dev_tracker.py start \
  --title "..." --source <source> --project "..." --repo "..." --requester "..." --priority <p>
```

## During development (IMPORTANT)

While a task session is active, you MUST:

1. **Log technical analysis** when you investigate the problem:
   ```bash
   python .agent/skills/dev-tracker/scripts/dev_tracker.py log --type analysis --note "..."
   ```

2. **Log bugs found** when you discover issues:
   ```bash
   python .agent/skills/dev-tracker/scripts/dev_tracker.py log --type bug --note "..."
   ```

3. **Log fixes** when you resolve something:
   ```bash
   python .agent/skills/dev-tracker/scripts/dev_tracker.py log --type fix --note "..."
   ```

4. **Log test results** after running tests:
   ```bash
   python .agent/skills/dev-tracker/scripts/dev_tracker.py log --type test --note "..."
   ```

5. **Log git commits** when committing:
   ```bash
   python .agent/skills/dev-tracker/scripts/dev_tracker.py log --type commit --note "<hash> - <message>"
   ```

6. **Log the branch name** when you create or switch branches:
   ```bash
   python .agent/skills/dev-tracker/scripts/dev_tracker.py log --type branch --note "feature/xyz"
   ```

File edits and commands are auto-logged by the PostToolUse hook. You only need to manually log the semantic events above.

## Completing a task

When the user says done, shipped, merged, or similar:

1. Ask for a brief summary if not obvious
2. Run: `python .agent/skills/dev-tracker/scripts/dev_tracker.py complete --summary "..."`
3. Show the generated report path
4. Offer to show the full report content

## Rules

- Only ONE task session can be active at a time
- If the user wants to switch tasks, complete or pause the current one first
- Always log analysis, bugs, fixes, tests, and commits during development
- The hook handles file edits and commands automatically — don't duplicate those

$ARGUMENTS
