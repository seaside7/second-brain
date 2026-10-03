"""Import orchestrator for the Transactions feature.

Handles: upload → parse → preview → confirm → delete lifecycle.
Coordinates dedupe checks, categorization, and ledger row creation.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import store
import schema
from parsers.gopay_pdf import parse_gopay_pdf, PARSER_VERSION as GOPAY_VERSION
from parsers.bca_pdf import parse_bca_pdf, PARSER_VERSION as BCA_VERSION
from parsers.bni_pdf import parse_bni_pdf, PARSER_VERSION as BNI_VERSION
from parsers.mandiri_pdf import parse_mandiri_pdf, PARSER_VERSION as MANDIRI_VERSION
from duplicates import check_duplicates
from categorize import categorize_batch

UPLOAD_PROVIDERS = {
    'gopay': {'parser': parse_gopay_pdf, 'version': GOPAY_VERSION,
              'kwargs': {'include_coins': False}},
    'bca': {'parser': parse_bca_pdf, 'version': BCA_VERSION, 'kwargs': {}},
    'bni': {'parser': parse_bni_pdf, 'version': BNI_VERSION, 'kwargs': {}},
    'mandiri': {'parser': parse_mandiri_pdf, 'version': MANDIRI_VERSION,
                'kwargs': {}},
}

# Brand labels shown in the UI when an upload's account is auto-created.
_ACCOUNT_DEFS = {
    'gopay': {'type': 'ewallet', 'provider': 'gopay', 'alias': 'GoPay'},
    'bca': {'type': 'bank', 'provider': 'bca', 'alias': 'BCA'},
    'bni': {'type': 'bank', 'provider': 'bni', 'alias': 'BNI'},
    'mandiri': {'type': 'bank', 'provider': 'mandiri', 'alias': 'Mandiri'},
}

_UPLOAD_DIR = Path(os.environ.get(
    'TRANSACTIONS_UPLOAD_DIR',
    Path(__file__).resolve().parent.parent.parent.parent / 'workspaces' / 'personal' / 'state' / 'uploads'
))


def ensure_upload_dir() -> Path:
    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return _UPLOAD_DIR


def _ensure_account(conn: sqlite3.Connection, provider: str) -> int | None:
    """Get-or-create the account stamped on a provider's uploaded rows."""
    low = (provider or 'gopay').lower()
    for a in store.list_accounts(conn, active_only=True):
        if (a.get('provider') or '').lower() == low:
            return a['id']
    if low not in _ACCOUNT_DEFS:
        return None
    return store.add_account(conn, **_ACCOUNT_DEFS[low])


def upload_pdf(conn: sqlite3.Connection, *,
               filename: str, b64data: str,
               provider: str = 'gopay',
               password: str = '',
               month: str = '',
               auto_confirm: bool = True,
               max_size_mb: int = 20) -> dict:
    """Upload a bank/e-wallet statement PDF (base64-encoded).

    ``provider`` selects the parser (gopay|bca|bni|mandiri); ``password`` is
    passed through for encrypted statements (e.g. Mandiri/BNI); ``month``
    (YYYY-MM) keeps only that month's rows.

    With auto_confirm=True (default) matching rows are stamped with the
    provider's account and confirmed straight into the ledger. With
    auto_confirm=False the upload stops at a 'preview' batch and returns the
    extracted rows with duplicate + cross-statement overlap flags; callers
    then confirm via confirm_import(...).

    Returns {ok, doc_id, batch_id, account_id, total_rows, new_rows,
    duplicate_rows, categorized, skipped_rows, ...} or {ok:False, error}.
    """
    # Decode
    try:
        raw = base64.b64decode(b64data)
    except Exception:
        return {'ok': False, 'error': 'Invalid base64 data'}

    # Size check
    if len(raw) > max_size_mb * 1024 * 1024:
        return {'ok': False, 'error': f'File exceeds {max_size_mb}MB limit'}

    # PDF magic check
    if not raw[:5] == b'%PDF-':
        return {'ok': False, 'error': 'Not a valid PDF file'}

    provider = (provider or 'gopay').lower()
    if provider not in UPLOAD_PROVIDERS:
        return {'ok': False, 'error': f'Unsupported provider "{provider}"'}
    parser_spec = UPLOAD_PROVIDERS[provider]
    kind = f'{provider}_pdf'

    # Sanitize filename
    safe_name = Path(filename).stem.replace('..', '').replace('/', '_').replace('\\', '_')[:80]
    safe_name = ''.join(c for c in safe_name if c.isalnum() or c in '._- ') + '.pdf'

    # Fingerprint + month filter
    fingerprint = hashlib.sha256(raw).hexdigest()[:32]
    source_key = f'upload:{fingerprint}'
    month = (month or '').strip()
    if month and not re.match(r'^\d{4}-(0[1-9]|1[0-2])$', month):
        return {'ok': False, 'error': f'Invalid month "{month}"'}

    # Check duplicate document. A re-import is only blocked when a committed
    # batch exists; a doc that never produced a batch (failed parse, or a
    # crash before the batch insert committed) is cleared and retried fresh.
    existing_doc = store.source_doc_exists(conn, source_key=source_key, fingerprint=fingerprint)
    if existing_doc:
        batch_count = conn.execute(
            'SELECT COUNT(*) FROM import_batches WHERE source_document_id=?',
            (existing_doc,)).fetchone()[0]
        if batch_count:
            return {'ok': False, 'error': 'This file has already been imported', 'doc_id': existing_doc}
        conn.execute('DELETE FROM extracted_txns WHERE doc_id=?', (existing_doc,))
        conn.execute('DELETE FROM import_batches WHERE source_document_id=?', (existing_doc,))
        conn.execute('DELETE FROM source_documents WHERE id=?', (existing_doc,))
        conn.commit()

    # Save file
    upload_dir = ensure_upload_dir()
    doc_dir = upload_dir / fingerprint
    doc_dir.mkdir(exist_ok=True)
    file_path = doc_dir / safe_name
    file_path.write_bytes(raw)

    # Create source_document
    doc_id = store.add_source_document(conn,
        kind=kind, source_key=source_key, fingerprint=fingerprint,
        provider=provider, upload_path=str(file_path))

    # Parse
    try:
        parse_result = parser_spec['parser'](file_path,
                                             password=password,
                                             **parser_spec['kwargs'])
        store.update_source_document(conn, doc_id, status='parsed',
            parser_version=parse_result.get('parser_version', parser_spec['version']),
            preview=json.dumps({
                'row_count': len(parse_result.get('rows', [])),
                'account_name': parse_result.get('account_name', ''),
                'period': parse_result.get('statement_period', ''),
            }, ensure_ascii=False))
    except Exception as e:
        store.update_source_document(conn, doc_id, status='failed', error=str(e))
        return {'ok': False, 'error': f'Parse error: {e}', 'doc_id': doc_id}

    # Month filter (out-of-month rows are skipped, not imported)
    rows = parse_result.get('rows', [])
    skipped_rows = 0
    if month:
        kept = [r for r in rows if str(r.get('occurred_at', ''))[:7] == month]
        skipped_rows = len(rows) - len(kept)
        rows = kept
        if not rows:
            store.update_source_document(conn, doc_id, status='failed',
                error=f'No transactions found for {month}')
            return {
                'ok': False, 'error': f'No transactions found for {month}',
                'doc_id': doc_id, 'skipped_rows': skipped_rows,
            }

    # Create batch + store extracted rows (with dup checks)
    batch_id = store.add_import_batch(conn, doc_id)
    if rows:
        _store_extracted_rows(conn, batch_id, doc_id, rows, provider)

    preview_data = {
        'doc_id': doc_id,
        'batch_id': batch_id,
        'row_count': len(rows),
        'skipped_rows': skipped_rows,
        'account_name': parse_result.get('account_name', ''),
        'period': parse_result.get('statement_period', ''),
        'fingerprint': fingerprint,
        'warnings': parse_result.get('warnings', []),
        'parser_version': parse_result.get('parser_version', parser_spec['version']),
        'provider': provider,
    }
    if not auto_confirm:
        # Preview-only: leave the batch in 'preview' state and report
        # duplicates + cross-statement overlap flags per row. The dashboard
        # passes auto_confirm=False so humans approve before the ledger moves.
        rows_meta = _row_flags(conn, batch_id)
        return {
            'ok': True, **preview_data,
            'account_id': None,
            'confirm_pending': True,
            'rows': rows_meta['rows'],
            'new_rows': rows_meta['new_rows'],
            'duplicate_rows': rows_meta['duplicate_rows'],
            'overlap_rows': rows_meta['overlap_rows'],
        }

    # Auto-confirm into the ledger, stamped with the provider's account
    account_id = _ensure_account(conn, provider)
    confirm = confirm_import(conn, batch_id, account_id=account_id)
    if not confirm.get('ok'):
        return {'ok': False, **preview_data,
                'account_id': account_id,
                'error': confirm.get('error', 'Auto-confirm failed')}
    return {
        'ok': True, **preview_data,
        'account_id': account_id,
        'confirm_pending': False,
        'total_rows': confirm.get('total_rows', len(rows)),
        'new_rows': confirm.get('new_rows', 0),
        'duplicate_rows': confirm.get('duplicate_rows', 0),
        'categorized': confirm.get('categorized', 0),
    }


def _row_flags(conn: sqlite3.Connection, batch_id: int) -> dict:
    """Per-row preview flags for a preview batch.

    Each extracted row is reported with its duplicate status and a
    cross-account overlap hint (used to catch the same transaction appearing
    on two statements, e.g. a transfer on both bank legs).
    """
    from duplicates import match_overlap
    rows = store.list_extracted_by_batch(conn, batch_id)
    preview = []
    new_rows = 0
    duplicate_rows = 0
    overlap_rows = 0
    for row in rows:
        cand = {
            'src_txn_id': row.get('src_txn_id', ''),
            'occurred_at': row.get('occurred_at', ''),
            'direction': row.get('direction', 'out'),
            'total_amount': row.get('total_amount', 0),
            'bank_ref': row.get('bank_ref', ''),
            'recipient': row.get('recipient', ''),
            'merchant': row.get('merchant', ''),
            'transaction_type': row.get('transaction_type', ''),
        }
        overlap = match_overlap(conn, cand)
        dup = row.get('dup_status') in ('exact_dup', 'possible')
        item = {
            'idx': row.get('ext_index', 0),
            'occurred_at': row.get('occurred_at', ''),
            'description': row.get('description', ''),
            'direction': row.get('direction', 'out'),
            'total_amount': row.get('total_amount', 0),
            'duplicate': 1 if dup else 0,
            'duplicate_of': row.get('duplicate_of'),
            'overlap_status': overlap.get('status', 'different'),
        }
        if dup:
            duplicate_rows += 1
        elif overlap.get('status') in ('identical', 'likely'):
            overlap_rows += 1
        else:
            new_rows += 1
        preview.append(item)
    return {
        'rows': preview,
        'new_rows': new_rows,
        'duplicate_rows': duplicate_rows,
        'overlap_rows': overlap_rows,
    }


_IMAGE_MAGIC = [(b'\x89PNG\r\n\x1a\n', 'image/png'),
                (b'\xff\xd8\xff', 'image/jpeg'),
                (b'RIFF', 'image/webp')]


def upload_screenshot(conn: sqlite3.Connection, *,
                      filename: str, b64data: str,
                      max_size_mb: int = 10) -> dict:
    """Upload a bank/e-wallet app screenshot and extract its rows via Gemini.

    Screenshots are always preview-only: rows are held in a 'preview' batch
    (kind 'screenshot', auto_confirm=False semantics) so the owner reviews
    vision extraction before anything is confirmed. Auth keys are read at call
    time via vision_extract and never logged.

    Returns the same preview payload shape as upload_pdf(auto_confirm=False).
    """
    import uuid
    from vision_extract import extract_transactions

    try:
        raw = base64.b64decode(b64data)
    except Exception:
        return {'ok': False, 'error': 'Invalid base64 data'}
    if len(raw) > max_size_mb * 1024 * 1024:
        return {'ok': False, 'error': f'File exceeds {max_size_mb}MB limit'}

    ext = (Path(filename).suffix or '.png').lower()
    if ext not in ('.png', '.jpg', '.jpeg', '.webp'):
        return {'ok': False, 'error': 'Unsupported image type (use PNG/JPG/WebP)'}
    mime_of = {'.png': 'image/png', '.jpg': 'image/jpeg',
               '.jpeg': 'image/jpeg', '.webp': 'image/webp'}[ext]
    if not any(raw.startswith(m) for m, _ in _IMAGE_MAGIC):
        return {'ok': False, 'error': 'Not a valid image file'}

    provider = 'screenshot'
    kind = 'screenshot'
    fingerprint = hashlib.sha256(raw).hexdigest()[:32]
    source_key = f'screenshot:{fingerprint}'
    safe_name = Path(filename).stem.replace('..', '').replace('/', '_').replace('\\', '_')[:80]
    safe_name = ''.join(c for c in safe_name if c.isalnum() or c in '._- ') + ext

    existing_doc = store.source_doc_exists(conn, source_key=source_key, fingerprint=fingerprint)
    if existing_doc:
        batch_count = conn.execute(
            'SELECT COUNT(*) FROM import_batches WHERE source_document_id=?',
            (existing_doc,)).fetchone()[0]
        if batch_count:
            return {'ok': False, 'error': 'This image has already been imported', 'doc_id': existing_doc}
        conn.execute('DELETE FROM extracted_txns WHERE doc_id=?', (existing_doc,))
        conn.execute('DELETE FROM import_batches WHERE source_document_id=?', (existing_doc,))
        conn.execute('DELETE FROM source_documents WHERE id=?', (existing_doc,))
        conn.commit()

    upload_dir = ensure_upload_dir()
    doc_dir = upload_dir / fingerprint
    doc_dir.mkdir(exist_ok=True)
    file_path = doc_dir / safe_name
    file_path.write_bytes(raw)

    doc_id = store.add_source_document(conn,
        kind=kind, source_key=source_key, fingerprint=fingerprint,
        provider=provider, upload_path=str(file_path))

    try:
        raw_rows = extract_transactions(file_path)
    except Exception as e:
        store.update_source_document(conn, doc_id, status='failed', error=str(e))
        return {'ok': False, 'error': f'Extraction failed: {e}', 'doc_id': doc_id}

    rows = []
    for i, r in enumerate(raw_rows):
        amount = _to_int_amount(r.get('amount_rp'))
        direction = 'in' if str(r.get('direction', 'in')).strip().lower() == 'in' else 'out'
        occurred = (r.get('occurred_at') or '').strip()
        if occurred and not re.match(r'^\d{4}-\d{2}-\d{2}', occurred):
            occurred = ''
        rows.append({
            'provider': provider,
            'src_txn_id': f'{fingerprint}:{i}',
            'occurred_at': occurred,
            'description': (r.get('description') or '').strip(),
            'raw_description': (r.get('description') or '').strip(),
            'transaction_type': '',
            'merchant': (r.get('merchant') or '').strip(),
            'recipient': (r.get('recipient') or '').strip(),
            'direction': direction,
            'principal_amount': amount,
            'fee_amount': 0,
            'total_amount': amount,
            'bank_ref': (r.get('reference') or '').strip(),
            'source_page': 0,
            'raw1': json.dumps(r, ensure_ascii=False),
            'raw2': '',
            'parser_version': 'screenshot-vision-1.0',
        })

    store.update_source_document(conn, doc_id, status='parsed',
        parser_version='screenshot-vision-1.0',
        preview=json.dumps({'row_count': len(rows), 'account_name': 'Screenshot',
                            'period': ''}, ensure_ascii=False))

    if not rows:
        store.update_source_document(conn, doc_id, status='failed',
            error='No transaction rows found in the screenshot')
        return {'ok': False, 'error': 'No transaction rows found', 'doc_id': doc_id}

    batch_id = store.add_import_batch(conn, doc_id)
    _store_extracted_rows(conn, batch_id, doc_id, rows, provider)

    rows_meta = _row_flags(conn, batch_id)
    return {
        'ok': True,
        'doc_id': doc_id,
        'batch_id': batch_id,
        'account_id': None,
        'row_count': len(rows),
        'skipped_rows': 0,
        'account_name': 'Screenshot',
        'period': '',
        'fingerprint': fingerprint,
        'warnings': [],
        'parser_version': 'screenshot-vision-1.0',
        'provider': provider,
        'confirm_pending': True,
        'rows': rows_meta['rows'],
        'new_rows': rows_meta['new_rows'],
        'duplicate_rows': rows_meta['duplicate_rows'],
        'overlap_rows': rows_meta['overlap_rows'],
    }


def _to_int_amount(value) -> int:
    """Coerce a vision-read amount ('12.345,67', '12345', 12345.0) to int."""
    try:
        if value in (None, ''):
            return 0
        text = str(value).replace('.', '').replace(',', '.')
        return int(float(text))
    except (ValueError, TypeError):
        return 0


def _store_extracted_rows(conn: sqlite3.Connection,
                           batch_id: int, doc_id: int, rows: list[dict],
                           provider: str = 'gopay') -> None:
    """Insert extracted rows with duplicate detection."""
    from datetime import datetime
    for i, row in enumerate(rows):
        # Normalize amounts
        principal = row.get('principal_amount', 0)
        fee = row.get('fee_amount', 0)
        total = row.get('total_amount', 0)
        if total == 0:
            total = principal + fee

        # Check duplicates
        dup_status, dup_id = check_duplicates(conn,
            source_key=f"{provider}:{row.get('src_txn_id', '')}",
            account_hint=provider,
            occurred_at=row.get('occurred_at', ''),
            total_amount=total,
            direction=row.get('direction', 'out'))

        store.add_extracted_rows(conn, batch_id, doc_id, [{
            **row,
            'ext_index': i,
            'principal_amount': principal,
            'fee_amount': fee,
            'total_amount': total,
            'dup_status': dup_status,
            'duplicate_of': dup_id,
        }])


def _guess_account_id(conn: sqlite3.Connection, batch_id: int) -> int | None:
    """Guess the target account for a batch from its provider/phone suffix."""
    batch = store.get_import_batch(conn, batch_id)
    if not batch:
        return None
    doc = store.get_source_document(conn, batch['source_document_id']) or {}
    hint = doc.get('provider') or ''
    # Prefer an account whose provider matches the doc provider (e.g. gopay)
    for a in store.list_accounts(conn, active_only=True):
        if hint and a.get('provider') and hint.lower() in a['provider'].lower():
            return a['id']
    return None


def confirm_import(conn: sqlite3.Connection, batch_id: int,
                   account_id: int | None = None) -> dict:
    """Confirm an import: move extracted rows → ledger, run categorization.

    account_id (optional) is stamped on the derived ledger rows; defaults to
    the batch's source document account guess when the caller doesn't know.

    Returns {ok, ledger_count, categorization_results} or {ok:False, error}.
    """
    batch = store.get_import_batch(conn, batch_id)
    if not batch:
        return {'ok': False, 'error': f'Batch {batch_id} not found'}
    if batch['state'] not in ('preview',):
        return {'ok': False, 'error': f'Batch is in state "{batch["state"]}", cannot confirm'}

    extracted = store.list_extracted_by_batch(conn, batch_id)

    # Filter out exact duplicates
    new_rows = [r for r in extracted if r['dup_status'] != 'exact_dup']

    # Use the batch's guessed account when the caller passes none
    if account_id is None:
        account_id = _guess_account_id(conn, batch_id)

    # Create ledger rows
    ledger_rows = []
    ext_ids = []
    for r in new_rows:
        # Determine nature from direction + amounts
        nature = _infer_nature(r)

        ledger_rows.append((
            r['id'],   # ext_id
            account_id,
            r['total_amount'],
            r['direction'],
            nature,
            None,      # category_id
            '',        # notes
            'none',    # confidence
            '',        # confidence_reason
            '{}',      # evidence_json
            'ok' if nature != 'needs_review' else 'uncategorized',
            'confirmed' if nature != 'needs_review' else 'needs_review',
        ))
        ext_ids.append(r['id'])

    if ledger_rows:
        ledger_ids = store.add_ledger_rows_batch(conn, ledger_rows)

    # Run categorization
    new_extracted = [e for e in store.list_extracted_by_batch(conn, batch_id) if e['dup_status'] != 'exact_dup']
    cat_results = categorize_batch(conn, new_extracted)

    # Apply the categorization results to the just-created ledger rows
    # (matched by ext_id -> ledger row created in the same order).
    applied = 0
    for i, res in enumerate(cat_results):
        if i >= len(ledger_ids):
            break
        if res.get('category_id'):
            store.update_ledger(conn, ledger_ids[i],
                category_id=res['category_id'],
                nature=res['nature'],
                confidence=res['confidence'],
                confidence_reason=res['reason'],
                review_status='ok' if res['confidence'] in ('high', 'medium') else 'uncategorized',
                txn_status='confirmed' if res['confidence'] in ('high', 'medium') else 'needs_review')
            applied += 1

    # Update batch state
    stats = {
        'total_rows': len(extracted),
        'new_rows': len(new_rows),
        'duplicate_rows': len(extracted) - len(new_rows),
        'categorized': sum(1 for r in cat_results if r.get('category_id')),
        'applied': applied,
    }
    store.update_import_batch(conn, batch_id,
        state='committed',
        stats_json=json.dumps(stats, ensure_ascii=False))

    # Audit
    store.add_audit(conn,
        action='import_confirm',
        entity='import_batch',
        entity_id=batch_id,
        after_json=json.dumps(stats))

    return {'ok': True, **stats}


def delete_import(conn: sqlite3.Connection, batch_id: int) -> dict:
    """Delete an import: reverse derived ledger rows, remove file, audit.

    Returns {ok, reversed_count} or {ok:False, error}.
    """
    batch = store.get_import_batch(conn, batch_id)
    if not batch:
        return {'ok': False, 'error': f'Batch {batch_id} not found'}

    doc_id = batch['source_document_id']

    # Count and reverse derived ledger rows
    reversed_count = 0
    extracted = store.list_extracted_by_batch(conn, batch_id)
    for ext in extracted:
        # Delete any linked transfers first
        transfer = store.get_transfer_for_ledger(conn, ext['id'])
        if transfer:
            store.delete_transfer(conn, transfer['id'])

    # CASCADE handles ledger_txns (via ext_id FK) and corrections
    # CASCADE handles extracted_txns (via batch_id FK)

    # Update batch
    store.update_import_batch(conn, batch_id, state='cancelled')

    # Remove uploaded file
    doc = store.get_source_document(conn, doc_id)
    if doc and doc.get('upload_path'):
        upload_path = Path(doc['upload_path'])
        if upload_path.exists():
            shutil.rmtree(upload_path.parent, ignore_errors=True)

    # Audit
    store.add_audit(conn,
        action='import_delete',
        entity='import_batch',
        entity_id=batch_id,
        before_json=json.dumps({'doc_id': doc_id}))

    return {'ok': True, 'reversed_count': reversed_count}


def _infer_nature(row: dict) -> str:
    """Infer transaction nature from extracted row data."""
    desc = (row.get('description', '') + ' ' + row.get('merchant', '')).lower()
    direction = row.get('direction', 'out')
    fee = row.get('fee_amount', 0)

    # Top-up detection
    if any(kw in desc for kw in ['top up', 'topup', 'isi ulang', 'isi saldo']):
        return 'top_up'

    # Fee detection
    if fee > 0:
        return 'expense'  # fee = expense with separate fee row

    # Cashback/reward
    if any(kw in desc for kw in ['cashback', 'reward', 'coins']):
        return 'cashback'

    # Refund
    if any(kw in desc for kw in ['refund', 'kembalian']):
        return 'refund'

    # Needs review
    return 'needs_review'


def preview_import(conn: sqlite3.Connection, batch_id: int) -> dict:
    """Return preview data for a batch (before confirmation)."""
    batch = store.get_import_batch(conn, batch_id)
    if not batch:
        return {'ok': False, 'error': f'Batch {batch_id} not found'}

    extracted = store.list_extracted_by_batch(conn, batch_id)
    return {
        'ok': True,
        'batch_id': batch_id,
        'state': batch['state'],
        'doc_kind': batch['kind'],
        'provider': batch['provider'],
        'rows': extracted,
        'summary': {
            'total': len(extracted),
            'new': sum(1 for r in extracted if r['dup_status'] == 'unique'),
            'exact_dup': sum(1 for r in extracted if r['dup_status'] == 'exact_dup'),
            'possible_dup': sum(1 for r in extracted if r['dup_status'] == 'possible'),
        },
    }


def add_manual_tx(conn: sqlite3.Connection, *,
                  direction: str, amount: Any, occurred_at: str,
                  description: str, account_id: int | None = None,
                  category_id: int | None = None,
                  notes: str = '') -> dict:
    """Record a transaction entered by hand (email notifications can be missed).

    Creates a 'manual' source document + batch + one extracted/ledger row and
    stamps the audit trail. The deterministic categorizer runs when the caller
    does not supply an explicit category so the row is either confidently
    bucketed or flagged for review - never silently dropped.

    Returns {ok, ledger_id, ext_id} or {ok:False, error}.
    """
    direction = 'in' if str(direction).lower() in (
        'in', 'income', 'credit', 'masuk', 'pemasukan', 'dana masuk') else 'out'
    try:
        amount = int(float(str(amount).replace('.', '').replace(',', '')))
    except (TypeError, ValueError):
        return {'ok': False, 'error': 'Invalid amount'}
    if amount <= 0:
        return {'ok': False, 'error': 'Amount must be greater than 0'}
    description = (description or '').strip()[:200]
    if not description:
        return {'ok': False, 'error': 'Description is required'}
    occurred_at = (occurred_at or '').strip()
    if not occurred_at:
        return {'ok': False, 'error': 'Date is required'}

    stamp = datetime.now().strftime('%Y%m%d%H%M%S%f')
    doc_id = store.add_source_document(
        conn, kind='manual', source_key=f'manual:{stamp}',
        fingerprint=f'manual:{stamp}', provider='manual', parser_version='manual',
        email_subject='Manual entry',
        email_received_at=occurred_at,
        preview=f'{direction.upper()} {description}')
    store.update_source_document(conn, doc_id, status='parsed', parser_version='manual')
    batch_id = store.add_import_batch(conn, doc_id)

    row = {
        'provider': 'manual', 'src_txn_id': f'M{stamp}',
        'description': description,
        'raw_description': f'Manual entry: {description}',
        'transaction_type': 'expense' if direction == 'out' else 'income',
        'merchant': '', 'recipient': '', 'direction': direction,
        'principal_amount': amount, 'fee_amount': 0, 'total_amount': amount,
        'currency': 'IDR', 'occurred_at': occurred_at, 'bank_ref': '',
        'parser_version': 'manual', 'dup_status': 'unique',
    }
    [ext_id] = store.add_extracted_rows(conn, batch_id, doc_id, [row])

    ledger_id = store.add_ledger_row(
        conn, ext_id, account_id=account_id, amount=amount,
        direction=direction,
        nature='income' if direction == 'in' else 'expense',
        category_id=category_id,
        notes=(notes or '')[:200],
        confidence='high' if category_id else 'medium',
        confidence_reason='Manual entry',
        evidence_json=json.dumps({'source': 'manual'}, ensure_ascii=False),
        review_status='ok' if category_id else 'review',
        txn_status='confirmed' if category_id else 'needs_review')

    if category_id is None:
        res = categorize_batch(conn, [row])[0]
        if res.get('category_id') and res.get('confidence') in ('high', 'medium'):
            store.update_ledger(conn, ledger_id,
                category_id=res['category_id'], nature=res['nature'],
                confidence=res['confidence'], confidence_reason=res['reason'],
                review_status='ok', txn_status='confirmed')
        else:
            store.update_ledger(conn, ledger_id,
                confidence_reason='Manual entry - no rule matched',
                review_status='uncategorized', txn_status='needs_review')

    store.update_import_batch(
        conn, batch_id, state='committed',
        stats_json=json.dumps({'total_rows': 1, 'new_rows': 1,
                               'duplicate_rows': 0, 'categorized': 1,
                               'applied': 1}, ensure_ascii=False))
    store.add_audit(conn, action='manual_entry', entity='ledger_txns',
                    entity_id=ledger_id,
                    after_json=json.dumps(
                        {'description': description, 'amount': amount,
                         'direction': direction}, ensure_ascii=False))

    return {'ok': True, 'ledger_id': ledger_id, 'ext_id': ext_id}
