"""Duplicate detection for transaction imports.

Checks both exact duplicates (same source_key/fingerprint) and possible
duplicates (same amount + day + fuzzy description).
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from typing import Optional

_REF_RUN_RE = re.compile(r'\b\d{10,16}\b')


def _ref_run(text: str) -> str:
    """First bank-reference run (10-16 digits) in text, or ''."""
    m = _REF_RUN_RE.search(text or '')
    return m.group(0) if m else ''


def _token_set(*parts: str) -> set[str]:
    tokens = set()
    for p in parts:
        for w in re.findall(r'[a-z0-9]{3,}', (p or '').lower()):
            tokens.add(w)
    return tokens


def match_overlap(conn: sqlite3.Connection, cand: dict, *,
                  window_days: int = 1) -> dict:
    """Check a candidate extracted row against committed ledger rows.

    Catches the SAME real-world transaction captured by a second statement or
    an email/notification (cross-source). Compared to check_duplicates this adds
    identity signals beyond amount+day: bank-reference equality, fee-row
    identity, counterparty-token overlap, and same-owner interbank pairing.

    cand expects extracted-row fields: occurred_at, total_amount, direction,
    transaction_type, bank_ref, recipient, merchant, src_txn_id.

    Returns {status, ledger_id, ext_id, reason} where status is
    'identical' | 'likely' | 'possible' | 'different'.
    """
    amount = int(cand.get('total_amount', 0) or 0)
    day = (cand.get('occurred_at') or '')[:10]
    if not amount or not day:
        return {'status': 'different', 'ledger_id': None, 'ext_id': None,
                'reason': 'no amount/date'}

    rows = [dict(r) for r in conn.execute(
        "SELECT l.id AS ledger_id, e.id AS ext_id, e.bank_ref, e.direction, "
        " e.transaction_type, e.recipient, e.merchant, e.provider "
        "FROM ledger_txns l JOIN extracted_txns e ON e.id = l.ext_id "
        "JOIN import_batches b ON b.id = e.batch_id "
        "WHERE b.state = 'committed' AND e.total_amount = ? "
        "  AND date(e.occurred_at) BETWEEN date(?, ?) AND date(?, ?)",
        (amount, day, f'-{int(window_days)} day', day, f'+{int(window_days)} day'))]
    if not rows:
        return {'status': 'different', 'ledger_id': None, 'ext_id': None,
                'reason': 'no overlap'}

    cand_ref = _ref_run(' '.join([
        cand.get('bank_ref', ''), cand.get('recipient', ''),
        cand.get('merchant', '')]))
    cand_type = (cand.get('transaction_type') or '').lower()
    cand_cp = _token_set(cand.get('recipient'), cand.get('merchant'))
    cand_dir = cand.get('direction', 'out')

    # 1. Identical bank reference (strongest signal; survives cross-account).
    for r in rows:
        if cand_ref and (r.get('bank_ref') or '') and (
                _ref_run(r['bank_ref']) == cand_ref):
            return {'status': 'identical', 'ledger_id': r['ledger_id'],
                    'ext_id': r['ext_id'], 'reason': 'bank reference match'}

    # 2. Fee rows: same day + amount + fee type is the row itself.
    if cand_type == 'fee':
        for r in rows:
            if (r.get('transaction_type') or '').lower() == 'fee':
                return {'status': 'identical', 'ledger_id': r['ledger_id'],
                        'ext_id': r['ext_id'], 'reason': 'fee row'}

    # 3. Same-owner interbank: opposite direction + own-name counterparty.
    if cand_cp and {'said', 'iskandar'} & cand_cp:
        for r in rows:
            if r['direction'] != cand_dir:
                return {'status': 'likely', 'ledger_id': r['ledger_id'],
                        'ext_id': r['ext_id'],
                        'reason': 'same-owner interbank transfer'}

    # 4. Same direction + counterparty-token overlap within the window.
    for r in rows:
        cp = _token_set(r.get('recipient'), r.get('merchant'))
        if r['direction'] == cand_dir and cp and (cand_cp & cp):
            return {'status': 'likely', 'ledger_id': r['ledger_id'],
                    'ext_id': r['ext_id'],
                    'reason': 'amount + counterparty tokens'}

    # 5. Amount + day + direction only - weak, flag for review.
    return {'status': 'possible', 'ledger_id': None, 'ext_id': None,
            'reason': 'amount + day only'}


def check_duplicates(conn: sqlite3.Connection, *,
                      source_key: str = '',
                      fingerprint: str = '',
                      account_hint: str = '',
                      occurred_at: str = '',
                      total_amount: int = 0,
                      direction: str = 'out') -> tuple[str, Optional[int]]:
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
