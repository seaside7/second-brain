# Knowledge Store

Long-term organizational memory system. Stores durable knowledge per workspace, auto-classified, searchable, and automatically referenced by Claude during work.

## Responsibilities

1. Classify incoming knowledge into categories
2. Store under the active workspace's knowledge folder
3. Deduplicate (update existing entries, never create duplicates)
4. Add metadata (date, workspace, category, tags, source, confidence)
5. Provide search/recall across all stored knowledge

## Does NOT

- Prioritize (that's Inbox Engine)
- Decide what to do with the knowledge (that's Claude's reasoning)
- Store ephemeral notes (use journal/ for those)

## Categories

| Category | What belongs here |
|---|---|
| `people` | Team members, preferences, communication styles, responsibilities |
| `projects` | Project details, architecture, repos, deployment, status |
| `architecture` | System design, infrastructure, tech stack decisions |
| `business` | Business context, clients, contracts, objectives |
| `decisions` | Technical and business decisions with rationale |
| `lessons` | Lessons learned, post-mortems, things that went wrong/right |
| `standards` | Coding standards, naming conventions, processes to follow |
| `troubleshooting` | Bug fixes, root causes, workarounds, known issues |
| `glossary` | Domain terms, acronyms, jargon definitions |
| `processes` | Step-by-step procedures, deployment flows, onboarding |
| `misc` | Anything that doesn't fit above |

## Storage

```
.agent/workspaces/<workspace>/knowledge/
├── people.md
├── projects.md
├── architecture.md
├── business.md
├── decisions.md
├── lessons.md
├── standards.md
├── troubleshooting.md
├── glossary.md
├── processes.md
└── misc.md
```

Each file is append-only markdown with structured entries.

## Entry Format

```markdown
### <title or summary>
- **Date**: 2026-08-03
- **Tags**: #abnj #deployment
- **Source**: manual
- **Confidence**: high

<content>

---
```

## Usage

```bash
# Store a memory (Claude classifies it)
python .agent/skills/knowledge-store/scripts/knowledge_store.py add \
  --category projects \
  --content "ABNJ production uses GitHub. Staging uses GitLab." \
  --tags "abnj,deployment"

# Search across all knowledge
python .agent/skills/knowledge-store/scripts/knowledge_store.py search --query "redis"

# List entries in a category
python .agent/skills/knowledge-store/scripts/knowledge_store.py list --category troubleshooting

# Show all categories and entry counts
python .agent/skills/knowledge-store/scripts/knowledge_store.py status
```

## Commands

- `/remember` — store new knowledge (Claude auto-classifies)
- `/recall` — search stored knowledge
