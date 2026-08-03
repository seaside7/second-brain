# Timesheet Writer Skill

Append time entries to the Catalyze timesheet Google Sheet. Only writes to columns C (Projects) and D (Description) on the sheet tab matching the current month name.

## Sheet Details

- **Sheet ID**: `1WKRVlUzG1RY0nL-Eb-ahPn4neigBJ4iSsjAOT7C-dqA`
- **Tab naming**: Current month in English (e.g. `July`, `August`)
- **Columns written**: C (Projects), D (Description)
- **Columns NOT written**: A (Date), B (Time), E (Time H/M), F (Time Decimal) - owner fills these manually

## Valid Projects

- ERIA - SEAMAP
- Brock University - OEI
- YKAN - KHG EXPLORER + MRV (Phase 1))
- WWF US - ELINOR
- WWF US - Conservation Navigator
- WWF US - NDH (Phase 2))
- ERIA - RKCMPD
- ERIA - RKCMPD BLAB
- UN - PNOC
- BFA - Bioplastic Feedstock Alliance
- LFL - Landscape Finance Lab
- CATALYZE - [Internal] General Operational
- CATALYZE - Website CATALYZE
- Data Platform
- EQI
- KMP
- ABNJ
- Katingan
- Viriya

## Usage

```bash
# Append a single entry
python .agent/skills/timesheet-writer/scripts/timesheet_writer.py append \
  --project "ABNJ" \
  --description "Fix authentication bug in API gateway"

# Append multiple entries from a dev report
python .agent/skills/timesheet-writer/scripts/timesheet_writer.py append \
  --project "ABNJ" \
  --description "Setting up CI/CD pipeline for staging environment"

# Append from a completed dev session
python .agent/skills/timesheet-writer/scripts/timesheet_writer.py from-session DEV-0001

# List valid project names
python .agent/skills/timesheet-writer/scripts/timesheet_writer.py projects

# Preview what would be written (dry run)
python .agent/skills/timesheet-writer/scripts/timesheet_writer.py append \
  --project "ABNJ" --description "..." --dry-run
```

## Integration with Dev Tracker

When a dev session is completed, the `/dev complete` flow can optionally write the entry to the timesheet:
```bash
python .agent/skills/timesheet-writer/scripts/timesheet_writer.py from-session DEV-0001
```

This reads the session's project and summary, maps it to a valid project name, and appends a row.

## Auth

Uses the same Google Drive/Sheets OAuth token at `.agent/skills/work-drive-connector/token.json`.
The Drive scope (`https://www.googleapis.com/auth/drive`) includes Sheets write access.
