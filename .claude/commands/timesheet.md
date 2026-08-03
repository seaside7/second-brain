---
description: Pull today's GitLab commits and write them to the timesheet Google Sheet
argument-hint: "[optional: 'dry-run' to preview, or a date like '2026-08-02']"
---

End-of-day timesheet sync: pull your commits from GitLab and write them to the timesheet.

## Steps

1. **Fetch today's commits from GitLab**:
   ```bash
   python .agent/skills/gitlab-connector/scripts/gitlab_client.py --action my_commits_today --json
   ```

2. **Map each project to a valid timesheet project name**. Use this mapping (GitLab path → Timesheet name):
   - `catalyzecommunications/abnj-*` → `ABNJ`
   - `catalyzecommunications/sea-map-*` → `ERIA - SEAMAP`
   - `catalyzecommunications/ykan-*` → `YKAN - KHG EXPLORER + MRV (Phase 1))`
   - `catalyzecommunications/equality-ocean-index*` or `catalyzecommunications/eqi-*` → `EQI`
   - `catalyzecommunications/katingan-*` → `Katingan`
   - `catalyzecommunications/wcs-mermaid*` → `KMP`
   - `catalyzecommunications/un-pnoc-*` → `UN - PNOC`
   - `catalyzecommunications/bioplastic-*` or `bfa-*` → `BFA - Bioplastic Feedstock Alliance`
   - `catalyzecommunications/landscape-finance-*` or `lfl-*` → `LFL - Landscape Finance Lab`
   - `catalyzecommunications/wwf-*` → check if it's ELINOR, Conservation Navigator, or NDH
   - `catalyzecommunications/eria-*` or `catalyzecommunications/viriya-*` → check specific project
   - `catalyzecommunications/catalyze*` or `catalyzecommunications/cms-*` → `CATALYZE - Website CATALYZE`
   - `catalyzecommunications/data-*` → `Data Platform`
   - `catalyzecommunications/viriya-*` → `Viriya`
   - Anything else → ask me which project to use

3. **Group commits by project** and create a description by joining commit messages.
   Example: if 3 commits in ABNJ today:
   - `abc1234 - fix auth middleware`
   - `def5678 - add unit tests for auth`
   - `ghi9012 - update API docs`

   The timesheet entry becomes:
   - Project: `ABNJ`
   - Description: `fix auth middleware, add unit tests for auth, update API docs`

4. **Show me the proposed entries** before writing. List each row:
   ```
   Project: ABNJ
   Description: fix auth middleware, add unit tests for auth, update API docs
   ```
   Wait for my approval.

5. **After approval**, write each entry:
   ```bash
   python .agent/skills/timesheet-writer/scripts/timesheet_writer.py append \
     --project "ABNJ" --description "fix auth middleware, add unit tests for auth, update API docs"
   ```

## Rules

- Never write to the sheet without showing me the entries first
- If a GitLab project can't be mapped, ask me
- Strip conventional commit prefixes (feat:, fix:, chore:, etc.) from the description for cleaner output
- If there are no commits today, say so and stop
- If $ARGUMENTS says "dry-run", only show what would be written, don't write

$ARGUMENTS
