# Samudera Credential & Data Access Checklist

> **Single source of truth for what the Samudera workspace can and cannot access.**
> Maintained from 2026-08-14. Revisit every time work touches the Samudera workspace.
>
> **KEY CONSTRAINT — first day at Samudera Indonesia: 18 August 2026.**
> Until 18 Aug 2026, NO Samudera corporate credentials, OAuth, databases, BI
> systems, ERP, or internal tools are assumed available. The Samudera workspace
> runs on credentials/data we ALREADY have (personal + Catalyze work), clearly
> tracked here. Do NOT ask the owner for Samudera credentials before 18 Aug.
> After 18 Aug the owner gradually provisions them; tick items off as they land.
>
> **SEPARATION RULE:** SAMUDERA credentials and PERSONAL credentials are never
> interchangeable. A Samudera token must only ever be stored under
> `.agent/workspaces/samudera/` and used with `--workspace samudera`. PERSONAL
> tokens are only for the owner's personal account. CATALYZE (work) tokens are
> only for said@catalyze.id. Cross-workspace use is a violation.

## Status legend

- `[x]` configured and working
- `[~]` configured, needs verification
- `[ ]` not configured yet
- `[!]` requires setup after 18 Aug 2026 (join date)
- `[?]` UNKNOWN — cannot be determined until after joining; do not assume

---

## 1. PERSONAL credentials (already configured)

Owner personal Google account (`said.iskandar@gmail.com`). These run the
meeting pipeline and the personal-drive meeting archive. They are **never**
used for Samudera corporate access; the Samudera workspace only *reads the
`Meeting Transcripts/Samudera/` folder* out of the personal drive.

| Check | Credential | Status | Where stored | Scope | Read-only? |
|---|---|---|---|---|---|
| [x] | Personal Google Drive OAuth | Working (verified in Phase 0) | `.agent/workspaces/personal/token_drive.json` | `drive` (read/write — needed for v0-upload) | No (upload pipeline) |
| [x] | Personal Drive meeting root | Working | `.agent/workspaces/personal/documents.json` → `meeting_folder` | Meeting Transcripts root | — |
| [x] | Samudera meeting archive in personal drive | Working | `Meeting Transcripts/Samudera/` (folder `1IL_fiBKaWmLv1JL3ZQOmvdV6In2mFry5`) | read via doc_connector | Yes |

## 2. CATALYZE (work) credentials (already configured — NOT Samudera)

Owner work account (`said@catalyze.id`). Present and functional for Catalyze
work. Isolated from the Samudera workspace by workspace routing; listed here
only so there is no accidental reuse.

| Check | Credential | Status | Where stored | Scope |
|---|---|---|---|---|
| [x] | Catalyze Google Gmail | Assumed working | `.agent/workspaces/catalyze/token_gmail.json` + `gmail-connector/token_gmail_work.json` | `gmail.modify` |
| [x] | Catalyze Google Drive | Assumed working | `.agent/workspaces/catalyze/token_drive.json` + `work-drive-connector/token.json` | `drive` |
| [x] | Catalyze Google Calendar | Assumed working | `.agent/workspaces/catalyze/token_calendar.json` + `work-drive-connector/token_calendar_work.json` | `calendar.events`, `calendar.readonly` |
| [x] | GitLab | Assumed working | `gitlab-connector/token.env` | read/write (Catalyze projects) |
| [x] | Mattermost | Assumed working | `mattermost-connector/token.env` | read/write (Catalyze team) |
| [x] | Trello | Assumed working | `trello-connector/token.env` | read/write (Catalyze boards) |
| [x] | Slack (work) | Real tokens present | root `.env` → `SLACK_BOT_TOKEN`, `SLACK_USER_TOKEN` | read + post (via approval only) |

## 3. SHARED / AI credentials (already configured, not workspace-bound)

| Check | Credential | Status | Where stored | Used by |
|---|---|---|---|---|
| [x] | DeepSeek API key | Working (verified 2026-08-14, model `deepseek-v4-flash`) | root `.env` → `DEEPSEEK_API_KEY` | chat, model_router, news scoring, orchestrator |
| [x] | OpenAI API key | Working (verified 2026-08-14, `gpt-5.6-luna/terra/sol`, whisper-1) | root `.env` → `OPENAI_API_KEY` | chat fallback, transcription, MOM drafting, complex reasoning |
| [x] | agy-bridge (Gemini subscription) | Configured via `models.json` | `.agent/skills/agy-bridge/models.json` (token optional; CLI subscription) | last-resort chat fallback, drafts |
| [~] | Fathom API key | Placeholder in root `.env` (`FATHOM_API_KEY=your-...`) | root `.env` | meeting transcripts (Catalyze) — verify before relying on it |
| [~] | Mixpanel / Figma / ClickUp tokens | Placeholder values in root `.env` | root `.env` | existing connectors — verify; not needed for Samudera pre-join |

---

## 4. SAMUDERA credentials — needed after 18 Aug 2026

The owner joins Samudera on **2026-08-18** and will provision these gradually.
Until then the Samudera workspace runs on personal-drive meeting archives +
shared AI keys only. `[!]` = must wait for join. Details per credential:

| # | Check | Credential / access | When to set up |
|---|---|---|---|
| 1 | [!] | Samudera Google Gmail (`said@samudera.id`) | After 18 Aug |
| 2 | [!] | Samudera Google Drive | After 18 Aug |
| 3 | [!] | Samudera Google Calendar | After 18 Aug |
| 4 | [!] | Samudera BI / analytics access | After 18 Aug (platform UNKNOWN) |
| 5 | [!] | Samudera database (read) access | After 18 Aug (UNKNOWN) |
| 6 | [!] | ERP access | After 18 Aug (UNKNOWN) |
| 7 | [!] | Communication platform | After 18 Aug (UNKNOWN) |
| 8 | [!] | Project / task management platform | After 18 Aug (UNKNOWN) |
| 9 | [?] | Other enterprise systems | Discover after joining |

### 4.1 Samudera Google Gmail
- **What it's for:** executive email management, executive-comms sends, inbox-hub
  aggregation for the Samudera dashboard, approval-queue email actions.
- **Which agent/skill needs it:** gmail-connector, inbox-hub, executive-comms,
  executive-orchestrator (communication specialist), approval queue.
- **Required or optional:** Required (core).
- **Where to store:** `.agent/workspaces/samudera/token_gmail.json` (gitignored).
- **Minimum permission/scope:** `gmail.readonly` to start. `gmail.modify` or
  `gmail.send` only when the approval queue authorizes sends.
- **Read-only?:** Read-only first; sends only via the approval queue + audit log.
- **When to set it up:** 2026-08-18+ (needs Samudera IT to grant/own the OAuth client or a service account).

### 4.2 Samudera Google Drive
- **What it's for:** business documents, PRDs, strategy decks, meeting artifacts;
  the Data/Document layer for the DT workspace.
- **Which agent/skill needs it:** document-intelligence (`doc_connector`/`doc_engine`),
  work-drive-connector, knowledge-store, solution-architect, executive-comms.
- **Required or optional:** Required (core).
- **Where to store:** `.agent/workspaces/samudera/token_drive.json` + update
  `.agent/workspaces/samudera/documents.json` `folder_id` (currently points to the
  personal-drive Samudera meeting archive as a pre-join fallback).
- **Minimum permission/scope:** `drive.readonly` to start; `drive` only if the
  pipeline must upload artifacts.
- **Read-only?:** Read-only first.
- **When to set it up:** 2026-08-18+.

### 4.3 Samudera Google Calendar
- **What it's for:** meeting prep, premeeting-cards, weekly reports, scheduling
  awareness, the chat "meetings today" context.
- **Which agent/skill needs it:** google-calendar-connector, premeeting-cards,
  weekly-report-generator, executive-orchestrator.
- **Required or optional:** Required (core).
- **Where to store:** `.agent/workspaces/samudera/token_calendar.json`.
- **Minimum permission/scope:** `calendar.readonly` to start; `calendar.events`
  only if the system books/schedules on the owner's behalf (via approval).
- **Read-only?:** Read-only first.
- **When to set it up:** 2026-08-18+.

### 4.4 Samudera BI / analytics access
- **What it's for:** Data/BI agent — executive KPIs, transformation metrics.
- **Which agent/skill needs it:** data-agent, metabase-connector / ga4-connector /
  mixpanel-connector (whichever platform Samudera uses — UNKNOWN), orchestrator.
- **Required or optional:** Desired for Phase 3 (Data/BI capability).
- **Where to store:** `.agent/workspaces/samudera/<platform>.env` or token file.
- **Minimum permission/scope:** read-only queries/views; no write.
- **Read-only?:** Yes — always.
- **When to set it up:** After 18 Aug, once the platform is discovered and read access is granted.

### 4.5 Samudera database (read) access
- **What it's for:** Data/BI agent, transformation research, process analysis.
- **Which agent/skill needs it:** data-agent, transformation-research.
- **Required or optional:** Optional.
- **Where to store:** `.agent/workspaces/samudera/db.env` (never in code/commits).
- **Minimum permission/scope:** `SELECT` only, on curated views; no DDL/DML.
- **Read-only?:** Yes — never write access from the LLM to production.
- **When to set it up:** After 18 Aug; requires DBA/data-team approval. Platform UNKNOWN.

### 4.6 ERP access
- **What it's for:** business-process analysis, digital-transformation initiatives.
- **Which agent/skill needs it:** transformation-research, solution-architect.
- **Required or optional:** Optional.
- **Where to store:** `.agent/workspaces/samudera/<erp>.env` (per connector).
- **Minimum permission/scope:** read-only reports/views.
- **Read-only?:** Yes.
- **When to set it up:** After 18 Aug. **ERP system UNKNOWN — do not assume a specific vendor.**

### 4.7 Communication platform
- **What it's for:** team/stakeholder comms, follow-up tracking, PM/execution.
- **Which agent/skill needs it:** slack-connector / mattermost-connector / other
  (existing connectors reused), commitment-ledger, waiting-watchdog, inbox-hub.
- **Required or optional:** Desired.
- **Where to store:** `.agent/workspaces/samudera/<platform>.env` or connector token.env.
- **Minimum permission/scope:** read + post; sends only via the approval queue.
- **Read-only?:** Read-only first; sends via approval queue + audit log.
- **When to set it up:** After 18 Aug. **Platform UNKNOWN (could be Slack, Teams,
  Mattermost, etc.) — do not assume.**

### 4.8 Project / task management platform
- **What it's for:** initiative and roadmap tracking, PM/execution agent.
- **Which agent/skill needs it:** jira-connector / trello-connector /
  clickup-connector (whichever matches), executive-pm.
- **Required or optional:** Desired.
- **Where to store:** `.agent/workspaces/samudera/<tool>.env`.
- **Minimum permission/scope:** read; create/update only via approval queue.
- **Read-only?:** Read-only first.
- **When to set it up:** After 18 Aug. **Platform UNKNOWN — do not assume.**

### 4.9 Other enterprise systems
- **What they are:** UNKNOWN. Org directory, HR, fleet/ops, procurement, finance —
  discovered after joining and mapped to skills then.
- **Required or optional:** TBD.
- **When to set them up:** After 18 Aug, as discovered.

---

## 5. Data sources still unknown

These are data *sources*, not credentials — cannot be confirmed until after joining:

- Corporate org directory / reporting lines / stakeholder map
- Budget and finance data (transformation program budget)
- Fleet / operations / shipping data (vessel tracking, terminals, logistics ops)
- HR / people data
- Procurement / vendor data
- Actual business KPIs and current-state metrics
- Which (if any) Google Workspace is used at Samudera (Gmail/Drive/Calendar is
  ASSUMED below only as a default; confirm after joining)

> Rule: none of these may be fabricated. Until confirmed, the executive-pm and
> orchestration outputs state "not yet available" rather than guessing.

---

## 6. After-join provisioning plan (from 18 Aug 2026)

1. Confirm Google Workspace availability; create samudera OAuth tokens
   (gmail/drive/calendar) in `.agent/workspaces/samudera/`.
2. Discover and record BI, database, ERP, comms, and project tools; mark each
   `[x]`/`[~]`/`[ ]` here as they are provisioned.
3. For each new token: read-only first, store only in the samudera workspace
   folder, never commit, always `--workspace samudera`.
4. Update this checklist + `credentials_status.json` on every change.
