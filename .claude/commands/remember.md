---
description: Store durable knowledge into the workspace memory - Claude auto-classifies and stores it
argument-hint: "[#tags] the knowledge to remember"
---

Store knowledge into the active workspace's long-term memory.

## How to process $ARGUMENTS:

1. **Extract tags** if present (words starting with #):
   - `/remember #abnj #deployment Production uses GitHub` → tags: [abnj, deployment], content: "Production uses GitHub"
   - `/remember Mas Andry prefers short updates` → tags: [], content: "Mas Andry prefers short updates"

2. **Classify the content** into one of these categories:
   - `people` — info about team members, preferences, communication styles
   - `projects` — project details, repos, deployment, architecture
   - `architecture` — system design, infrastructure, tech decisions
   - `business` — business context, clients, contracts
   - `decisions` — decisions made with rationale
   - `lessons` — lessons learned, things that went wrong/right
   - `standards` — coding standards, conventions, rules to follow
   - `troubleshooting` — bug fixes, root causes, workarounds
   - `glossary` — terms, acronyms, definitions
   - `processes` — step-by-step procedures, deployment flows
   - `misc` — doesn't fit elsewhere

   Classification hints:
   - Mentions a person's name/preferences → `people`
   - Mentions a repo/deployment/server → `projects` or `architecture`
   - "Never do X" / "Always do Y" → `standards`
   - "Bug was caused by..." / "Fixed by..." → `troubleshooting`
   - "We decided..." / "The decision was..." → `decisions`
   - "Lesson:" / "Next time..." → `lessons`

3. **Check for duplicates** by searching existing knowledge:
   ```bash
   python .agent/skills/knowledge-store/scripts/knowledge_store.py search --query "<key phrase>"
   ```
   If something very similar already exists, tell me and ask whether to update or skip.

4. **Store it**:
   ```bash
   python .agent/skills/knowledge-store/scripts/knowledge_store.py add \
     --category <category> \
     --content "<the full content>" \
     --tags "<comma-separated tags>" \
     --title "<short title>"
   ```

5. **Confirm** what was stored:
   - Category chosen
   - Title generated
   - File path
   - Tags applied

## Rules

- Never ask me which category to use. Classify it yourself.
- If genuinely ambiguous between two categories, pick the more specific one.
- Keep the stored content clean and factual. Don't add fluff.
- If I provide multiple facts in one /remember, store them as ONE entry (not split).
- Tags are optional. If I don't provide any, infer 1-2 relevant tags from the content.
- This is workspace-scoped. The active workspace determines where it's stored.

## Examples

Input: `/remember #abnj #deployment ABNJ production uses GitHub. Staging uses GitLab. Never deploy directly to production.`
→ Category: `projects`
→ Tags: abnj, deployment
→ Title: "ABNJ deployment: GitHub production, GitLab staging"

Input: `/remember Mas Andry prefers short updates. He usually delegates CMS and backend tasks to me.`
→ Category: `people`
→ Tags: andry
→ Title: "Mas Andry - communication and delegation preferences"

Input: `/remember WWF Data Platform login bug was caused by Redis TTL mismatch. Increasing TTL to 24 hours fixed it.`
→ Category: `troubleshooting`
→ Tags: wwf, redis, login
→ Title: "WWF Data Platform login bug - Redis TTL mismatch"

$ARGUMENTS
