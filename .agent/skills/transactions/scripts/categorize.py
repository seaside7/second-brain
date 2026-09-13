"""Categorization engine: deterministic keyword + lookup-table rules. No LLM.

Implements the decision tree agreed for the Personal Finance dashboard. First
match wins; most specific rules first. Reads only normalised extracted fields
(description, merchant, recipient, direction, amount, provider,
transaction_type) - never raw bank-specific format, so new sources only need a
Step-0 parser.

Natures map to the ledger_txns CHECK constraint:
    internal_transfer -> excluded from spend/net (your own wallet moves)
    transfer_to_person -> tracked, not personal expense (family)
    top_up / fee / refund / income -> distinct stats
    expense -> the spending that lands in the dashboard's numbers
    needs_review -> ambiguous / no rule matched; user picks manually

Taxonomy is two-level: category = categories.group, subcategory =
categories.name (e.g. Utilities > Electricity (PLN), Food & Dining > Warung /
Snacks, Transport > Fuel).
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any, Optional

import store

# ── own-account / e-wallet registry (for internal-move detection) ─────────
_EWALLET_PROVIDERS = {'gopay', 'ovo', 'dana', 'shopeepay', 'wondr'}
_BANK_PROVIDERS = {'bca', 'bni', 'bri', 'mandiri'}

# VA billers that are loan facilities (owner categorizes the biller, the
# categorizer never guesses). Everything else falls through to Review.
_VA_LOAN_BILLERS = ('pegadaian', 'gadai')

# Buy-now-pay-later / online credit facilities (owner categorizes the biller,
# the categorizer never guesses). -> Online Credit.
_VA_ONLINE_CREDIT = ('spaylater', 'spinjam', 'gopay later', 'golater', 'paylater',
                     'shopee pinjam', 'kredivo', 'akulaku', 'indodana',
                     'ada kredit')

# Wallet providers that can be OUR OWN wallet. A bank debit only counts as an
# own-wallet top-up when the parsed counterparty (recipient/merchant - never
# the raw email body) names one of these, or the row is an explicit wallet
# top-up. 'purchase' is deliberately absent: BCA payment emails all say
# "Transaction Type: PURCHASE", which used to swallow real bill payments.
_WALLET_KEYWORDS = ('gopay', 'go-pay', 'ovo', 'shopeepay', 'shopee pay',
                    'e-wallet', 'ewallet', 'dompet')

# VA acquirers that are never our own wallet (ShopeePay via AirPay, ...). A
# bank debit into one of these VAs is money out to a third party - it goes
# to Review, never internal, even when the description says "Top Up".
_THIRD_PARTY_ACQUIRERS = ('airpay',)

# Our own wallet companies (GoPay's PT): a VA debit naming one of these is
# still ours, never third-party.
_OWN_WALLET_COMPANIES = ('dompet anak bangsa',)

# Our own names: transfer to an account under these -> internal move.
_OWN_NAMES = ['said iskandar']

# ── category taxonomy (group, name); get_or_create adds rows on first use ──
_CAT = {
    'income':         ('Income', 'Income'),
    'internal':       ('Transfers', 'Internal Transfer'),
    'family':         ('Transfers', 'Family Transfer'),
    'topup_3rd':      ('Transfers', 'Top Up (3rd party)'),
    'bills_elec':     ('Utilities', 'Electricity (PLN)'),
    'bills_internet': ('Utilities', 'Internet'),
    'bills_phone':    ('Utilities', 'Mobile & Data'),
    'bills_cc':       ('Utilities', 'Credit Card'),
    'groceries':      ('Groceries', 'Groceries'),
    'food':            ('Food & Dining', 'Food & Dining'),
    'transport_fuel': ('Transport', 'Fuel'),
    'transport_toll': ('Transport', 'Toll'),
    'transport_parking': ('Transport', 'Parking'),
    'transport_ride': ('Transport', 'Ride Sharing'),
'vehicle_service': ('Vehicle', 'Car Service'),
    'home_upkeep':     ('Home', 'Home Maintenance & Repair'),
    'shopping':        ('Shopping', 'Online Shopping'),
    'cash':           ('Cash', 'Cash Withdrawal'),
    'fee':            ('Fees', 'Bank Fee'),
    'loan_payment':   ('Loans', 'Loan Payment'),
    'online_credit':  ('Loans', 'Online Credit'),
    'refund':         ('Income', 'Refund'),
    'uncategorized':  ('Uncategorized', 'Uncategorized'),
}

# ── keyword maps (the "muscle" - extend these as new merchants appear) ────

# Billers: keyword -> taxonomy key. Order matters (most specific first).
_BILLERS = [
    ('token listrik', 'bills_elec'), ('pln', 'bills_elec'),
    ('indihome', 'bills_internet'), ('first media', 'bills_internet'),
    ('biznet', 'bills_internet'),
    ('telkomsel', 'bills_phone'), ('indosat', 'bills_phone'),
    ('pulsa', 'bills_phone'), ('paket data', 'bills_phone'),
    ('kartu kredit', 'bills_cc'), ('payment cc', 'bills_cc'),
]

_GROCERIES = ['indomaret', 'alfamart', 'superindo', 'hypermart', 'transmart',
              'ranch market', 'sembako', 'belanja sayur', 'sayur']
_FOOD_DELIVERY = ['gofood', 'grabfood', 'shopeefood', 'go food', 'delivery']
_FOOD_RIDE = ['gojek', 'grab']
_FOOD_RESTAURANT = ['restoran', 'restaurant', 'rm ', 'warung makan', 'rumah makan']
_FOOD_CAFE = ['cafe', 'kopi', 'café', 'kafé']
_ECOMMERCE = ['tokopedia', 'shopee', 'lazada', 'blibli', 'forumer', ' marketplace']

# "warung" is ambiguous on its own: combine it with food words, else -> Review.
_WARUNG_MARKERS = ['warung', 'warkop']
_FOOD_WORDS = ['gorengan', 'nasi', 'soto', 'bakso', 'mie', 'mihun', 'kopi',
               'restoran', 'cafe', 'padang', 'ayam', 'rendang', 'rujak',
               'pecel', 'ikan bak', 'seafood', 'gado-gado', 'penyet', 'sate']

_TRANSPORT_FUEL = ['pertamina', 'bensin', 'solar', 'spbu', 'shell']
_TRANSPORT_TOLL = ['tol', 'toll', 'jalan tol', 'flazz', 'e-money', 'emoney',
                   'mandiri e-money']
_TRANSPORT_PARKING = ['parkir', 'parkmen']
_VEHICLE_SERVICE = ['bengkel', 'servis', 'service', 'spooring', 'tune-up',
                    'tune up', 'ganti oli', 'kaki-kaki', 'gearbox']
_HOME_UPKEEP = ['tukang', 'plumber', 'ledeng', 'renovasi',
                'perbaikan rumah', 'servis ac', 'ac service', 'kulkas',
                'keran', 'cctv']
_ATM_CASH = ['tarik tunai', 'withdrawal', 'penarikan tunai']
_INCOME_HINTS = ['gaji', 'salary', 'payroll', 'invoice', 'honor', 'dana masuk',
                 'transfer masuk', 'terima', 'received', 'freelance', 'upah']
_FEE_KEYWORDS = ['administrasi', 'admin fee', 'biaya admin', 'fee',
                 'service charge', 'biaya transfer', 'transfer fee',
                 'biaya layanan', 'biaya adm']
_TOPUP_KEYWORDS = ['top up', 'topup', 'top-up', 'isi ulang', 'isi saldo',
                   'add balance', 'beli saldo']
_REFUND_KEYWORDS = ['refund', 'kembalian', 'cancelled refund']

# Short tokens need whole-word matching to avoid false positives (e.g. "xl").
_WORD_BOUNDED = {'pln', 'grab', 'shopee', 'xl', 'sate', 'kopi', 'tol'}


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


def _text(row: dict) -> str:
    return ((row.get('description', '') + ' ' + row.get('raw_description', '')
             + ' ' + row.get('merchant', '')
             + ' ' + row.get('recipient', ''))).lower()


def _clean_text(row: dict) -> str:
    """Clean-only text (no raw body): used for fee detection so a split
    principal row containing 'Biaya Admin' in raw doesn't classify as Fee."""
    return ((row.get('description', '') + ' ' + row.get('merchant', '')
             + ' ' + row.get('recipient', ''))).lower()


def _is_own_wallet_topup(conn: sqlite3.Connection, row: dict) -> bool:
    """True when a top-up moves money between OUR own wallet and a bank.

    Bank -> e-wallet (out) and e-wallet <- bank (in) are treated as internal
    transfers by default so the same money is not double-counted as two
    expenses (bank side + wallet side). E-wallet -> e-wallet or a wallet
    out-spend falls through to normal categorization.

    The top-up keyword is matched against the CLEAN text (parsed description /
    merchant / recipient) - never the raw email body, which contains generic
    words like "purchase". A bank debit additionally needs wallet evidence:
    the counterparty names one of our wallets, or the row is an explicit
    wallet top-up.
    """
    clean = _clean_text(row)
    if not any(_has(clean, kw) for kw in _TOPUP_KEYWORDS):
        return False
    provider = (row.get('provider', '') or '').lower()
    direction = row.get('direction', 'out')

    if direction == 'out':
        # Bank debit funding one of our own wallets.
        if provider not in _BANK_PROVIDERS:
            return False
        counterparty = ((row.get('recipient', '') or '') + ' ' +
                        (row.get('merchant', '') or '')).lower()
        if any(w in counterparty for w in _WALLET_KEYWORDS):
            return True
        tx_type = (row.get('transaction_type', '') or '').lower()
        if tx_type == 'top_up' and any(w in clean for w in _WALLET_KEYWORDS):
            return True
        return False
    # Money in: e-wallet received funding from a bank.
    return provider in _EWALLET_PROVIDERS


def _is_own_wallet_withdrawal(conn: sqlite3.Connection, row: dict) -> bool:
    """True when a BANK credit is a withdrawal from one of OUR own e-wallets.

    The statement shows the money IN on the bank side; the e-wallet side will
    later record the actual spend. Keeping this as an internal transfer avoids
    double-counting the wallet's balance as income.
    """
    if row.get('direction') != 'in':
        return False
    provider = (row.get('provider', '') or '').lower()
    if provider not in _BANK_PROVIDERS:
        return False
    text = (_text(row) + ' ' + (row.get('recipient', '') or '')).lower()
    return any(m in text for m in ('dompet anak bangsa', 'gopay wallet',
                                   'gopay bank transfer', 'gopay withdrawal'))


def _is_third_party_topup(row: dict) -> bool:
    """True when a BANK debit pays a Virtual Account of a known third-party
    acquirer (ShopeePay via AirPay for someone else's account, ...).

    The description may still say "Top Up" - but the money leaves to a third
    party, so it must go to Review, never internal_transfer.
    """
    if row.get('direction', 'out') != 'out':
        return False
    if (row.get('provider', '') or '').lower() not in _BANK_PROVIDERS:
        return False
    text = _text(row)
    if 'virtual account' not in text:
        return False
    if any(c in text for c in _OWN_WALLET_COMPANIES):
        return False
    return any(a in text for a in _THIRD_PARTY_ACQUIRERS)


def _own_person_transfer(conn: sqlite3.Connection, row: dict) -> bool:
    """True when a transfer's counterparty is one of OUR registered accounts.

    Checks the `recipient` (and merchant fallback) against the accounts
    registry (alias, owner_name, provider) so BNI -> BCA (same owner) is an
    internal transfer, not an expense.
    """
    # Only the parsed counterparty values are evidence of OUR account - the
    # raw text always mentions the provider ('wondr by BNI', 'BI-FAST') and
    # matching on it would mark every BNI transfer as an internal move.
    cand = ((row.get('recipient', '') or '').strip() + ' ' +
            (row.get('merchant', '') or '').strip()).strip().lower()
    if not cand:
        return False
    for acc in store.list_accounts(conn, active_only=True):
        hay = ' '.join([acc.get('alias', ''), acc.get('owner_name', ''),
                        acc.get('provider', ''), acc.get('masked', '')]).lower()
        tokens = [t for t in acc.get('alias', '').lower().split()
                  if len(t) > 2]
        if acc.get('owner_name') and acc.get('owner_name').lower() in cand:
            return True
        if any(t and t in cand for t in tokens if len(t) > 2):
            return True
        if acc.get('provider') and acc.get('provider').lower() in cand:
            return True
    return False


def _is_own_name_transfer(row: dict) -> bool:
    """True when a bank transfer's counterparty is one of OUR names.

    A transfer to an account under our own name (Said Iskandar) at any bank is
    money moving between our own accounts, so it is an internal transfer and
    must stay out of both income and spending totals. The expense is recorded
    later from the destination account when the money is actually used.

    Virtual Account payments are excluded: a VA belongs to a real biller
    (never ourselves), and the VA parser already sets transaction_type
    'va_payment' with the biller as recipient.
    """
    if (row.get('transaction_type', '') or '').lower() == 'va_payment':
        return False
    if 'virtual account' in _text(row):
        return False
    cand = ((row.get('recipient', '') or '') + ' ' +
            (row.get('merchant', '') or '')).lower()
    return any(name in cand for name in _OWN_NAMES)


def categorize_batch(conn: sqlite3.Connection, rows: list[dict]) -> list[dict]:
    """Run deterministic rules on a batch of extracted rows.

    Returns list of dicts {ext_id, category_id, nature, confidence, reason}.
    """
    return [_categorize_single(conn, row) for row in rows]


def _categorize_single(conn: sqlite3.Connection, row: dict) -> dict:
    ext_id = row.get('id')
    desc = _text(row)
    direction = row.get('direction', 'out')
    total = row.get('total_amount', 0) or row.get('principal_amount', 0) or 0
    fee = row.get('fee_amount', 0) or 0
    tx_type = (row.get('transaction_type', '') or '').lower()

    hit = {
        'ext_id': ext_id,
        'category_id': None, 'nature': 'needs_review',
        'confidence': 'none', 'reason': '',
    }

    def set_cat(key, nature, confidence, reason):
        group, name = _CAT[key]
        hit['category_id'] = store.get_or_create_category(conn, name, group=group)
        hit['nature'] = nature
        hit['confidence'] = confidence
        hit['reason'] = reason

    def set_fallback(reason='No rule matched'):
        hit['category_id'] = store.get_or_create_category(
            conn, _CAT['uncategorized'][1], group=_CAT['uncategorized'][0])
        hit['nature'] = 'needs_review'
        hit['confidence'] = 'none'
        hit['reason'] = reason

    # 0. Remembered rule wins over everything (highest specificity)
    rule = _match_rule(conn, desc)
    if rule:
        store.increment_rule_usage(conn, rule['id'])
        hit['category_id'] = rule['category_id']
        hit['nature'] = rule['nature']
        hit['confidence'] = 'high'
        hit['reason'] = f'Rule: {rule["merchant_or_recipient"]}'
        return hit

    # 0.5 Explicit fee rows (parser split an admin fee) are always Fees.
    if tx_type == 'fee':
        set_cat('fee', 'fee', 'high', 'Explicit admin fee')
        return hit
    if any(_has(_clean_text(row), kw) for kw in _FEE_KEYWORDS):
        set_cat('fee', 'fee', 'high', 'Fee keyword')
        return hit

    # 0.7 Bank debit into a third-party acquirer VA (ShopeePay via AirPay
    #     for someone else's account, ...) is NOT our wallet - Review it.
    if _is_third_party_topup(row):
        set_fallback('Top-up to unverified recipient - verify')
        return hit

    # 1. INTERNAL MOVE (own bank <-> own wallet / own account). Excluded from spend.
    if _is_own_wallet_topup(conn, row):
        set_cat('internal', 'internal_transfer', 'medium',
                'Top-up to own wallet')
        return hit

    text = desc

    # 1.5 MONEY-IN specific identity beats the merchant tree (also honours
    #     the parser's 'refund' transaction_type). Cashback credits have no
    #     category - they fall to Review for manual filing.
    if tx_type in ('refund',) or any(_has(desc, kw) for kw in _REFUND_KEYWORDS):
        if direction == 'in':
            set_cat('refund', 'refund', 'high', 'Refund')
            return hit

    # 2. TRANSFER detection (bank out / person transfer) - do NOT auto-treat
    #    the sign or subject as income/expense. Transfer to own account is
    #    internal; to another person it is a tracked, non-spend transfer.
    if direction == 'out' and _is_transfer_desc(desc):
        if _is_own_name_transfer(row):
            set_cat('internal', 'internal_transfer', 'high',
                    'Transfer to own account')
            return hit
        if _own_person_transfer(conn, row):
            set_cat('internal', 'internal_transfer', 'medium',
                    'Transfer to own (registered) account')
            return hit
        # Transfer to a person, optionally with a categorizable note.
        if 'belanja' in desc or any(_has(desc, m) for m in _GROCERIES):
            set_cat('groceries', 'expense', 'medium',
                    'Transfer with grocery note')
            return hit
        # Transfer to a person - one shared category, no per-recipient name
        # (the owner annotates the recipient themselves).
        if (row.get('recipient', '') or '').strip():
            group, name_cat = ('Transfers', 'Transfer')
            hit['category_id'] = store.get_or_create_category(conn, name_cat, group=group)
            hit['nature'] = 'transfer_to_person'
            hit['confidence'] = 'medium'
            hit['reason'] = 'Transfer to person'
            return hit
        set_fallback('Transfer without counterparty details')
        return hit

    # 2.5 MONEY-IN transfer (credit) - not income by sign alone.
    if direction == 'in' and _is_transfer_desc(desc):
        if _is_own_name_transfer(row):
            set_cat('internal', 'internal_transfer', 'high',
                    'Transfer from own account')
            return hit
        if _is_own_wallet_withdrawal(conn, row):
            set_cat('internal', 'internal_transfer', 'medium',
                    'Withdrawal from own wallet')
            return hit
        set_fallback('Inbound transfer - verify income vs person')
        return hit

    # 2.6 VIRTUAL ACCOUNT loan payments - the owner pays down a loan facility
    #     through a VA. Online credit (SPayLater / GoPay Later / Kredivo) ->
    #     Online Credit; pawnshop facilities (Pegadaian) -> Loan Payment.
    #     Never guessed.
    if (direction == 'out' and tx_type == 'va_payment'
            and any(_has(_text(row), m) for m in _VA_ONLINE_CREDIT)):
        set_cat('online_credit', 'expense', 'high',
                'VA online credit payment')
        return hit
    if (direction == 'out' and tx_type == 'va_payment'
            and any(_has(_text(row), m) for m in _VA_LOAN_BILLERS)):
        set_cat('loan_payment', 'expense', 'high',
                'VA loan facility payment')
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
    if any(_has(desc, m) or _has((row.get('recipient', '')), m) for m in _GROCERIES):
        set_cat('groceries', 'expense', 'high', 'Retail merchant')
        return hit

    # 5b. HOME MAINTENANCE & REPAIR (tukang / plumber / servis AC) - before
    #     vehicle 'servis' so home services win over Car Service.
    if any(_has(desc, m) for m in _HOME_UPKEEP):
        set_cat('home_upkeep', 'expense', 'high', 'Home repair / tukang')
        return hit

    # 6. FOOD & DINING - one flat category. Warung needs a food word,
    #    otherwise -> Review.
    if any(_has(desc, m) for m in _WARUNG_MARKERS):
        if any(_has(desc, m) for m in _FOOD_WORDS):
            set_cat('food', 'expense', 'medium',
                    'Warung + food word')
            return hit
        set_fallback('Warung without food context - verify')
        return hit
    if any(_has(desc, m) for m in _FOOD_RESTAURANT):
        set_cat('food', 'expense', 'high', 'Restaurant')
        return hit
    if any(_has(desc, m) for m in _FOOD_CAFE):
        set_cat('food', 'expense', 'high', 'Cafe / coffee')
        return hit
    if any(_has(desc, m) for m in _FOOD_DELIVERY):
        set_cat('food', 'expense', 'high', 'Food delivery')
        return hit

    # 7. TRANSPORT
    if any(_has(desc, m) for m in _TRANSPORT_FUEL):
        set_cat('transport_fuel', 'expense', 'high', 'Fuel')
        return hit
    if any(_has(desc, m) for m in _TRANSPORT_TOLL):
        set_cat('transport_toll', 'expense', 'high', 'Toll / e-money')
        return hit
    if any(_has(desc, m) for m in _TRANSPORT_PARKING):
        set_cat('transport_parking', 'expense', 'high', 'Parking')
        return hit
    if any(_has(desc, m) for m in _VEHICLE_SERVICE):
        set_cat('vehicle_service', 'expense', 'high', 'Vehicle service')
        return hit
    if any(_has(desc, m) for m in _FOOD_RIDE):
        set_cat('transport_ride', 'expense', 'high', 'Ride sharing')
        return hit

    # 8. E-COMMERCE
    if any(_has(desc, m) or _has((row.get('recipient', '') or ''), m) for m in _ECOMMERCE):
        set_cat('shopping', 'expense', 'high', 'E-commerce merchant')
        return hit

    # 9. ATM / CASH WITHDRAWAL
    if any(_has(text, k) for k in _ATM_CASH):
        set_cat('cash', 'expense', 'high', 'Cash withdrawal')
        return hit

    # 10. TOP-UP (external / not our own wallet -> real outflow)
    if any(_has(desc, kw) for kw in _TOPUP_KEYWORDS):
        set_cat('topup_3rd', 'top_up', 'medium', '3rd-party top-up')
        return hit

    # 11. Fee-only rows (fee == total)
    if fee > 0 and total == fee:
        set_cat('fee', 'fee', 'high', 'Fee-only row')
        return hit

    # 12. Credit with no other signal -> needs review (user picks), NOT income.
    if direction == 'in':
        set_fallback('Money in, no clear pattern - verify')
        return hit

    # 13. FALLBACK -> uncategorized, needs review
    set_fallback('No rule matched')
    return hit


def _is_transfer_desc(desc: str) -> bool:
    """Whether a description clearly represents a person-to-person / bank
    transfer (as opposed to a merchant purchase)."""
    for kw in ['transfer', 'trf', 'trsf', 'pengiriman', 'kirim uang',
               'bi-fast', 'bifast', 'antar rekening']:
        if _has(desc, kw):
            return True
    return False


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