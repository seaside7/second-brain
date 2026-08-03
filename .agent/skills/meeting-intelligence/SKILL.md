# Meeting Intelligence Engine

Source-agnostic meeting processing pipeline. Fetches meetings from any source (Fathom, manual notes, future: Teams/Zoom), normalizes into a common MeetingRecord format, extracts actionable knowledge, and writes normalized tasks to the Universal Task Store.

## Responsibilities (ONLY these)

1. Fetch meeting transcripts and summaries from configured sources
2. Store immutable meeting records in `journal/meetings/<workspace>/YYYY/MM/`
3. Extract structured knowledge: action items, decisions, follow-ups, ideas
4. Normalize action items into Universal Task Schema → `journal/state/tasks.json`
5. Deduplicate: re-processing the same meeting never creates duplicate items

## Does NOT

- Prioritize tasks (Inbox Engine's job)
- Recommend what to work on (Inbox Engine's job)
- Send messages or modify external systems
- Delete or modify stored meeting records (they are immutable)

## Sources

| Source | Adapter | Status |
|---|---|---|
| Fathom | `fathom_adapter.py` | Active |
| Manual notes | `manual_adapter.py` | Active |
| Google Meet (native) | `gcal_adapter.py` | Future |
| Microsoft Teams | `teams_adapter.py` | Future |
| Zoom | `zoom_adapter.py` | Future |

## Usage

```bash
# Fetch and process today's meetings from Fathom (active workspace)
python .agent/skills/meeting-intelligence/scripts/meeting_engine.py sync

# Fetch meetings from a specific date range
python .agent/skills/meeting-intelligence/scripts/meeting_engine.py sync --since 2026-08-01

# Fetch from a specific workspace
python .agent/skills/meeting-intelligence/scripts/meeting_engine.py sync --workspace catalyze

# Process a manually provided transcript
python .agent/skills/meeting-intelligence/scripts/meeting_engine.py ingest \
  --title "ABNJ Sprint Planning" \
  --file ./notes/meeting_notes.md \
  --workspace catalyze \
  --project ABNJ \
  --attendees "Said,Andry,Carlito"

# List processed meetings
python .agent/skills/meeting-intelligence/scripts/meeting_engine.py list

# Show extracted tasks from meetings
python .agent/skills/meeting-intelligence/scripts/meeting_engine.py tasks

# Re-extract from an already stored meeting (idempotent)
python .agent/skills/meeting-intelligence/scripts/meeting_engine.py extract --meeting-id <id>
```

## Storage

```
journal/
├── meetings/
│   └── <workspace>/
│       └── YYYY/MM/
│           └── YYYY-MM-DD_<slug>.json    ← Immutable MeetingRecord
├── state/
│   ├── tasks.json                        ← Universal Task Store (shared with all sources)
│   ├── meeting_decisions.json            ← Extracted decisions
│   ├── meeting_followups.json            ← Follow-ups / waiting-on
│   └── meeting_ideas.json                ← Ideas / proposals
```

## Workspace-aware

Credentials are loaded from the active workspace:
- `.agent/workspaces/<workspace>/fathom.env` → `FATHOM_API_KEY`

Switching workspace (`/workspace switch`) changes which Fathom account is queried.

## Dedupe Strategy

```
dedupe_key = "meeting:{source}:{source_id}:{sha256(description)[:12]}"
```

Same meeting + same action item description = same key, always skipped on re-run.
