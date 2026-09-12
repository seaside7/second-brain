"""Transaction API routes for the dashboard server.

Mirrors the coding_agent delegate pattern: exposes ``route_get(handler)``
and ``route_post(handler)`` called from server.py dispatch.

All routes are personal-only (reject samudera).
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

# Add the skill scripts to sys.path so we can import the transactions package
_SKILL_DIR = Path(__file__).resolve().parent.parent / '.agent' / 'skills' / 'transactions' / 'scripts'
if str(_SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILL_DIR))

try:
    from schema import connect, ensure_tables, DB_PATH
    from store import (list_accounts, get_account, add_account, update_account,
                       list_ledger, get_ledger, update_ledger, count_ledger,
                       list_transfers, get_transfer_for_ledger,
                       list_categories, list_rules, add_rule, deactivate_rule,
                       list_import_batches, get_import_batch,
                       add_audit, list_audit, fmt_idr)
    from import_engine import upload_pdf, confirm_import, delete_import, preview_import
    from categorize import apply_correction
    from reconcile import (find_transfer_candidates, confirm_transfer,
                           suggest_transfer, reject_transfer, unlink_transfer)
    from reports import overview, spending_breakdown, cashflow, fees_total
    from gmail_sync import sync_gmail
    from scheduler import TransactionScheduler
    from reprocess import reprocess as reprocess_engine
    _IMPORTS_OK = True
except ImportError as exc:
    _IMPORTS_OK = False
    _IMPORT_ERR = str(exc)


def _get_handler_attr(handler, name, default=None):
    """Safe attribute access on the handler."""
    return getattr(handler, name, default)


def _request_ws(handler) -> str | None:
    """Get workspace from the handler."""
    return _get_handler_attr(handler, 'ws', None)


def _send_json(handler, status: int, body: str) -> None:
    """Send JSON response."""
    handler._send_json(status, body)


def _read_body(handler) -> dict:
    """Read and parse JSON body from request."""
    length = int(handler.headers.get('Content-Length', 0))
    if length == 0:
        return {}
    raw = handler.rfile.read(length)
    return json.loads(raw.decode('utf-8'))


def _parse_qs(handler) -> dict:
    """Parse query string from request URL."""
    return parse_qs(urlsplit(handler.path).query)


def _ok(handler, data: Any = None) -> None:
    """Send 200 OK response."""
    _send_json(handler, 200, json.dumps({'ok': True, **(data if isinstance(data, dict) else {'data': data})},
                                        ensure_ascii=False))


def _err(handler, status: int, message: str) -> None:
    """Send error response."""
    _send_json(handler, status, json.dumps({'ok': False, 'error': message}))


def _get_db(handler):
    """Get database connection (personal workspace only)."""
    ws = _request_ws(handler)
    if ws == 'samudera':
        return None
    conn = connect()
    ensure_tables(conn)
    return conn


# ── GET routes ───────────────────────────────────────────────────────────

def route_get(handler) -> None:
    """Route GET requests to the appropriate handler."""
    if not _IMPORTS_OK:
        _err(handler, 500, f'Transactions module not available: {_IMPORT_ERR}')
        return

    path = handler.path.split('?')[0]
    qs = _parse_qs(handler)

    try:
        if path == '/api/transactions/overview':
            _handle_overview(handler, qs)
        elif path == '/api/transactions/list':
            _handle_list(handler, qs)
        elif path.startswith('/api/transactions/'):
            _id = path.split('/')[-1]
            if _id.isdigit():
                _handle_detail(handler, int(_id))
            elif _id == 'spending':
                _handle_spending(handler, qs)
            elif _id == 'transfers':
                _handle_transfers_list(handler, qs)
            elif _id == 'accounts':
                _handle_accounts_list(handler)
            elif _id == 'review':
                _handle_review_list(handler, qs)
            elif _id == 'categories':
                _handle_categories_list(handler)
            elif _id == 'imports':
                _handle_imports_list(handler)
            elif _id == 'rules':
                _handle_rules_list(handler)
            elif _id == 'audit':
                _handle_audit_list(handler, qs)
            else:
                _err(handler, 404, 'Not found')
        else:
            _err(handler, 404, 'Not found')
    except Exception as e:
        traceback.print_exc()
        _err(handler, 500, f'Server error: {e}')


def _handle_overview(handler, qs: dict) -> None:
    conn = _get_db(handler)
    if not conn:
        _err(handler, 403, 'Not available in samudera mode')
        return
    period = qs.get('period', ['current_month'])[0]
    try:
        data = overview(conn, period=period)
        _ok(handler, data)
    finally:
        conn.close()


def _handle_list(handler, qs: dict) -> None:
    conn = _get_db(handler)
    if not conn:
        _err(handler, 403, 'Not available in samudera mode')
        return
    try:
        limit = int(qs.get('limit', ['200'])[0])
        offset = int(qs.get('offset', ['0'])[0])
        rows = list_ledger(conn,
            nature=qs.get('nature', [None])[0],
            review_status=qs.get('review', [None])[0],
            txn_status=qs.get('status', [None])[0],
            account_id=int(qs['account'][0]) if 'account' in qs else None,
            from_date=qs.get('from', [None])[0],
            to_date=qs.get('to', [None])[0],
            search=qs.get('q', [None])[0],
            limit=min(limit, 500),
            offset=offset)
        total = count_ledger(conn)
        _ok(handler, {'rows': rows, 'total': total, 'limit': limit, 'offset': offset})
    finally:
        conn.close()


def _handle_detail(handler, txn_id: int) -> None:
    conn = _get_db(handler)
    if not conn:
        _err(handler, 403, 'Not available in samudera mode')
        return
    try:
        row = get_ledger(conn, txn_id)
        if not row:
            _err(handler, 404, 'Transaction not found')
            return
        transfer = get_transfer_for_ledger(conn, txn_id)
        _ok(handler, {'row': row, 'transfer': transfer})
    finally:
        conn.close()


def _handle_spending(handler, qs: dict) -> None:
    conn = _get_db(handler)
    if not conn:
        _err(handler, 403, 'Not available in samudera mode')
        return
    try:
        data = spending_breakdown(conn,
            from_date=qs.get('from', [None])[0],
            to_date=qs.get('to', [None])[0])
        _ok(handler, data)
    finally:
        conn.close()


def _handle_transfers_list(handler, qs: dict) -> None:
    conn = _get_db(handler)
    if not conn:
        _err(handler, 403, 'Not available in samudera mode')
        return
    try:
        rows = list_transfers(conn, status=qs.get('status', [None])[0])
        _ok(handler, {'rows': rows})
    finally:
        conn.close()


def _handle_accounts_list(handler) -> None:
    conn = _get_db(handler)
    if not conn:
        _err(handler, 403, 'Not available in samudera mode')
        return
    try:
        accounts = list_accounts(conn, active_only=False)
        _ok(handler, {'accounts': accounts})
    finally:
        conn.close()


def _handle_review_list(handler, qs: dict) -> None:
    conn = _get_db(handler)
    if not conn:
        _err(handler, 403, 'Not available in samudera mode')
        return
    try:
        days = int(qs.get('days', ['7'])[0])
        rows = list_ledger(conn, review_status='uncategorized', limit=100)
        _ok(handler, {'rows': rows, 'count': len(rows)})
    finally:
        conn.close()


def _handle_imports_list(handler) -> None:
    conn = _get_db(handler)
    if not conn:
        _err(handler, 403, 'Not available in samudera mode')
        return
    try:
        batches = list_import_batches(conn, limit=50)
        _ok(handler, {'batches': batches})
    finally:
        conn.close()


def _handle_categories_list(handler) -> None:
    conn = _get_db(handler)
    if not conn:
        _err(handler, 403, 'Not available in samudera mode')
        return
    try:
        _ok(handler, {'categories': list_categories(conn)})
    finally:
        conn.close()


def _handle_rules_list(handler) -> None:
    conn = _get_db(handler)
    if not conn:
        _err(handler, 403, 'Not available in samudera mode')
        return
    try:
        rules = list_rules(conn, active_only=False)
        _ok(handler, {'rules': rules})
    finally:
        conn.close()


def _handle_audit_list(handler, qs: dict) -> None:
    conn = _get_db(handler)
    if not conn:
        _err(handler, 403, 'Not available in samudera mode')
        return
    try:
        limit = int(qs.get('limit', ['100'])[0])
        rows = list_audit(conn,
            entity=qs.get('entity', [None])[0],
            limit=min(limit, 500))
        _ok(handler, {'rows': rows})
    finally:
        conn.close()


# ── POST routes ──────────────────────────────────────────────────────────

def route_post(handler) -> None:
    """Route POST requests to the appropriate handler."""
    if not _IMPORTS_OK:
        _err(handler, 500, f'Transactions module not available: {_IMPORT_ERR}')
        return

    path = handler.path.split('?')[0]
    body = _read_body(handler)

    try:
        if path == '/api/transactions/upload':
            _handle_upload(handler, body)
        elif path == '/api/transactions/import/preview':
            _handle_preview(handler, body)
        elif path == '/api/transactions/import/confirm':
            _handle_confirm(handler, body)
        elif path.startswith('/api/transactions/import/') and path.endswith('/delete'):
            batch_id = int(path.split('/')[-2])
            _handle_delete(handler, batch_id)
        elif path == '/api/transactions/sync/gmail':
            _handle_sync_gmail(handler)
        elif path == '/api/transactions/reprocess':
            _handle_reprocess(handler, body)
        elif path == '/api/transactions/categorize':
            _handle_categorize(handler, body)
        elif path.startswith('/api/transactions/') and path.endswith('/edit'):
            txn_id = int(path.split('/')[-2])
            _handle_edit(handler, txn_id, body)
        elif path == '/api/transactions/review/respond':
            _handle_review_respond(handler, body)
        elif path == '/api/transactions/transfers/match':
            _handle_transfer_match(handler, body)
        elif path == '/api/transactions/transfers/reject':
            _handle_transfer_reject(handler, body)
        elif path == '/api/transactions/transfers/link':
            _handle_transfer_link(handler, body)
        elif path == '/api/transactions/transfers/unlink':
            _handle_transfer_unlink(handler, body)
        elif path == '/api/transactions/rules':
            _handle_create_rule(handler, body)
        elif path == '/api/transactions/accounts':
            _handle_create_account(handler, body)
        else:
            _err(handler, 404, 'Not found')
    except Exception as e:
        traceback.print_exc()
        _err(handler, 500, f'Server error: {e}')


def _handle_upload(handler, body: dict) -> None:
    conn = _get_db(handler)
    if not conn:
        _err(handler, 403, 'Not available in samudera mode')
        return
    filename = body.get('filename', '')
    b64data = body.get('data', '')
    if not filename or not b64data:
        _err(handler, 400, 'Missing filename or data')
        return
    try:
        result = upload_pdf(conn, filename=filename, b64data=b64data)
        if result.get('ok'):
            _ok(handler, result)
        else:
            _err(handler, 400, result.get('error', 'Upload failed'))
    finally:
        conn.close()


def _handle_preview(handler, body: dict) -> None:
    conn = _get_db(handler)
    if not conn:
        _err(handler, 403, 'Not available in samudera mode')
        return
    batch_id = body.get('batch_id')
    if not batch_id:
        _err(handler, 400, 'Missing batch_id')
        return
    try:
        result = preview_import(conn, int(batch_id))
        if result.get('ok'):
            _ok(handler, result)
        else:
            _err(handler, 400, result.get('error', 'Preview failed'))
    finally:
        conn.close()


def _handle_confirm(handler, body: dict) -> None:
    conn = _get_db(handler)
    if not conn:
        _err(handler, 403, 'Not available in samudera mode')
        return
    batch_id = body.get('batch_id')
    if not batch_id:
        _err(handler, 400, 'Missing batch_id')
        return
    try:
        result = confirm_import(conn, int(batch_id),
                                account_id=int(body['account_id']) if body.get('account_id') else None)
        if result.get('ok'):
            _ok(handler, result)
        else:
            _err(handler, 400, result.get('error', 'Confirm failed'))
    finally:
        conn.close()


def _handle_delete(handler, batch_id: int) -> None:
    conn = _get_db(handler)
    if not conn:
        _err(handler, 403, 'Not available in samudera mode')
        return
    try:
        result = delete_import(conn, batch_id)
        if result.get('ok'):
            _ok(handler, result)
        else:
            _err(handler, 400, result.get('error', 'Delete failed'))
    finally:
        conn.close()


def _handle_sync_gmail(handler) -> None:
    conn = _get_db(handler)
    if not conn:
        _err(handler, 403, 'Not available in samudera mode')
        return
    try:
        result = sync_gmail(conn)
        if result.get('ok'):
            _ok(handler, result)
        else:
            _err(handler, 400, result.get('error', 'Sync failed'))
    finally:
        conn.close()


def _handle_reprocess(handler, body: dict) -> None:
    conn = _get_db(handler)
    if not conn:
        _err(handler, 403, 'Not available in samudera mode')
        return
    try:
        provider = body.get('provider') or None
        dry_run = bool(body.get('dry_run', False))
        result = reprocess_engine(conn, provider=provider, dry_run=dry_run)
        _ok(handler, result)
    finally:
        conn.close()


def _handle_categorize(handler, body: dict) -> None:
    conn = _get_db(handler)
    if not conn:
        _err(handler, 403, 'Not available in samudera mode')
        return
    try:
        # Run categorization on uncategorized/needs-review rows.
        from store import list_ledger
        from categorize import categorize_batch, apply_categorization
        rows = list_ledger(conn, review_status='uncategorized', limit=200)
        if not rows:
            _ok(handler, {'categorized': 0, 'total': 0})
            return

        # Each ledger row already carries the extracted fields we need
        # (description, merchant, recipient, provider, amounts); use its
        # ext_id so categorize_batch returns results keyed by ext_id.
        for r in rows:
            res = categorize_batch(conn, [dict(r, id=r['ext_id'])])[0]
            if res.get('category_id'):
                apply_categorization(conn, res, r['id'])

        _ok(handler, {'categorized': sum(
            1 for r in rows if r.get('category_id')), 'total': len(rows)})
    finally:
        conn.close()


def _handle_edit(handler, txn_id: int, body: dict) -> None:
    conn = _get_db(handler)
    if not conn:
        _err(handler, 403, 'Not available in samudera mode')
        return
    try:
        row = get_ledger(conn, txn_id)
        if not row:
            _err(handler, 404, 'Transaction not found')
            return

        # Apply edits
        fields = {}
        for key in ['nature', 'category_id', 'notes', 'account_id', 'review_status']:
            if key in body:
                fields[key] = body[key]

        # Setting/clearing a category resolves review status automatically.
        if 'category_id' in body:
            fields['review_status'] = 'ok' if body['category_id'] else 'uncategorized'

        if fields:
            update_ledger(conn, txn_id, **fields)
            add_audit(conn,
                action='manual_edit',
                entity='ledger_txns',
                entity_id=txn_id,
                after_json=json.dumps(fields, ensure_ascii=False))

        _ok(handler, {'ledger_id': txn_id})
    finally:
        conn.close()


def _handle_review_respond(handler, body: dict) -> None:
    conn = _get_db(handler)
    if not conn:
        _err(handler, 403, 'Not available in samudera mode')
        return
    txn_id = body.get('id')
    action = body.get('action')  # set-category | apply-once | remember | skip
    if not txn_id or not action:
        _err(handler, 400, 'Missing id or action')
        return
    try:
        if action == 'skip':
            update_ledger(conn, int(txn_id), review_status='ok')
            _ok(handler)
        elif action in ('set-category', 'apply-once', 'remember'):
            category_id = body.get('category_id')
            nature = body.get('nature', 'expense')
            if not category_id:
                _err(handler, 400, 'Missing category_id')
                return
            result = apply_correction(conn,
                ledger_id=int(txn_id),
                field='category_id',
                old_value=None,
                new_value=category_id,
                reason=body.get('reason', ''),
                remember=(action == 'remember'),
                merchant_or_recipient=body.get('merchant_or_recipient', ''),
                category_id=category_id,
                nature=nature)
            _ok(handler, result)
        else:
            _err(handler, 400, f'Unknown action: {action}')
    finally:
        conn.close()


def _handle_transfer_match(handler, body: dict) -> None:
    conn = _get_db(handler)
    if not conn:
        _err(handler, 403, 'Not available in samudera mode')
        return
    from_id = body.get('from')
    to_id = body.get('to')
    if not from_id or not to_id:
        _err(handler, 400, 'Missing from/to')
        return
    try:
        row = get_ledger(conn, int(from_id))
        if not row:
            _err(handler, 404, 'Source not found')
            return
        result = confirm_transfer(conn,
            from_ledger_id=int(from_id),
            to_ledger_id=int(to_id),
            principal_amount=row['amount'],
            matched_by='manual')
        if result.get('ok'):
            _ok(handler, result)
        else:
            _err(handler, 400, result.get('error', 'Match failed'))
    finally:
        conn.close()


def _handle_transfer_reject(handler, body: dict) -> None:
    conn = _get_db(handler)
    if not conn:
        _err(handler, 403, 'Not available in samudera mode')
        return
    transfer_id = body.get('transfer_id')
    if not transfer_id:
        _err(handler, 400, 'Missing transfer_id')
        return
    try:
        result = reject_transfer(conn, int(transfer_id))
        _ok(handler, result)
    finally:
        conn.close()


def _handle_transfer_link(handler, body: dict) -> None:
    conn = _get_db(handler)
    if not conn:
        _err(handler, 403, 'Not available in samudera mode')
        return
    from_id = body.get('from')
    to_id = body.get('to')
    if not from_id or not to_id:
        _err(handler, 400, 'Missing from/to')
        return
    try:
        row = get_ledger(conn, int(from_id))
        if not row:
            _err(handler, 404, 'Source not found')
            return
        result = confirm_transfer(conn,
            from_ledger_id=int(from_id),
            to_ledger_id=int(to_id),
            principal_amount=row['amount'],
            matched_by='manual')
        _ok(handler, result)
    finally:
        conn.close()


def _handle_transfer_unlink(handler, body: dict) -> None:
    conn = _get_db(handler)
    if not conn:
        _err(handler, 403, 'Not available in samudera mode')
        return
    transfer_id = body.get('transfer_id')
    if not transfer_id:
        _err(handler, 400, 'Missing transfer_id')
        return
    try:
        result = unlink_transfer(conn, int(transfer_id))
        _ok(handler, result)
    finally:
        conn.close()


def _handle_create_rule(handler, body: dict) -> None:
    conn = _get_db(handler)
    if not conn:
        _err(handler, 403, 'Not available in samudera mode')
        return
    merchant = body.get('merchant_or_recipient', '')
    category_id = body.get('category_id')
    nature = body.get('nature', 'expense')
    if not merchant or not category_id:
        _err(handler, 400, 'Missing merchant_or_recipient or category_id')
        return
    try:
        rule_id = add_rule(conn,
            merchant_or_recipient=merchant,
            category_id=int(category_id),
            nature=nature)
        _ok(handler, {'rule_id': rule_id})
    finally:
        conn.close()


def _handle_create_account(handler, body: dict) -> None:
    conn = _get_db(handler)
    if not conn:
        _err(handler, 403, 'Not available in samudera mode')
        return
    acc_type = body.get('type', 'bank')
    provider = body.get('provider', '')
    alias = body.get('alias', '')
    masked = body.get('masked', '')
    owner = body.get('owner_name', '')
    if not provider or not alias:
        _err(handler, 400, 'Missing provider or alias')
        return
    # Mask the number if provided (security: never store full)
    if masked and len(masked) > 4:
        masked = '*' * (len(masked) - 4) + masked[-4:]
    try:
        acc_id = add_account(conn,
            type=acc_type, provider=provider, alias=alias,
            masked=masked, owner_name=owner)
        _ok(handler, {'account_id': acc_id})
    finally:
        conn.close()
