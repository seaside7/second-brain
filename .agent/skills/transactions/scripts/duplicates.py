"""Duplicate detection for transaction imports.

Checks both exact duplicates (same source_key/fingerprint) and possible
duplicates (same amount + day + fuzzy description).
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from typing import Optional


def check_duplicates(conn: sqlite3.Connection, *,
                      source_key: str = '',
                      fingerprint: str = '',
                      account_hint: str = '',
                      occurred_at: str = '',
                      total_amount: int = 0,
                      direction: str = 'out') -> tuple[str, Optional[int]]:
    """Check for duplicates against existing extracted rows.

    Returns (dup_status, duplicate_of_id):
        ('unique', None)           - no duplicates
        ('exact_dup', ledger_id)   - already imported (skip)
        ('possible', ledger_id)    - possible duplicate (flag for review)
    """
    # Exact: same source_key (txn ID)
    if source_key:
        r = conn.execute(
            "SELECT e.id FROM extracted_txns e "
            "JOIN import_batches b ON b.id=e.batch_id "
            "WHERE e.src_txn_id=? AND e.dup_status != 'exact_dup' "
            "LIMIT 1",
            (source_key.split(':')[-1] if ':' in source_key else source_key,)
        ).fetchone()
        if r:
            return ('exact_dup', r[0])

    # Exact: same document fingerprint
    if fingerprint:
        r = conn.execute(
            "SELECT e.id FROM extracted_txns e "
            "JOIN source_documents d ON d.id=e.doc_id "
            "WHERE d.fingerprint=? AND e.dup_status != 'exact_dup' LIMIT 1",
            (fingerprint,)
        ).fetchone()
        if r:
            return ('exact_dup', r[0])

    # Possible: same amount + same day
    if total_amount > 0 and occurred_at:
        day = occurred_at[:10]  # YYYY-MM-DD
        r = conn.execute(
            "SELECT e.id, e.description FROM extracted_txns e "
            "WHERE e.total_amount=? AND e.occurred_at LIKE ? "
            "AND e.direction=? AND e.dup_status != 'exact_dup' "
            "LIMIT 1",
            (total_amount, f'{day}%', direction)
        ).fetchone()
        if r:
            return ('possible', r[0])

    return ('unique', None)


def find_matching_transfer_candidates(conn: sqlite3.Connection, *,
                                       ledger_id: int,
                                       from_date: str = '',
                                       to_date: str = '',
                                       principal_amount: int = 0,
                                       direction: str = 'out') -> list[dict]:
    """Find potential transfer match candidates for a ledger row.

    Returns list of candidate ledger dicts with a score.
    """
    # We need opposite direction
    opp_dir = 'in' if direction == 'out' else 'out'

    conds = ["l.direction=?", "l.id!=?", "l.nature IN ('internal_transfer','top_up','expense')"]
    params: list = [opp_dir, ledger_id]

    if principal_amount > 0:
        conds.append("l.amount=?")
        params.append(principal_amount)

    if from_date:
        conds.append("l.created_at>=?")
        params.append(from_date)
    if to_date:
        conds.append("l.created_at<=?")
        params.append(to_date)

    # Exclude already-matched
    conds.append(
        "NOT EXISTS (SELECT 1 FROM transfers t "
        "WHERE t.from_ledger_id=l.id OR t.to_ledger_id=l.id)")

    where = "WHERE " + " AND ".join(conds)
    sql = (
        "SELECT l.*, e.description, e.merchant, e.src_txn_id, e.provider "
        "FROM ledger_txns l "
        "LEFT JOIN extracted_txns e ON e.id=l.ext_id "
        f"{where} LIMIT 20")

    candidates = [dict(r) for r in conn.execute(sql, params).fetchall()]

    # Score each candidate
    source = conn.execute(
        "SELECT e.description, e.merchant FROM ledger_txns l "
        "JOIN extracted_txns e ON e.id=l.ext_id WHERE l.id=?",
        (ledger_id,)
    ).fetchone()

    source_desc = (source[0] if source else '') or ''
    source_merchant = (source[1] if source else '') or ''

    for c in candidates:
        score = 0.0
        c_desc = c.get('description', '') or ''
        c_merchant = c.get('merchant', '') or ''

        # Amount match (exact = +5)
        if c.get('amount') == principal_amount:
            score += 5.0

        # Description similarity (simple token overlap)
        tokens_src = set(re.findall(r'\w+', source_desc.lower()))
        tokens_cand = set(re.findall(r'\w+', c_desc.lower()))
        if tokens_src and tokens_cand:
            overlap = len(tokens_src & tokens_cand) / max(len(tokens_src | tokens_cand), 1)
            score += overlap * 3.0

        # Provider match
        c_provider = c.get('provider', '') or ''
        if source_merchant.lower() == c_provider.lower():
            score += 2.0

        c['score'] = round(score, 2)

    # Sort by score descending
    candidates.sort(key=lambda x: x['score'], reverse=True)
    return candidates
