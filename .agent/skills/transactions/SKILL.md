# Transactions Skill

Track personal bank/e-wallet transactions from Gmail notifications + GoPay PDF uploads.

## Overview

This skill provides:
- **Gmail ingestion**: Automatic daily sync of BCA/BNI transaction emails at 23:59 WIB
- **GoPay PDF import**: Upload and parse GoPay statement PDFs
- **Duplicate detection**: Exact and fuzzy deduplication across all sources
- **Transfer reconciliation**: Match opposite-direction transactions (bank↔GoPay)
- **Categorization**: Deterministic rules for known patterns + AI for ambiguity
- **Reports**: Spending, cash-flow, fees, category breakdowns

## Data Storage

- **SQLite database**: `.agent/workspaces/personal/state/transactions.db` (WAL mode)
- **Uploaded PDFs**: `.agent/workspaces/personal/state/uploads/<fingerprint>/`
- **Config**: `.agent/skills/transactions/config/accounts.json`

## Architecture

```
.agent/skills/transactions/
├── SKILL.md                    # This file
├── config/
│   └── accounts.json           # Account registry + verified senders
└── scripts/
    ├── schema.py               # SQLite schema + connection
    ├── store.py                # DAL (CRUD operations, incl. accounts)
    ├── import_engine.py        # Upload → preview → confirm/delete lifecycle
    ├── duplicates.py           # Duplicate detection
    ├── categorize.py           # Deterministic rules + AI categorization
    ├── reconcile.py            # Transfer matching
    ├── reports.py              # Overview/spending reports
    ├── gmail_sync.py           # Gmail fetch + parse
    ├── scheduler.py            # 23:59 WIB daily sync + startup catch-up
    └── parsers/
        ├── __init__.py
        └── gopay_pdf.py        # pdfplumber GoPay PDF parser
```

## API Routes (server.py)

All routes are personal-only (403 for samudera mode).

### GET
- `/api/transactions/overview?period=` — Dashboard summary
- `/api/transactions/list?filters` — Paginated transaction list
- `/api/transactions/{id}` — Single transaction detail
- `/api/transactions/spending` — Spending by category
- `/api/transactions/transfers` — Transfer links
- `/api/transactions/accounts` — Account registry
- `/api/transactions/review?days=` — Pending review queue
- `/api/transactions/imports` — Import history
- `/api/transactions/rules` — Categorization rules
- `/api/transactions/audit` — Audit trail

### POST
- `/api/transactions/upload` — Upload GoPay PDF (base64 JSON)
- `/api/transactions/import/preview` — Preview import
- `/api/transactions/import/confirm` — Confirm import
- `/api/transactions/import/{id}/delete` — Delete import (reverses ledger)
- `/api/transactions/sync/gmail` — Manual Gmail sync
- `/api/transactions/categorize` — Run AI categorization
- `/api/transactions/{id}/edit` — Edit transaction fields
- `/api/transactions/review/respond` — Apply correction (once/remember)
- `/api/transactions/transfers/{match,reject,link,unlink}` — Transfer ops
- `/api/transactions/rules` — Create rule
- `/api/transactions/accounts` — Create account

## Configuration

### accounts.json

```json
{
  "verified_senders": [
    {"domain": "klikbca.com", "provider": "bca"},
    {"domain": "bni.co.id", "provider": "bni"}
  ],
  "accounts": [
    {"type": "bank", "provider": "bca", "alias": "BCA Said", "masked": "****1234"}
  ]
}
```

## Scheduler

- Daemon thread in dashboard server
- Runs at 23:59 WIB daily (configurable via `TRANSACTIONS_SYNC_HOUR/MINUTE`)
- Startup catch-up if last sync >25h ago
- Manual trigger via `POST /api/transactions/sync/gmail`

## Privacy

- Personal workspace only (never samudera)
- Gmail readonly scope (separate token)
- Masked account numbers (full numbers never stored)
- Minimal data sent to LLM providers (only categorization fields)
- All mutations audit-logged
