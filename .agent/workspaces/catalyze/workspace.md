# Catalyze Communications — Workspace Context

## Company

- **Name**: Catalyze Communications
- **Type**: Freelance engagement
- **My role**: Backend Engineer (mainly CMS development), sometimes Backend Developer
- **Engagement**: Hourly billing, including meetings

---

## Team

| Name | Role | Notes |
|---|---|---|
| Andry Muharyo | Web Development Lead | Delegates most of my tasks. I call him "Mas" or "Mas Andry" |
| Rendy | Project Manager | |
| Rizqur | Project Manager | |
| Anggun | QA | Often assigns Trello tasks. I call her "Nggun" |
| Carlito | Backend Lead | My backend partner. I call him "To" |
| Fahmi | Frontend Lead | I call him "Mi" |
| Dirom | Frontend Developer | I call him "Rom" |
| Ari | Infrastructure | |
| Said Iskandar | Backend Engineer | Me |

When I mention "Mas" or "Mas Andry" in context, it means Andry Muharyo (my lead).
When I say "Nggun", it means Anggun (QA).
When I say "To", it means Carlito (backend lead).
When I say "Mi", it means Fahmi (frontend lead).
When I say "Rom", it means Dirom (frontend dev).

---

## Active Projects

### WWF Data Platform
- **Stack**: Node.js Backend
- **Auth**: SSO Login
- **Source control**: GitLab (Catalyze, staging) + GitHub (production)
- **Deployment**: Azure

### WWF Data Hub
- **Stack**: Node.js Backend
- **Integrations**: ArcGIS
- **Auth**: SSO
- **Source control**: GitHub (production)
- **Deployment**: Azure

### ABNJ
- **Stack**: Strapi CMS, Node.js
- **Source control**: GitLab (staging) + GitHub (production)
- **Deployment**: Azure

### Katingan Mentaya
- **Stack**: Laravel PHP

### SeaMap ASEAN
- **Stack**: Node.js

### Future Projects
- New projects may be assigned at any time by Mas Andry or the PMs (Rendy, Rizqur).
- When a new project appears, add it here.

---

## Task Sources

Tasks arrive from these channels, ordered by importance:

1. **Mattermost private chat** — direct messages from Mas Andry, Carlito, or PMs
2. **Mattermost group channels** — project-specific channels
3. **Trello** — cards assigned by Anggun (QA) or PMs
4. **Meeting notes** — action items from syncs
5. **Email (Gmail)** — occasional task-bearing emails

Important: many tasks are NOT created in Trello first. They come as Mattermost messages and only later (if ever) become Trello cards. The AI should not assume that "no Trello card = no task."

---

## Git Workflow

Most Catalyze repositories follow this pattern:

```
GitLab (catalyzecommunications/*) → internal testing / staging
GitHub (catalyzecommunications/*) → production
Azure → deployment target
```

- Feature branches off the staging branch (varies: `staging`, `development`, `develop`)
- Merge Requests in GitLab for code review
- After approval, merged to staging, then promoted to GitHub production
- Some projects are GitLab-only or GitHub-only

### GitLab Group
- URL: https://gitlab.com/catalyzecommunications
- 50+ repositories

---

## Development Rules

1. Every development task must be tracked using the Dev Tracker (`/dev start`).
2. Every git push must belong to one tracked task.
3. Every completed task should produce a development report.
4. Development reports are the source material for:
   - Daily work logs
   - Timesheets (Google Sheet, hourly billing)
   - Weekly reports
   - Engineering knowledge base

---

## Communication Style

- Internal chat: casual Indonesian (Bahasa), mixed with English technical terms
- Documentation: English
- Code: English (variables, comments, commit messages)
- When addressing team members, I use their nicknames (Mas, Nggun, To, Mi, Rom)

---

## Timesheet

- **Sheet**: Google Sheets (see `timesheet.json` for ID)
- **Tab naming**: current month in English (July, August, etc.)
- **Columns filled**: C (Projects), D (Description)
- **Columns manual**: A (Date), B (Time), E (Time H/M), F (Time Decimal)
- **Billing**: hourly, includes meetings
- **Source**: GitLab commits + Google Calendar events, pulled via `/timesheet` command

---

## Invoice Generation (billing)

- **Purpose**: Generate monthly Catalyze invoices from the timesheet Google Sheet.
- **Skill**: `.agent/skills/invoice-generator/` (`SKILL.md`, `config.json`, `scripts/invoice_gen.py`)
- **Rate**: IDR 175,000 / hour
- **Bank**: BNI 2017667507 (Said Iskandar)
- **Sheet ID**: `1WKRVlUzG1RY0nL-Eb-ahPn4neigBJ4iSsjAOT7C-dqA`
- **Invoice number format**: `INV-DDMMYY-XXX` (e.g. `INV-310826-001`)
- **Invoice date** = last calendar day of the month
- **PDF layout**: A4 (reportlab); header left-aligned flush with table; approval footer
  `Issued by (Said) / Checked by (HRGA) / Approved by (MD)`; `materai` stamp
  (`assets/materai.png`) slightly overlapping the signature (`assets/signature.jpeg`) on
  the right. Signature + materai images are committed in `assets/` (needed on VPS).
- **Auth**: uses the `catalyze` workspace Drive token (OAuth2, auto-refresh). The token
  file must exist on any machine that generates invoices (copied to VPS via scp; not
  committed).
- **CLI**: `python .agent/skills/invoice-generator/scripts/invoice_gen.py {list-tabs|validate|generate} --month <Month>`
- **Dashboard UI**: the dashboard has an **Invoice tab** (`/api/invoices`,
  `/api/invoice/generate` `{month}`, `/api/invoice/file?name=` download) + a
  `/invoice <month>` slash command in `dashboard/server.py`.
- **Output**: `invoices/` dir, filename `Invoice - Said - [Month].pdf` (gitignored;
  per-machine).
- **Runtime deps**: `reportlab` (installed on VPS with
  `pip3 install --break-system-packages` due to PEP 668).

---

## Connected Tools

| Tool | Status | Purpose |
|---|---|---|
| Gmail | Connected | Task source, notifications |
| Google Drive | Connected | Document storage, timesheets |
| Google Calendar | Pending auth | Meeting tracking, billable hours |
| GitLab | Connected | Source control, commits, MRs |
| Mattermost | Pending token | Primary communication, task source |
| Trello | Pending token | Task management (QA-driven) |
| Fathom | Not connected | Meeting transcripts (future) |

---

## Future Automation Goals

This workspace should eventually support:

- **Inbox Sweep** — scan Mattermost + Gmail + Trello for pending tasks
- **Task Sweep** — aggregate all task sources into a unified view
- **Daily Priority Recommendation** — based on deadlines, source urgency, and context
- **Trello synchronization** — read cards, update status after completion
- **Mattermost synchronization** — read messages, detect task assignments
- **GitLab synchronization** — commits, MRs, pipeline status
- **Gmail synchronization** — task-bearing emails
- **Google Drive synchronization** — shared documents, meeting notes
- **AI-assisted development loop** — Claude helps with code during a tracked dev session
- **Automatic worklog generation** — from dev reports + calendar
- **Timesheet generation** — end-of-day GitLab commits + meetings → Google Sheet
- **Weekly report generation** — synthesize the week's dev reports + meetings

---

## Known Patterns

- Tasks from Mas Andry are usually high priority (he's the lead).
- Trello cards from Anggun are usually QA bugs or testing tasks.
- Carlito and I split backend work — check with him on shared projects.
- Azure deployments are handled by Ari (infrastructure), but I need to ensure staging works before handoff.
- Projects often have both a `-fe` (frontend) and `-be` (backend) repo. I work on the `-be` side.
