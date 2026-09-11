"""Transfer reconciliation: match opposite-direction ledger rows.

Handles: bank↔GoPay top-ups, bank-to-bank transfers, delayed GoPay
reconciliation. Scored matching with one-to-one enforcement via
transfers table UNIQUE constraints.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

import store


def find_transfer_candidates(conn: sqlite3.Connection, *,
                              ledger_id: int,
                              window_days: int = 7) -> list[dict]:
    """Find candidates that could be the other side of a transfer."""
    row = store.get_ledger(conn, ledger_id)
    if not row:
        return []

    opp_dir = 'in' if row['direction'] == 'out' else 'out'
    total = row['amount']
    created = row.get('created_at', '')

    conds = [
        "l.direction = ?",
        "l.id != ?",
        "l.nature IN ('internal_transfer','top_up','expense','income')",
        "l.id NOT IN (SELECT from_ledger_id FROM transfers UNION SELECT to_ledger_id FROM transfers)",
    ]
    params: list = [opp_dir, ledger_id]

    if total > 0:
        conds.append("l.amount = ?")
        params.append(total)

    if created:
        try:
            dt = datetime.fromisoformat(created)
            window_start = (dt - timedelta(days=window_days)).isoformat()
            window_end = (dt + timedelta(days=window_days)).isoformat()
            conds.append("l.created_at BETWEEN ? AND ?")
            params.extend([window_start, window_end])
        except ValueError:
            pass

    where = "WHERE " + " AND ".join(conds)
    sql = (
        "SELECT l.*, e.description, e.merchant, e.src_txn_id, e.provider, "
        "e.phone_suffix "
        "FROM ledger_txns l "
        "LEFT JOIN extracted_txns e ON e.id = l.ext_id "
        f"{where} LIMIT 20"
    )

    candidates = [dict(r) for r in conn.execute(sql, params).fetchall()]

    # Score each candidate
    source_desc = (row.get('description', '') or '').lower()
    source_merchant = (row.get('merchant', '') or '').lower()
    source_provider = (row.get('account_provider', '') or '').lower()

    for c in candidates:
        c_desc = (c.get('description', '') or '').lower()
        c_merchant = (c.get('merchant', '') or '').lower()
        c_provider = (c.get('provider', '') or '').lower()

        score = 0.0

        # Amount match (exact = +5)
        if c.get('amount') == total:
            score += 5.0

        # Provider pair (bank↔gopay = +3)
        if source_provider and c_provider:
            pair = tuple(sorted([source_provider, c_provider]))
            if pair in [('bca','gopay'), ('bni','gopay'), ('bri','gopay'),
                        ('mandiri','gopay'), ('bca','bni'), ('bca','bri'),
                        ('bni','bri')]:
                score += 3.0

        # Description token overlap
        tokens_src = set(re.findall(r'\w+', source_desc))
        tokens_cand = set(re.findall(r'\w+', c_desc))
        if tokens_src and tokens_cand:
            overlap = len(tokens_src & tokens_cand) / max(len(tokens_src | tokens_cand), 1)
            score += overlap * 2.0

        # Phone suffix match
        if row.get('phone_suffix') and c.get('phone_suffix'):
            if row['phone_suffix'] == c['phone_suffix']:
                score += 1.0

        c['score'] = round(score, 2)

    candidates.sort(key=lambda x: x['score'], reverse=True)
    return candidates


def confirm_transfer(conn: sqlite3.Connection, *,
                      from_ledger_id: int,
                      to_ledger_id: int,
                      principal_amount: int,
                      fee_ledger_id: int | None = None,
                      matched_by: str = 'manual',
                      score: float = 0.0) -> dict:
    """Confirm a transfer between two ledger rows.

    Creates transfer record and updates both ledger rows' txn_status.
    """
    # Validate one-to-one
    existing_from = store.get_transfer_for_ledger(conn, from_ledger_id)
    if existing_from:
        return {'ok': False, 'error': 'Source ledger already linked'}
    existing_to = store.get_transfer_for_ledger(conn, to_ledger_id)
    if existing_to:
        return {'ok': False, 'error': 'Target ledger already linked'}

    # Create transfer
    transfer_id = store.add_transfer(conn,
        from_ledger_id=from_ledger_id,
        to_ledger_id=to_ledger_id,
        principal_amount=principal_amount,
        fee_ledger_id=fee_ledger_id,
        status='confirmed',
        matched_by=matched_by,
        score=score)

    # Update ledger statuses
    store.update_ledger(conn, from_ledger_id, txn_status='matched')
    store.update_ledger(conn, to_ledger_id, txn_status='matched')

    # Audit
    store.add_audit(conn,
        action='transfer_confirm',
        entity='transfers',
        entity_id=transfer_id,
        after_json=f'{{"from": {from_ledger_id}, "to": {to_ledger_id}}}')

    return {'ok': True, 'transfer_id': transfer_id}


def suggest_transfer(conn: sqlite3.Connection, *,
                      from_ledger_id: int,
                      to_ledger_id: int,
                      principal_amount: int,
                      score: float = 0.0) -> dict:
    """Suggest a transfer (for review queue)."""
    existing_from = store.get_transfer_for_ledger(conn, from_ledger_id)
    if existing_from:
        return {'ok': False, 'error': 'Source ledger already linked'}
    existing_to = store.get_transfer_for_ledger(conn, to_ledger_id)
    if existing_to:
        return {'ok': False, 'error': 'Target ledger already linked'}

    transfer_id = store.add_transfer(conn,
        from_ledger_id=from_ledger_id,
        to_ledger_id=to_ledger_id,
        principal_amount=principal_amount,
        status='suggested',
        matched_by='auto',
        score=score)

    return {'ok': True, 'transfer_id': transfer_id}


def reject_transfer(conn: sqlite3.Connection, transfer_id: int) -> dict:
    """Reject (dismiss) a suggested transfer."""
    store.update_transfer(conn, transfer_id, status='rejected')
    return {'ok': True}


def unlink_transfer(conn: sqlite3.Connection, transfer_id: int) -> dict:
    """Remove a transfer link and restore both ledger rows."""
    transfer = conn.execute(
        "SELECT * FROM transfers WHERE id=?", (transfer_id,)
    ).fetchone()
    if not transfer:
        return {'ok': False, 'error': 'Transfer not found'}

    from_id = transfer['from_ledger_id']
    to_id = transfer['to_ledger_id']

    store.delete_transfer(conn, transfer_id)

    # Reset ledger statuses
    store.update_ledger(conn, from_id, txn_status='confirmed')
    store.update_ledger(conn, to_id, txn_status='confirmed')

    store.add_audit(conn,
        action='transfer_unlink',
        entity='transfers',
        entity_id=transfer_id,
        before_json=f'{{"from": {from_id}, "to": {to_id}}}')

    return {'ok': True}
