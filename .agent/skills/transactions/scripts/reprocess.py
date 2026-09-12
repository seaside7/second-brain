"""In-place reprocessing of previously-imported GMAIL emails.

Re-fetches each stored gmail source document's FULL message (referenced by
msg_id inside source_key = ``gmail:{provider}:{msg_id}``) so BNI/wondr bodies
parse with the current v3.0 parser instead of the old 400-char preview.

Behavior
--------
- Updates extracted_txns + ledger_txns IN PLACE, keyed by existing rows.
- A parser-introduced fee split creates ONE new ext + ledger row (never a
  duplicate of an existing transaction).
- Idempotent: re-running echoes the same numbers.
- Preserves user manual corrections (has_manual_correction) - their
  category_id/nature/confidence are never overwritten.
- Re-fetched bodies are re-masked (mask_sensitive) before storing.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Optional

import gmail_sync
import store


def _list_gmail_docs(conn: sqlite3.Connection,
                     provider: Optional[str] = None) -> list[dict]:
    sql = ("SELECT d.*, b.id as batch_id "
           "FROM source_documents d JOIN import_batches b ON b.source_document_id=d.id "
           "WHERE d.kind='gmail' AND d.status='parsed'")
    params: list = []
    if provider:
        sql += " AND d.provider=?"
        params.append(provider)
    sql += " ORDER BY d.id"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _extracted_for_doc(conn: sqlite3.Connection, doc_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM extracted_txns WHERE doc_id=? ORDER BY ext_index, id",
        (doc_id,)).fetchall()]


def _ledger_for_ext(conn: sqlite3.Connection, ext_id: int) -> Optional[dict]:
    r = conn.execute("SELECT * FROM ledger_txns WHERE ext_id=?", (ext_id,)).fetchone()
    return dict(r) if r else None


def reprocess(conn: sqlite3.Connection, *,
              provider: Optional[str] = None,
              msg_ids: Optional[set[str]] = None,
              dry_run: bool = False) -> dict:
    """Reprocess stored gmail docs with the current parser/categorizer.

    Filters to the given provider and/or explicit gmail message ids when
    supplied (msg_ids like {'<msg_id>'} or full source_key hashes).
    """
    stats = {'ok': True, 'scanned': 0, 'processed': 0, 'skipped': 0,
             'failed': 0, 'updated_ext': 0, 'added_ledger': 0,
             'preserved_manual': 0, 'uncategorized': 0, 'errors': []}

    service = gmail_sync.get_gmail_service()
    if not service:
        return {'ok': False, 'error': 'Gmail not configured (token missing or invalid)'}

    docs = _list_gmail_docs(conn, provider=provider)
    for doc in docs:
        m = re.match(r'^gmail:([a-z]+):(.+)$', doc['source_key'] or '')
        if not m:
            stats['failed'] += 1
            stats['errors'].append(f"{doc['id']}: bad source_key {doc['source_key']!r}")
            continue
        p_sk = m.group(1)
        msg_id = m.group(2)
        if msg_ids and msg_id not in msg_ids:
            continue
        stats['scanned'] += 1

        try:
            # Pace fetches so the free Gmail quota (~50 full messages/min)
            # never trips mid-run.
            time.sleep(0.35)
            message = gmail_sync._fetch_message(service, msg_id)
            headers = message.get('payload', {}).get('headers', [])
            if gmail_sync.verify_sender(headers) != p_sk:
                stats['skipped'] += 1
                continue

            subject = next((h['value'] for h in headers
                            if h['name'].lower() == 'subject'), '')
            date_str = next((h['value'] for h in headers
                             if h['name'].lower() == 'date'), '')
            body_text = gmail_sync._extract_body(message.get('payload', {}))

            parsed = gmail_sync._parse_email(body_text, p_sk, subject, date_str)
            if not parsed:
                stats['skipped'] += 1
                continue

            doc_stats = _reprocess_doc(conn, doc, parsed, dry_run=dry_run)
            stats['updated_ext'] += doc_stats['updated_ext']
            stats['added_ledger'] += doc_stats['added_ledger']
            stats['preserved_manual'] += doc_stats['preserved_manual']
            stats['uncategorized'] += doc_stats['uncategorized']
            stats['skipped'] += doc_stats['skipped']
            stats['processed'] += 1

            if not dry_run:
                store.update_source_document(
                    conn, doc['id'], status='parsed', parser_version='3.0',
                    preview=body_text[:400])
        except Exception as e:
            stats['failed'] += 1
            stats['errors'].append(f'{msg_id}: {e}')

    return stats


def _reprocess_doc(conn: sqlite3.Connection, doc: dict,
                   parsed: list[dict], *, dry_run: bool) -> dict:
    """Update one doc's extracted+ledger rows in place; returns counts."""
    out = {'updated_ext': 0, 'added_ledger': 0, 'preserved_manual': 0,
           'uncategorized': 0, 'skipped': 0}

    existing = _extracted_for_doc(conn, doc['id'])
    from categorize import categorize_batch, apply_categorization
    next_index = len(existing)

    for i, p in enumerate(parsed):
        ext = existing[i] if i < len(existing) else None
        amount = p.get('total_amount') or p.get('principal_amount') or 0

        if ext is None:
            # New row introduced by this parser (fee split). Create ext + ledger.
            if dry_run:
                out['added_ledger'] += 1
                continue
            p = dict(p)
            p['ext_index'] = next_index
            next_index += 1
            batch_id = doc['batch_id']
            ext_ids = store.add_extracted_rows(conn, batch_id, doc['id'], [p])
            ledger_id = store.add_ledger_row(
                conn, ext_ids[0], amount=amount,
                direction=p.get('direction', 'out'),
                nature='needs_review', confidence='low',
                confidence_reason=f'email_regex_{doc["provider"]}',
                evidence_json=json.dumps(
                    {'msg_id': doc['source_key'], 'provider': doc['provider']}),
                review_status='ok')
            out['added_ledger'] += 1
            _apply_result(conn, p, ledger_id, out)
            continue

        if dry_run:
            out['updated_ext'] += 1
            continue

        store.update_extracted(conn, ext['id'],
            description=p.get('description', ''),
            raw_description=p.get('raw_description', ''),
            transaction_type=p.get('transaction_type', ''),
            merchant=p.get('merchant', ''),
            recipient=p.get('recipient', ''),
            direction=p.get('direction', 'out'),
            principal_amount=p.get('principal_amount', 0),
            fee_amount=p.get('fee_amount', 0),
            total_amount=amount,
            occurred_at=p.get('occurred_at', ''),
            parser_version='3.0')
        out['updated_ext'] += 1

        ledger = _ledger_for_ext(conn, ext['id'])
        if ledger is None:
            continue
        if store.has_manual_correction(conn, ledger['id']):
            out['preserved_manual'] += 1
            # Manual correction: refresh the amount (parse fix) but never the
            # user's chosen category/nature/confidence.
            if not dry_run:
                store.update_ledger(conn, ledger['id'],
                                    amount=amount,
                                    direction=p.get('direction', 'out'))
            continue
        _apply_result(conn, p, ledger['id'], out)

    return out


def _apply_result(conn: sqlite3.Connection, p: dict,
                  ledger_id: int, out: dict) -> None:
    """Run the deterministic categorizer on a re-parsed row and write it."""
    from categorize import categorize_batch, apply_categorization
    results = categorize_batch(conn, [dict(p)])
    if results and results[0].get('category_id'):
        apply_categorization(conn, results[0], ledger_id)
        if results[0].get('nature') == 'needs_review':
            out['uncategorized'] += 1
    else:
        out['uncategorized'] += 1