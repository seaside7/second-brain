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
                     'ada kredit', 'kredit pintar')

# Wallet providers that can be OUR OWN wallet. A bank debit only counts as an
# own-wallet top-up when the parsed counterparty (recipient/merchant - never
# the raw email body) names one of these, or the row is an explicit wallet
# top-up. 'purchase' is deliberately absent: BCA payment emails all say
# "Transaction Type: PURCHASE", which used to swallow real bill payments.
_WALLET_KEYWORDS = ('gopay', 'go-pay', 'ovo', 'shopeepay', 'shopee pay',
                    'e-wallet', 'ewallet', 'dompet')

# VA acquirers that are never our own wallet by acquirer name alone (ShopeePay
# via AirPay, OVO via Visionet, ...). A bank debit into one of these VAs is
# money out to a wallet account - it goes to Review (or to our own wallet
# top-up, see 0.7), never internal, even when the description says "Top Up".
_THIRD_PARTY_ACQUIRERS = ('airpay', 'visionet')

# Wallet-acquirer VA products: acquirer -> wallet token(s) present in the row.
# Matching one of these marks the debit as a WALLET top-up, not a purchase.
_WALLET_ACQUIRERS = {'airpay': ('shopeepay',), 'visionet': ('ovo',)}
_WALLET_TOPUP_CATS = {'shopeepay': 'topup_shopeepay', 'ovo': 'topup_ovo'}

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
    'topup_shopeepay': ('Transfers', 'ShopeePay Top-up'),
    'topup_ovo':      ('Transfers', 'OVO Top-up'),
    'bills_elec':     ('Utilities', 'Electricity (PLN)'),
    'bills_internet': ('Utilities', 'Internet'),
    'bills_phone':    ('Utilities', 'Mobile & Data'),
    'bills_cc':       ('Utilities', 'Credit Card'),
    'digital':        ('Utilities', 'Digital & Subscriptions'),
    'groceries':      ('Groceries', 'Groceries'),
    'food':            ('Food & Dining', 'Food & Dining'),
    'transport_fuel':   ('Transport', 'Fuel'),
    'transport_toll':  ('Transport', 'Toll'),
    'transport_parking': ('Transport', 'Parking'),
    'ride_hailing':    ('Transport', 'Ride Hailing'),
    'vehicle_service': ('Vehicle', 'Car Service'),
    'home_upkeep':     ('Home', 'Home Maintenance & Repair'),
    'home_loan':       ('Home', 'Home Installment'),
    'project_cost':     ('Work', 'Project Cost'),
    'shopping':        ('Shopping', 'Online Shopping'),
    'leisure':         ('Leisure', 'Leisure'),
    'padel':            ('Leisure', 'Padel'),
    'health_vitamins': ('Health', 'Vitamins'),
    'health_medical':  ('Health', 'Medical'),
    'cash':            ('Cash', 'Cash Withdrawal'),
    'fee':             ('Fees', 'Bank Fee'),
    'loan_repayment':  ('Loans', 'Loan Payment'),
    'pegadaian':      ('Loans', 'Pegadaian'),
    # Manual-only categories (owner assigns by hand; the categorizer never
    # auto-fires them): money received from a friend (income) and money paid
    # back to a friend (expense).
    'friend_loan':    ('Loans', 'Friend Loan'),
    'friend_repay':   ('Loans', 'Friend Repayment'),
    'refund':         ('Income', 'Refund'),
    # Adeeva (owner's daughter): a real, counted personal expense, but
    # never auto-assigned. The payments come off our own cards/accounts and
    # are indistinguishable from our own spending at parse time, so the owner
    # files them by hand. Kept out of the auto-fire path on purpose - a name
    # match would sweep in unrelated merchants.
    'adeeva':         ('Family', 'Adeeva'),
    'dinda':          ('Family', 'Dinda'),
    'savings':        ('Transfers', 'Savings'),
    'maid_salary':    ('Home', 'Maid Salary'),
    'uncategorized':  ('Uncategorized', 'Uncategorized'),
}

# ── keyword maps (the "muscle" - extend these as new merchants appear) ────

# Billers: keyword -> taxonomy key. Order matters (most specific first).
_BILLERS = [
    ('token listrik', 'bills_elec'), ('pln prepaid', 'bills_elec'),
    ('pln', 'bills_elec'),
    ('indihome', 'bills_internet'), ('first media', 'bills_internet'),
    ('biznet', 'bills_internet'),
    ('telkomsel', 'bills_phone'), ('indosat', 'bills_phone'),
    ('pulsa', 'bills_phone'), ('paket data', 'bills_phone'),
    ('kartu kredit', 'bills_cc'), ('payment cc', 'bills_cc'),
    ('credit card', 'bills_cc'),
]

_GROCERIES = ['indomaret', 'alfamart', 'superindo', 'hypermart', 'transmart',
              'ranch market', 'sembako', 'belanja sayur', 'sayur']
_FOOD_DELIVERY = ['gofood', 'grabfood', 'shopeefood', 'go food', 'delivery']
_FOOD_RESTAURANT = ['restoran', 'restaurant', 'rm ', 'warung makan', 'rumah makan']
_FOOD_CAFE = ['cafe', 'kopi', 'café', 'kafé']
_ECOMMERCE = ['tokopedia', 'shopee', 'lazada', 'blibli', 'forumer', ' marketplace']

# Digital subscriptions / SaaS paid by card (e.g. the opencode.ai plan).
_DIGITAL_SUBS = ['opencode']

# Standalone food words -> Food & Dining unconditionally. 'warung'/'warkop' are
# treated as food on their own (no extra food word required). Keep 'nasi' and
# 'warung' whole-word to avoid substring false positives (e.g. "nasional").
_FOOD_WORDS = ['gorengan', 'nasi', 'soto', 'bakso', 'mie', 'mihun', 'kopi',
               'restoran', 'restaurant', 'cafe', 'padang', 'ayam', 'rendang',
               'rujak', 'pecel', 'ikan bak', 'seafood', 'gado-gado', 'penyet',
               'sate', 'warung', 'warkop', 'nasi goreng']

_TRANSPORT_FUEL = ['pertamina', 'bensin', 'solar', 'spbu', 'shell']
_TRANSPORT_TOLL = ['tol', 'toll', 'jalan tol', 'flazz', 'e-money', 'emoney',
                   'mandiri e-money']
_TRANSPORT_PARKING = ['parkir', 'parkmen']
_RIDE_HAILING = ['gojek', 'grab', 'go-ride', 'grabbike', 'bluebird', 'saki',
                 'grabcar', 'gocar', 'maxim']
_VEHICLE_SERVICE = ['bengkel', 'servis', 'service', 'spooring', 'tune-up',
                    'tune up', 'ganti oli', 'kaki-kaki', 'gearbox']
_HOME_UPKEEP = ['tukang', 'plumber', 'ledeng', 'renovasi',
                'perbaikan rumah', 'servis ac', 'ac service', 'kulkas',
                'keran', 'cctv']
_PROJECT_COST = ['domain', 'hosting', 'server', 'vps', 'aws', 'digitalocean',
                 'railway', 'vercel', 'netlify', 'cloudflare', 'namecheap',
                 'github', 'gitlab', 'bitbucket', 'openai', 'anthropic',
                 'google workspace', 'microsoft 365', 'subscription']
_MEDICAL = ['apotek', 'pharmacy', 'farmasi', 'klinik', 'dokter', 'rumah sakit',
            'hospital', 'obat', 'puskesmas', 'laboratorium']
_VITAMINS = ['vitamin', 'suplemen', 'supplement', 'multivitamin']
_LEISURE = ['netflix', 'spotify', 'youtube', 'steam', 'playstation', 'xbox',
            'game', 'gaming', 'bioskop', 'cinema', 'movie', 'konser', 'concert',
            'hobi', 'hobby', 'museum', 'bowling', 'golf', 'karaoke', 'padel',
            'tenis', 'badminton', 'futsal', 'basket']
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
_WORD_BOUNDED = {'pln', 'grab', 'shopee', 'xl', 'sate', 'kopi', 'tol',
                 'nasi', 'warung', 'warkop', 'bakso', 'mie', 'ayam'}


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


# Biller keywords usable for a CARD CHARGE: every _BILLERS entry except the
# card-bill ones ('kartu kredit' / 'credit card' / 'payment cc' describe a
# bill PAYMENT; a charge description always contains 'Kartu Kredit' and
# must never match them).
_CC_CHARGE_BILLERS = [t for t in _BILLERS if t[1] != 'bills_cc']


def _match_cc_merchant(desc: str, row: dict) -> Optional[tuple]:
    """(cat_key, confidence, reason) for an obvious card-charge merchant,
    or None when nothing matches (caller falls to Review)."""
    bill = _match_table(desc, _CC_CHARGE_BILLERS)
    if bill:
        return (bill, 'high', 'CC charge: biller match')
    if any(_has(desc, m) for m in _GROCERIES):
        return ('groceries', 'high', 'CC charge: retail merchant')
    if any(_has(desc, m) for m in _HOME_UPKEEP):
        return ('home_upkeep', 'high', 'CC charge: home repair')
    if any(_has(desc, m) for m in _PROJECT_COST):
        return ('project_cost', 'medium', 'CC charge: project cost')
    if any(_has(desc, m) for m in _MEDICAL):
        return ('health_medical', 'high', 'CC charge: medical / pharmacy')
    if any(_has(desc, m) for m in _VITAMINS):
        return ('health_vitamins', 'high', 'CC charge: vitamins')
    if any(_has(desc, m) for m in _LEISURE):
        return ('leisure', 'high', 'CC charge: leisure')
    if any(_has(desc, m) for m in _FOOD_WORDS):
        return ('food', 'medium', 'CC charge: food word')
    if any(_has(desc, m) for m in _FOOD_RESTAURANT):
        return ('food', 'high', 'CC charge: restaurant')
    if any(_has(desc, m) for m in _FOOD_CAFE):
        return ('food', 'high', 'CC charge: cafe')
    if any(_has(desc, m) for m in _FOOD_DELIVERY):
        return ('food', 'high', 'CC charge: food delivery')
    if any(_has(desc, m) for m in _TRANSPORT_FUEL):
        return ('transport_fuel', 'high', 'CC charge: fuel')
    if any(_has(desc, m) for m in _TRANSPORT_TOLL):
        return ('transport_toll', 'high', 'CC charge: toll')
    if any(_has(desc, m) for m in _TRANSPORT_PARKING):
        return ('transport_parking', 'high', 'CC charge: parking')
    if any(_has(desc, m) for m in _RIDE_HAILING):
        return ('ride_hailing', 'high', 'CC charge: ride hailing')
    if any(_has(desc, m) for m in _VEHICLE_SERVICE):
        return ('vehicle_service', 'high', 'CC charge: vehicle service')
    if any(_has(desc, m) for m in _ECOMMERCE):
        return ('shopping', 'high', 'CC charge: e-commerce')
    if any(_has(desc, m) for m in _DIGITAL_SUBS):
        return ('digital', 'medium', 'CC charge: digital subscription')
    return None


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


def _wallet_acquirer(row) -> Optional[str]:
    """Wallet name when a bank-debit VA is a wallet top-up, else None.

    Only matches when the row names a third-party acquirer VA (AirPay =
    ShopeePay, Visionet = OVO) AND carries the wallet token. 'SHOPEE Bill' (an
    invoice for goods) has no wallet token, so it falls through to the spend
    flow.
    """
    if not _is_third_party_topup(row):
        return None
    text = _text(row)
    for wallets in _WALLET_ACQUIRERS.values():
        for w in wallets:
            if _has(text, w):
                return w
    return None


def _own_wallet_va(row: dict) -> bool:
    """True when a wallet-acquirer VA funds OUR OWN wallet.

    The enriched description carries the holder detail ('Name SAID ISKANDAR',
    unmasked for OVO) and the email's 'Kirim ke Said Iskandar' line. A masked
    holder that is not ours ('DINX LUTXXXXX') proves nothing - such rows stay
    in Review until the wallet owner is verified.
    """
    text = _text(row)
    if 'kirim ke said iskandar' in text:
        return True
    return re.search(r'\bname\s+said iskandar\b', text) is not None


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

    # 0.4 CREDIT-CARD charge (e.g. BRI 'Notification BRI' emails). A card
    #     charge is always a payment - never a transfer, top-up or bill
    #     payment. Obvious merchants file to their category; everything
    #     else falls to Review. The description contains 'Kartu Kredit',
    #     which must NOT match the bills_cc keyword (that key is for bill
    #     PAYMENTS, not charges).
    if tx_type == 'cc_charge':
        cc = _match_cc_merchant(desc, row)
        if cc:
            set_cat(cc[0], 'expense', cc[1], cc[2])
            return hit
        set_fallback('Card charge - assign category')
        return hit

    # 0.5 Explicit fee rows (parser split an admin fee) are always Fees.
    if tx_type == 'fee':
        set_cat('fee', 'fee', 'high', 'Explicit admin fee')
        return hit
    if any(_has(_clean_text(row), kw) for kw in _FEE_KEYWORDS):
        set_cat('fee', 'fee', 'high', 'Fee keyword')
        return hit

    # 0.7 Bank debit into a wallet-acquirer VA (ShopeePay via AirPay, OVO via
    #     Visionet). Product 'SHOPEE Bill' (an invoice for goods) keeps its
    #     place in the spend flow. A wallet top-up addressed to OUR OWN wallet
    #     ('Name SAID ISKANDAR' / 'Kirim ke Said Iskandar') is excluded from
    #     spend; any other holder (masked, e.g. 'DINX LUTXXXXX') goes to
    #     Review - never internal, never spend, until the wallet owner is
    #     verified.
    wallet = _wallet_acquirer(row)
    if wallet is not None:
        if _own_wallet_va(row):
            set_cat(_WALLET_TOPUP_CATS[wallet], 'top_up', 'medium',
                    'Top-up to own wallet')
            return hit
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
    #    VA payments never qualify: their raw body often says 'Transfer to BCA
    #    Virtual Account' but a VA always belongs to a real biller, not a person.
    if direction == 'out' and tx_type != 'va_payment' and _is_transfer_desc(desc):
        if _is_own_name_transfer(row):
            set_cat('internal', 'internal_transfer', 'high',
                    'Transfer to own account')
            return hit
        if _own_person_transfer(conn, row):
            set_cat('internal', 'internal_transfer', 'medium',
                    'Transfer to own (registered) account')
            return hit
        # Family transfers stay in Review so the owner can read the remark
        # (e.g. 'Beli Ayam') and pick the category themselves. Only Dinda is
        # held this way (owner decision).
        if re.search(r'\bdinda\b', _text(row), re.I):
            set_fallback('Family transfer to Dinda - review remarks & assign category')
            return hit
        # Adeeva (owner's daughter) transfers file straight to her category.
        if re.search(r'\badeeva\b', _text(row), re.I):
            set_cat('adeeva', 'transfer_to_person', 'medium',
                    'Transfer to Adeeva')
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
    if direction == 'in' and tx_type != 'va_payment' and _is_transfer_desc(desc):
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
    #     through a VA. All online credit (SPayLater / GoPay Later / Kredivo) and
    #     pawnshop (Pegadaian) -> Loan Repayment. Never guessed.
    if (direction == 'out' and tx_type == 'va_payment'
            and any(_has(_text(row), m) for m in _VA_ONLINE_CREDIT)):
        set_cat('loan_repayment', 'expense', 'high',
                'VA online credit payment')
        return hit
    if (direction == 'out' and tx_type == 'va_payment'
            and any(_has(_text(row), m) for m in _VA_LOAN_BILLERS)):
        set_cat('pegadaian', 'expense', 'high',
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

    # 5b2. PROJECT COST - domain / hosting / SaaS subscriptions for projects.
    if any(_has(desc, m) for m in _PROJECT_COST):
        set_cat('project_cost', 'expense', 'medium', 'Project cost')
        return hit

    # 5c. HEALTH - medical / pharmacy first, then supplements/vitamins. Before
    #     food so an apotek row is never mistaken for a warung.
    if any(_has(desc, m) for m in _MEDICAL):
        set_cat('health_medical', 'expense', 'high', 'Medical / pharmacy')
        return hit
    if any(_has(desc, m) for m in _VITAMINS):
        set_cat('health_vitamins', 'expense', 'high', 'Vitamins / supplements')
        return hit

    # 5d. LEISURE - streaming, games, entertainment.
    if any(_has(desc, m) for m in _LEISURE):
        set_cat('leisure', 'expense', 'high', 'Leisure / entertainment')
        return hit

    # 6. FOOD & DINING - one flat category. Food words (incl. warung/warkop)
    #    fire standalone; restaurant/cafe/delivery are kept for confidence.
    if any(_has(desc, m) for m in _FOOD_WORDS):
        set_cat('food', 'expense', 'medium', 'Food word')
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
    if any(_has(desc, m) for m in _RIDE_HAILING):
        set_cat('ride_hailing', 'expense', 'high', 'Ride hailing')
        return hit
    if any(_has(desc, m) for m in _VEHICLE_SERVICE):
        set_cat('vehicle_service', 'expense', 'high', 'Vehicle service')
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