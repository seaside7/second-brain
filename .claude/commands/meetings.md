---
description: Sync and view meetings from Fathom - extract action items, decisions, and follow-ups
argument-hint: "[sync|list|tasks|or a meeting topic to search]"
---

Meeting Intelligence Engine. Fetches meetings, extracts tasks and decisions.

## Behavior based on $ARGUMENTS:

### "sync" or no arguments — Fetch and process new meetings

```bash
python .agent/skills/meeting-intelligence/scripts/meeting_engine.py sync
```

This:
1. Connects to Fathom (using the active workspace's API key)
2. Fetches recent meetings with transcripts + summaries + action items
3. Stores immutable meeting records in `journal/meetings/<workspace>/YYYY/MM/`
4. Extracts action items into the Universal Task Store (`journal/state/tasks.json`)
5. Extracts decisions into `journal/state/meeting_decisions.json`
6. Reports what was found

After syncing, summarize:
- How many meetings were processed
- Key action items extracted (show title + assignee)
- Any decisions made

### "list" — Show stored meetings

```bash
python .agent/skills/meeting-intelligence/scripts/meeting_engine.py list
```

### "tasks" — Show tasks extracted from meetings

```bash
python .agent/skills/meeting-intelligence/scripts/meeting_engine.py tasks
```

### A meeting topic (search) — Find a specific meeting

If the user types something like "meetings about ABNJ" or "what did we discuss about SSO":
1. Run `list` and scan titles
2. If found, read the stored meeting JSON and summarize the transcript/decisions

### Ingest manual notes

If the user provides meeting notes directly or points to a file:
```bash
python .agent/skills/meeting-intelligence/scripts/meeting_engine.py ingest \
  --title "Meeting Title" --file <path> --project "ProjectName" --attendees "A,B,C"
```

## Rules

- This engine EXTRACTS and STORES. It does NOT prioritize.
- Show extracted tasks but do not recommend which to do first (that's /inbox's job)
- If Fathom is not configured for the active workspace, tell the user to add their API key:
  `.agent/workspaces/<workspace>/fathom.env` with `FATHOM_API_KEY=...`
- Never modify stored meeting records (they are immutable)
- Re-running sync on the same meetings is safe (deduplicated)

$ARGUMENTS
