"""Categorization engine: deterministic keyword + lookup-table rules. No LLM.

Implements the decision tree from finance-notification-categorization-rules.md.
First match wins; most specific rules first. Reads only normalised extracted
fields (description, merchant, recipient, direction, amount, provider) - never
raw bank-specific format, so new sources only need a Step-0 parser.

Natures map to the ledger_txns CHECK constraint:
    internal_transfer -> excluded from spend/net (your own wallet moves)
    transfer_to_person -> tracked, not personal expense (family)
    top_up / fee / cashback / refund / income -> distinct stats
    expense -> the spending that lands in the dashboard's numbers
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any, Optional

import store

# ── own-account / e-wallet registry (for internal-move detection) ─────────
# A top-up debit counts as an internal transfer only when the row text names
# a wallet/account we actually hold (registered in `accounts`).
_EWALLET_PROVIDERS = {'gopay', 'ovo', 'dana', 'shopeepay', 'wondr', 'bni'}

# ── category taxonomy (names created on first use via get_or_create) ──────
_CAT = {
    'income':         'Income',
    'internal':       'Internal Transfer',
    'family':         'Family Transfer',
    'topup_3rd':      'Top Up (3rd party)',
    'bills_elec':     'Bills - Electricity',
    'bills_water':    'Bills - Water',
    'bills_internet': 'Bills - Internet',
    'bills_phone':    'Bills - Phone',
    'bills_insure':   'Bills - Insurance',
    'bills_cc':       'Bills - Credit Card',
    'groceries':      'Groceries',
    'food':           'Food & Transport',
    'shopping':       'Online Shopping',
    'cash':           'Cash Withdrawal',
    'fee':            'Bank fee',
    'cashback':       'Cashback/Reward',
    'refund':         'Refund',
    'uncategorized':  'Uncategorized',
}

# ── keyword maps (the "muscle" - extend these as new merchants appear) ────

# Billers: keyword -> (taxonomy key, subtype used in the category name)
_BILLERS = [
    ('token listrik', 'bills_elec'), ('pln', 'bills_elec'),
    ('pdam', 'bills_water'), ('aetra', 'bills_water'), ('palyja', 'bills_water'),
    ('indihome', 'bills_internet'), ('first media', 'bills_internet'), ('biznet', 'bills_internet'),
    ('telkomsel', 'bills_phone'), ('indosat', 'bills_phone'), ('pulsa', 'bills_phone'),
    ('paket data', 'bills_phone'),
    ('bpjs kesehatan', 'bills_insure'), ('bpjs ketenagakerjaan', 'bills_insure'),
    ('kartu kredit', 'bills_cc'), ('payment cc', 'bills_cc'),
]

_GROCERIES = ['indomaret', 'alfamart', 'superindo', 'hypermart', 'transmart',
              'ranch market']
_FOOD_TRANSPORT = ['gojek', 'gofood', 'grab', 'grabfood', 'shopeefood',
                   'mandiri e-money', 'emoney', 'e-money', 'toll', 'tol',
                   'flazz']
_ECOMMERCE = ['tokopedia', 'shopee', 'lazada', 'blibli']
_ATM_CASH = ['tarik tunai', 'withdrawal', 'penarikan tunai']
_INCOME_HINTS = ['gaji', 'salary', 'payroll', 'invoice', 'honor', 'dana masuk',
                 'transfer masuk', 'terima', 'received']

_FEE_KEYWORDS = ['administrasi', 'admin fee', 'biaya admin', 'fee',
                 'service charge', 'biaya transfer', 'transfer fee',
                 'biaya layanan', 'biaya adm']
_TOPUP_KEYWORDS = ['top up', 'topup', 'top-up', 'isi ulang', 'isi saldo',
                   'add balance', 'beli saldo', 'isi gojek', 'purchase']
_CASHBACK_KEYWORDS = ['cashback', 'reward', 'coin', 'bonus', 'promo']
_REFUND_KEYWORDS = ['refund', 'kembalian', 'cancelled refund']

# Short tokens need whole-word matching to avoid false positives (e.g. "xl").
_WORD_BOUNDED = {'pln', 'grab', 'shopee', 'xl'}


def _has(text: str, token: str) -> bool:
    if token in _WORD_BOUNDED:
        return bool(re.search(rf'\b{re.escape(token)}\b', text))
    return token in text


def _match_table(text: str, entries: list[tuple]) -> Optional[str]:
    """Return taxonomy key of the first matching keyword, or None."""
    for token, key in entries:
        if _has(text, token):
            return key
    return None


def _is_own_wallet_topup(conn: sqlite3.Connection, row: dict) -> bool:
    """True when a top-up debit moves money into one of OUR own wallets.

    Bank -> e-wallet top-ups are treated as internal *by default* so a top-up
    (out of BNI) plus the later wallet spends (GoPay receipts) do not
    double-count as two expenses. The only escape hatch: a wallet we do NOT
    hold is named in the text (e.g. 'OVO' when we never registered OVO), which
    falls through to 'Top Up (3rd party)' for review instead.
    """
    desc = ((row.get('description', '') + ' ' + row.get('merchant', '')
             + ' ' + row.get('recipient', ''))).lower()
    if row.get('direction', 'out') != 'out':
        return False
    if not any(_has(desc, kw) for kw in _TOPUP_KEYWORDS):
        return False

    our = {a['provider'].lower() for a in store.list_accounts(conn, active_only=True)}
    provider = (row.get('provider', '') or '').lower()

    # E-wallet -> bank is a withdrawal, not a top-up (shouldn't land here).
    if provider in _EWALLET_PROVIDERS:
        return False

    # A wallet we don't hold explicitly named -> not ours.
    named = [p for p in _EWALLET_PROVIDERS if _has(desc, p)]
    if named and not all(p in our for p in named):
        return False

    # Default: bank debit + top-up wording = funding our own wallet.
    return provider in {'bca', 'bni', 'bri', 'mandiri'}


def categorize_batch(conn: sqlite3.Connection, rows: list[dict]) -> list[dict]:
    """Run deterministic rules on a batch of extracted rows.

    Returns list of dicts {ext_id, category_id, nature, confidence, reason}.
    """
    return [_categorize_single(conn, row) for row in rows]


def _categorize_single(conn: sqlite3.Connection, row: dict) -> dict:
    ext_id = row.get('id')
    desc = ((row.get('description', '') + ' ' + row.get('merchant', ''))).lower()
    recipient = (row.get('recipient', '') or '').lower()
    direction = row.get('direction', 'out')
    total = row.get('total_amount', 0) or row.get('principal_amount', 0) or 0
    fee = row.get('fee_amount', 0) or 0

    hit = {
        'ext_id': ext_id,
        'category_id': None, 'nature': 'needs_review',
        'confidence': 'none', 'reason': '',
    }

    def set_cat(key, nature, confidence, reason):
        hit['category_id'] = store.get_or_create_category(conn, _CAT[key])
        hit['nature'] = nature
        hit['confidence'] = confidence
        hit['reason'] = reason

    # 0. Remembered rule wins over everything (highest specificity)
    rule = _match_rule(conn, desc + ' ' + recipient)
    if rule:
        store.increment_rule_usage(conn, rule['id'])
        hit['category_id'] = rule['category_id']
        hit['nature'] = rule['nature']
        hit['confidence'] = 'high'
        hit['reason'] = f'Rule: {rule["merchant_or_recipient"]}'
        return hit

    # 1. INTERNAL MOVE (own BNI/BCA -> own wallet). Excluded from spend.
    if _is_own_wallet_topup(conn, row):
        set_cat('internal', 'internal_transfer', 'medium',
                'Top-up to own wallet')
        return hit

    text = desc + ' ' + recipient

    # 2. MONEY-IN with specific identity beats the merchant tree.
    #    A "SHOPEE REFUND" credit is a refund, NOT an e-commerce expense.
    #    Order matters: these keywords can overlap with merchant names.
    if direction == 'in':
        if any(_has(desc, kw) for kw in _REFUND_KEYWORDS):
            set_cat('refund', 'refund', 'high', 'Refund')
            return hit
        if any(_has(desc, kw) for kw in _CASHBACK_KEYWORDS):
            set_cat('cashback', 'cashback', 'high', 'Cashback/reward')
            return hit

    # 3. INCOME (credit + clear income hints)
    if direction == 'in' and (_match_table(text, [(k, 'income')
                             for k in _INCOME_HINTS])):
        set_cat('income', 'income', 'high', 'Income hint')
        return hit

    # 4. BILLS (most specific billers first)
    bill = _match_table(text, _BILLERS)
    if bill:
        set_cat(bill, 'expense', 'high', 'Biller match')
        return hit

    # 5. GROCERIES / retail
    if any(_has(desc, m) or _has(recipient, m) for m in _GROCERIES):
        set_cat('groceries', 'expense', 'high', 'Retail merchant')
        return hit

    # 6. FOOD & TRANSPORT (common on GoPay)
    if any(_has(desc, m) or _has(recipient, m) for m in _FOOD_TRANSPORT):
        set_cat('food', 'expense', 'high', 'Food/transport merchant')
        return hit

    # 7. E-COMMERCE
    if any(_has(desc, m) or _has(recipient, m) for m in _ECOMMERCE):
        set_cat('shopping', 'expense', 'high', 'E-commerce merchant')
        return hit

    # 8. ATM / CASH WITHDRAWAL
    if any(_has(text, k) for k in _ATM_CASH):
        set_cat('cash', 'expense', 'high', 'Cash withdrawal')
        return hit

    # 9. TOP-UP (external / not our own wallet -> real outflow)
    if any(_has(desc, kw) for kw in _TOPUP_KEYWORDS):
        set_cat('topup_3rd', 'top_up', 'medium', '3rd-party top-up')
        return hit

    # 10. Fees (fee-only rows)
    if any(_has(desc, kw) for kw in _FEE_KEYWORDS) or (fee > 0 and total == fee):
        set_cat('fee', 'fee', 'high', 'Bank fee')
        return hit

    # 11. Credit with no other signal -> income (low confidence)
    if direction == 'in':
        set_cat('income', 'income', 'low', 'Money in, no specific pattern')
        return hit

    # 12. FALLBACK -> uncategorized, needs review
    set_cat('uncategorized', 'needs_review', 'none', 'No rule matched')
    return hit


def _match_rule(conn: sqlite3.Connection, text: str) -> Optional[dict]:
    """Check if description/recipient matches any active remembered rule."""
    for rule in store.list_rules(conn, active_only=True):
        pattern = rule['merchant_or_recipient'].lower()
        if pattern and pattern in text:
            return rule
    return None


def apply_categorization(conn: sqlite3.Connection, row: dict,
                         ledger_id: int) -> None:
    """Persist a categorization result onto an existing ledger row."""
    if row.get('category_id'):
        store.update_ledger(conn, ledger_id,
            category_id=row['category_id'],
            nature=row['nature'],
            confidence=row['confidence'],
            confidence_reason=row['reason'],
            review_status='ok' if row['nature'] != 'needs_review' else 'uncategorized',
            txn_status='confirmed' if row['nature'] != 'needs_review' else 'needs_review')


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
    store.update_ledger(conn, ledger_id, **{field: new_value})

    store.add_correction(conn,
        ledger_id=ledger_id,
        field=field,
        before_val=str(old_value),
        after_val=str(new_value),
        reason=reason)

    store.add_audit(conn,
        action='manual_correction',
        entity='ledger_txns',
        entity_id=ledger_id,
        before_json=f'{{"{field}": "{old_value}"}}',
        after_json=f'{{"{field}": "{new_value}"}}')

    if remember and merchant_or_recipient and category_id:
        store.add_rule(conn,
            merchant_or_recipient=merchant_or_recipient,
            category_id=category_id,
            nature=nature or 'expense',
            source='manual')

    return {'ok': True, 'ledger_id': ledger_id, 'field': field}