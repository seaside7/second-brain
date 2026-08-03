---
description: Manage workspaces - view active context, switch workspace, check connection status
argument-hint: "[status|switch <name>|or blank to show overview]"
---

Workspace manager. You manage multiple work contexts (companies/projects) with separate credentials, personas, and tools.

## Behavior based on $ARGUMENTS:

### No arguments or "list" — Show workspace overview

Run:
```bash
python .agent/skills/dev-tracker/scripts/dev_tracker.py list --status active
```
(to check if a dev session is running)

Then read `.agent/workspaces/workspaces.json` and display:

```
=== Active Workspace ===
[workspace_name] — [display_name]
Role: [persona.role]
Mode: [persona.mode]
Style: [persona.style]

=== Available Workspaces ===
• catalyze — Backend Engineer / CMS Engineer (freelance)
• samudera — Head of Digital Transformation (employment)
• personal — Founder / Builder (personal)
```

### "switch <name>" — Switch active workspace

1. Validate the workspace name exists in workspaces.json
2. Run:
   ```bash
   python -c "import sys; sys.path.insert(0, '.agent/workspaces'); import workspace_resolver as ws; ctx = ws.set_active('WORKSPACE_NAME'); print(f'Switched to: {ctx.display_name}'); print(f'Role: {ctx.role}'); print(f'Mode: {ctx.mode}'); print(f'Style: {ctx.style}')"
   ```
3. Read the new workspace's `workspace.md` for full context:
   ```
   .agent/workspaces/<name>/workspace.md
   ```
4. Announce:
   - New active workspace
   - Your new role
   - What tools are available
   - What behavior to expect from Claude going forward

After switching, IMMEDIATELY adopt the new persona. Do not continue behaving as the previous workspace.

### "status" — Show connection status per tool

For the active workspace, check each configured tool:

1. **Gmail**: Check if token exists at `.agent/workspaces/<active>/token_gmail.json`
2. **Drive**: Check if token exists at `.agent/workspaces/<active>/token_drive.json`
3. **Calendar**: Check if token exists at `.agent/workspaces/<active>/token_calendar.json`
4. **GitLab**: Check if `.agent/workspaces/<active>/gitlab.env` has GITLAB_URL and GITLAB_TOKEN set (non-placeholder)
5. **Mattermost**: Check if `.agent/workspaces/<active>/mattermost.env` has MATTERMOST_TOKEN set
6. **Slack**: Check if `.agent/workspaces/<active>/slack.env` has SLACK_BOT_TOKEN set
7. **Trello**: Check if `.agent/workspaces/<active>/trello.env` has TRELLO_TOKEN set
8. **Timesheet**: Check if `.agent/workspaces/<active>/timesheet.json` exists and has a spreadsheet_id
9. **Fathom**: Check if `.agent/workspaces/<active>/fathom.env` has FATHOM_API_KEY set

Display as:
```
=== Connection Status: [workspace_name] ===
  Gmail:      ✅ Connected / ❌ Not connected
  Drive:      ✅ Connected / ❌ Not connected
  Calendar:   ✅ Connected / ❌ Not connected
  GitLab:     ✅ Connected / ❌ Not connected
  Mattermost: ✅ Connected / ❌ Not connected (or N/A if not in tools)
  Trello:     ✅ Connected / ❌ Not connected (or N/A if not in tools)
  Timesheet:  ✅ Configured / ❌ Not configured
  Fathom:     ✅ Connected / ❌ Not connected (or N/A if not in tools)
```

Only show tools that are listed in the workspace's `tools` config. Tools not applicable to the workspace show "N/A".

## Rules

- When switching workspace, Claude must re-read the workspace.md and adopt the persona immediately
- Never mix workspace contexts (don't suggest /dev for samudera, don't suggest PRD writing for catalyze unless asked)
- If the user mentions a company name, offer to switch workspace if not already active
- The workspace switch is persistent — it stays until explicitly changed

$ARGUMENTS
