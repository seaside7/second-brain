---
description: Search your workspace memory for stored knowledge
argument-hint: "<search term>"
---

Search the active workspace's knowledge store for relevant information.

## Steps:

1. Run the search:
   ```bash
   python .agent/skills/knowledge-store/scripts/knowledge_store.py search --query "$ARGUMENTS"
   ```

2. If results are found, present them clearly:
   - Category
   - Title
   - Date stored
   - Full content
   - Tags

3. If no results, try variations:
   - Synonyms (e.g. "deploy" for "deployment", "redis" for "cache")
   - Partial matches
   - Search in related categories

4. If still nothing, say so plainly and suggest using `/remember` to store the information.

## Rules

- Search is workspace-scoped (active workspace only)
- Present the most relevant results first
- If the query matches multiple entries, show all of them
- Never fabricate knowledge that isn't in the store

$ARGUMENTS
