# Samudera Indonesia — Workspace Context

## Company Overview

<!-- TODO: fill in — company background, industry, size, public/private, HQ -->
- **Industry**: Shipping / maritime logistics (conglomerate)
- **Public/private**: <!-- TODO -->
- **HQ**: <!-- TODO -->
- **About**: Samudera Indonesia is an Indonesian logistics and shipping group with container shipping, terminal, and logistics businesses.

---

## My Role

- **Title**: Head of Digital Transformation
- **Reports to**: Pak Rando (Digital Transformation) -> Pak Kadek -> Ibu Tara (Human Capital Director)
- **Scope**: Digital transformation strategy, AI initiatives, business process analysis, technology roadmaps across the group
- **Started**: **2026-08-18** (official first day — until then NO Samudera corporate access is assumed)

---

## Organization Structure

Reporting line (confirmed by owner 2026-08-24):

Said Iskandar (Head of Digital Transformation)
  -> Pak Rando (direct manager)
    -> Pak Kadek
      -> Ibu Tara (Human Capital Director)

Group-level structure (from SAMUDERA INDONESIA KNOWLEDGE BASE, Drive/General Docs):
- President Director / CEO at top; Executive Committee: Investment,
  Procurement, Organization & Talent, Business Continuity Management.
- CEO Office Division Head with Special Assistants (Shipowning; Overseas
  Business Development; Employee Satisfaction & Happiness); Corporate
  Internal Audit reports to CEO.
- Directorate level reporting to CEO includes Finance Director (Corporate
  Controller Lukas Gotama, Treasury Stefani W. Savitri, Tax Indra S. Dewa,
  Insurance Prita Sylvanny, Commercial Iksan Ade Kurniawan), Compliance
  Director (Legal: Maharlika Wiedhayaka), Human Capital Director (Ibu Tara).
- Full details live in the brain: knowledge blocks "Samudera KB - 4.
  Organizational Structure" and "Samudera KB - 3. Human Capital Director"
  (.agent/brain/knowledge/people.md). Keep THIS section in sync when new
  org facts are confirmed.

## Business Units

<!-- TODO: list each BU, what they do, DT maturity -->

---

## Key Stakeholders

<!-- TODO: Name | Title | Relationship | Communication preference | Notes -->

---

## Current Initiatives

<!-- TODO -->

---

## Digital Transformation Roadmap

<!-- TODO: phased plan — done / in progress / next -->
<!-- Include: quick wins, medium-term projects, long-term vision -->

---

## AI Opportunities

<!-- TODO: where AI can be applied in the business -->
<!-- Prioritized by: impact, feasibility, data readiness, stakeholder buy-in -->

---

## Meetings

<!-- TODO: recurring meetings — name, cadence, attendees, purpose, prep -->

---

## SOPs

<!-- TODO: SOPs owned/followed — how decisions get made, approvals, budget -->

---

## Systems

<!-- TODO: tech stack, ERP, internal tools, legacy systems -->

---

## Vendors

<!-- TODO: external vendors, consultants, SIs -->

---

## Weekly Reports

<!-- TODO: what reports produced, for whom, format, cadence -->

---

## KPIs

<!-- TODO: metrics for the transformation program -->

---

## Decision Log

<!-- Major decisions made, pending, or escalated — use the decision-log skill to track formally -->

---

## Current Priorities

<!-- TODO -->

---

## Connected Tools

| Tool | Status | Purpose |
|---|---|---|
| Gmail | Not connected (post-join: 2026-08-18+) | Email communication |
| Google Drive | Meeting archive only (personal drive) | Meeting transcripts/MOM storage |
| Google Calendar | Not connected (post-join: 2026-08-18+) | Meeting management |
| Slack | Not connected | Team communication (platform UNKNOWN at Samudera) |
| Fathom | Not connected | Meeting transcripts |

**Full credential/access status: see `CREDENTIALS.md` and `credentials_status.json`
in this workspace. Everything not marked `post_join` is what actually works now.**

## Executive layer

- `executive-pm` skill: workspace-scoped digest (`/focus`, `/risk`).
- `approval-queue` skill: human-approval gate for external actions
  (`/approvals`); execution disabled by design until after join.
- `executive-orchestrator` skill (`/orchestrate <request>` in chat): classifies
  intent, gathers only the minimum relevant specialists, synthesizes a
  decision-oriented answer (escalates to a strong model only for complex
  synthesis). Read-only on workspace data; never executes external actions.

## Phase 3 - Data/BI + research (no corporate data assumed)

- `data-agent` skill: read-only Data/BI agent. `availability` reports exactly
  what is usable today; `query` answers ONLY from real files or
  `configured_working` sources. Anything needing Samudera corporate data that
  was not actually provided returns a graceful "data unavailable" message
  (what is missing, why, when expected, where to drop an export). NEVER
  fabricates numbers.
- `transformation-research` skill: grounded research from news briefings +
  meeting archive + knowledge store; states gaps explicitly (web research not
  configured; ERP/BI post_join >= 2026-08-18). No fabrication.
- Shared availability registry: `.agent/scripts/availability_registry.py` reads
  `credentials_status.json` + the data drop folder.
- Data drop folder: `.agent/workspaces/samudera/data/` - the owner drops
  read-only CSVs/exports here AFTER joining; a source is available only when a
  matching file actually exists (see `data/README.md`).

---

## Notes

<!-- Anything else: cultural norms, communication style, politics, gotchas -->
- **Credential/access constraint:** No Samudera corporate credentials, OAuth,
  databases, BI systems, ERP, or internal tools are assumed available before the
  join date **2026-08-18**. This workspace runs on the personal-drive meeting
  archive + shared AI keys until then. Do not ask the owner for post-join
  credentials early, and never fabricate corporate access or data. Track all of
  it in `CREDENTIALS.md`; do not let the chat/orchestrator claim access it does
  not have.
- Working language: Indonesian for internal chat, English for documents.
- Meetings are recorded locally (meeting-recorder) and archived to the personal drive under `Meeting Transcripts/Samudera/YYYY/MM/`.
