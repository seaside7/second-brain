# Requirement Doc Template

Copy to `docs/requirements/<menu>.md` and fill in. One file per menu. Keep it the single source of truth for that menu's requirements: every time a change request is approved, update the relevant sections below AND append an entry to `## Change Requests`.

## 1. Purpose & scope

What this feature/menu does, and what it explicitly does NOT do.

## 2. Inputs / sources

Every data source it reads: APIs, Gmail, PDFs, sheets, DBs, VPS files, manual entries. Format: source -> format -> how it arrives.

## 3. Logic & rules

The decision trees, thresholds, key-value tables, ordering, and edge-case handling. List rules top-to-bottom if order matters (specify "first match wins").

## 4. Outputs

API routes, reports, files, notifications it produces. For each: what it returns/creates and who consumes it.

## 5. Data storage

Files and DBs it reads/writes, with paths. Note what is tracked in git vs gitignored.

## 6. Acceptance criteria

How to tell this menu behaved correctly on a given input. Include edge cases that must hold.

## 7. Change Requests

One line per request entry:

- `YYYY-MM-DD | <your request, verbatim-ish> | decision: <what was decided> | changed: <what changed + files>`

Append only, newest at the bottom.