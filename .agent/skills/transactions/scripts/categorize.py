"""Categorization engine: deterministic rules → AI (via model_router).

Phase 1: deterministic rules only (own-account transfers, top-ups, fees,
known merchants, refunds, cashback, repeated rules, Dinda recipient+note).
AI categorization is a separate pass (Phase 4).
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any, Optional

import store

# ── Dinda detection ─────────────────────────────────────────────────────

_DINDA_NAME_RE = re.compile(r'dinda|dindafn|dinda\s*fitri', re.IGNORECASE)

# ── Known merchants (IDR flat fee patterns) ─────────────────────────────
_KNOWN_MERCHANTS = {
    'bca': {'provider': 'bca'},
    'bni': {'provider': 'bni'},
    'bri': {'provider': 'bri'},
    'mandiri': {'provider': 'mandiri'},
    'gopay': {'provider': 'gopay'},
    'shopeepay': {'provider': 'shopeepay'},
    'ovo': {'provider': 'ovo'},
    'dana': {'provider': 'dana'},
    'linkaja': {'provider': 'linkaja'},
    'qris': {'provider': 'qris'},
}

# ── Fee keywords (Bahasa + English) ────────────────────────────────────
_FEE_KEYWORDS = [
    'administrasi', 'admin fee', 'biaya admin', 'fee', 'service charge',
    'biaya transfer', 'transfer fee', 'biaya layanan',
]

_TOPUP_KEYWORDS = [
    'top up', 'topup', 'isi ulang', 'isi saldo', 'add balance',
    'purchase', 'beli saldo', 'isi gojek',
]

_CASHBACK_KEYWORDS = [
    'cashback', 'reward', 'coin', 'bonus', 'promo',
]

_REFUND_KEYWORDS = [
    'refund', 'kembalian', 'cancelled refund',
]


def categorize_batch(conn: sqlite3.Connection, rows: list[dict]) -> list[dict]:
    """Run deterministic rules on a batch of extracted rows.

    Returns list of dicts with {ext_id, category_id, nature, confidence, reason}.
    """
    results = []
    for row in rows:
        result = _categorize_single(conn, row)
        results.append(result)
    return results


def _categorize_single(conn: sqlite3.Connection, row: dict) -> dict:
    """Categorize a single extracted transaction row."""
    ext_id = row.get('id')
    desc = (row.get('description', '') + ' ' + row.get('merchant', '')).lower()
    recipient = (row.get('recipient', '') or '').lower()
    direction = row.get('direction', 'out')
    total = row.get('total_amount', 0)
    fee = row.get('fee_amount', 0)
    provider = (row.get('provider', '') or '').lower()

    category_id = None
    nature = 'needs_review'
    confidence = 'none'
    reason = ''

    # 1. Check existing rules first (remembered)
    rule = _match_rule(conn, desc, recipient)
    if rule:
        category_id = rule['category_id']
        nature = rule['nature']
        confidence = 'high'
        reason = f"Matched rule: {rule['merchant_or_recipient']}"
        store.increment_rule_usage(conn, rule['id'])

    # 2. Dinda recipient + note
    elif _DINDA_NAME_RE.search(recipient) or _DINDA_NAME_RE.search(desc):
        if 'belanja sayur' in desc:
            category_id = store.get_or_create_category(conn, 'Groceries')
            nature = 'expense'
            confidence = 'high'
            reason = 'Dinda + Belanja Sayur'
        elif 'bayar utang' in desc:
            category_id = store.get_or_create_category(conn, 'Debt repayment')
            nature = 'debt_repayment'
            confidence = 'high'
            reason = 'Dinda + Bayar Utang'
        else:
            # Transfer to Dinda, no note
            category_id = store.get_or_create_category(conn, 'Transfer - Dinda')
            nature = 'transfer_to_person'
            confidence = 'medium'
            reason = 'Dinda recipient, no specific note'

    # 3. Fee detection
    elif any(kw in desc for kw in _FEE_KEYWORDS) or (fee > 0 and total == fee):
        category_id = store.get_or_create_category(conn, 'Bank fee')
        nature = 'fee'
        confidence = 'high'
        reason = 'Fee keyword or fee-only amount'

    # 4. Top-up detection
    elif any(kw in desc for kw in _TOPUP_KEYWORDS):
        category_id = store.get_or_create_category(conn, 'Top-up')
        nature = 'top_up'
        confidence = 'high'
        reason = 'Top-up keyword'

    # 5. Cashback/reward
    elif any(kw in desc for kw in _CASHBACK_KEYWORDS):
        category_id = store.get_or_create_category(conn, 'Cashback/Reward')
        nature = 'cashback'
        confidence = 'high'
        reason = 'Cashback/reward keyword'

    # 6. Refund
    elif any(kw in desc for kw in _REFUND_KEYWORDS):
        category_id = store.get_or_create_category(conn, 'Refund')
        nature = 'refund'
        confidence = 'high'
        reason = 'Refund keyword'

    # 7. GoTagihan without specific bill
    elif re.search(r'go\s*tagihan|gopay\s*bills', desc):
        category_id = store.get_or_create_category(conn, 'Bills - Unknown')
        nature = 'expense'
        confidence = 'low'
        reason = 'GoTagihan without specific bill'

    # 8. Income patterns (money in)
    elif direction == 'in':
        if any(kw in desc for kw in ['salary', 'gaji', 'income', 'pendapatan']):
            category_id = store.get_or_create_category(conn, 'Salary')
            nature = 'income'
            confidence = 'medium'
        else:
            nature = 'income'
            confidence = 'low'
            reason = 'Money in, no specific pattern'

    return {
        'ext_id': ext_id,
        'category_id': category_id,
        'nature': nature,
        'confidence': confidence,
        'reason': reason,
    }


def _match_rule(conn: sqlite3.Connection, desc: str, recipient: str) -> Optional[dict]:
    """Check if description or recipient matches any active rule."""
    rules = store.list_rules(conn, active_only=True)
    for rule in rules:
        pattern = rule['merchant_or_recipient'].lower()
        if pattern in desc or pattern in recipient:
            return rule
    return None


def apply_correction(conn: sqlite3.Connection, *,
                      ledger_id: int, field: str,
                      old_value: Any, new_value: Any,
                      reason: str = '',
                      remember: bool = False,
                      merchant_or_recipient: str = '',
                      category_id: int | None = None,
                      nature: str | None = None) -> dict:
    """Apply a manual correction to a ledger row.

    If remember=True, creates a category rule for future matching.
    """
    # Update the ledger row
    update_fields = {field: new_value}
    store.update_ledger(conn, ledger_id, **update_fields)

    # Record correction
    store.add_correction(conn,
        ledger_id=ledger_id,
        field=field,
        before_val=str(old_value),
        after_val=str(new_value),
        reason=reason)

    # Audit
    store.add_audit(conn,
        action='manual_correction',
        entity='ledger_txns',
        entity_id=ledger_id,
        before_json=f'{{"{field}": "{old_value}"}}',
        after_json=f'{{"{field}": "{new_value}"}}')

    # Remember rule if requested
    if remember and merchant_or_recipient and category_id:
        store.add_rule(conn,
            merchant_or_recipient=merchant_or_recipient,
            category_id=category_id,
            nature=nature or 'expense',
            source='manual')

    return {'ok': True, 'ledger_id': ledger_id, 'field': field}
