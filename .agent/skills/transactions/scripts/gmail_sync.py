"""Gmail ingestion for bank/e-wallet transaction emails.

Uses gmail.readonly scope for personal email (Said Iskandar).
Fetches from verified senders (BCA, BNI, GoPay/GoTo), extracts transaction
fields, normalises to extracted_txns AND ledger_txns, stored idempotently
via source_key.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Optional

import store

# Verified sender patterns (BCA, BNI, GoPay). GoPay receipts come from
# customers.go-pay.co.id (hyphenated); statements/exported history may come
# from the user's own account in a Gmail "GoPay Transaction History" forward.
_VERIFIED_SENDERS = [
    {'domain': 'klikbca.com', 'provider': 'bca'},
    {'domain': 'bca.co.id', 'provider': 'bca'},
    {'domain': 'bni.co.id', 'provider': 'bni'},
    {'domain': 'go-pay.co.id', 'provider': 'gopay'},
    {'domain': 'gopay.co.id', 'provider': 'gopay'},
]

# Bank/junk subjects that should never become transactions.
_JUNK_SUBJECT_MARKERS = [
    'belum berhasil', 'tidak berhasil', 'gagal', 'failed',
    'unsuccessful', 'chat', 'verifikasi email', 'e-statement',
    'estatement', 'statement', 'promo', 'update', 'pemberitahuan',
]
_JUNK_BODY_MARKERS = [
    'belum berhasil', 'tidak berhasil', 'gagal', 'failed', 'unsuccessful',
]

# Token path for personal Gmail
_TOKEN_PATH = Path(os.environ.get(
    'TRANSACTIONS_GMAIL_TOKEN',
    Path(__file__).resolve().parent.parent.parent.parent / 'workspaces' / 'personal' / 'token_gmail.json'
))

_CREDENTIALS_PATH = Path(os.environ.get(
    'TRANSACTIONS_GMAIL_CREDENTIALS',
    Path(__file__).resolve().parent.parent.parent.parent / 'workspaces' / 'personal' / 'credentials.json'
))

# Sender-scoped query so promos / newsletters never crowd out bank emails.
_SENDER_Q = ' OR '.join(
    f'from:({d})' for d in sorted({s['domain'] for s in _VERIFIED_SENDERS})
)
_DEFAULT_QUERY = f'({_SENDER_Q}) newer_than:30d'

# Owner names that need stripping from wallet recipients (ours, not the wallet).
_OWNER_NAMES = {'said', 'sakd', 'sa', 's.'}


def get_gmail_service():
    """Build Gmail API service using personal token (gmail.readonly)."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError:
        return None

    if not _TOKEN_PATH.exists():
        return None

    creds = Credentials.from_authorized_user_file(
        str(_TOKEN_PATH),
        ['https://www.googleapis.com/auth/gmail.readonly']
    )
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(_TOKEN_PATH, 'w') as f:
                f.write(creds.to_json())
        else:
            return None

    return build('gmail', 'v1', credentials=creds)


def _fetch_message(service, msg_id: str) -> dict:
    """Fetch one full message with quota-friendly backoff.

    Gmail's free quota throttles messages.get (~250 units/min, ~5 units per
    fetch) and returns 403 'Quota exceeded' while throttled - retry with
    exponential backoff instead of failing the whole run.
    """
    delay = 1.0
    for attempt in range(8):
        try:
            return service.users().messages().get(
                userId='me', id=msg_id, format='full').execute()
        except Exception as e:
            msg = str(e)
            if 'rateLimitExceeded' not in msg and 'Quota exceeded' not in msg:
                raise
            if attempt == 7:
                raise
            time.sleep(delay)
            delay = min(60.0, delay * 2)
    return {}


def verify_sender(headers: list[dict]) -> Optional[str]:
    """Check if email is from a verified sender. Returns provider or None."""
    from_addr = ''
    auth_results = ''
    for h in headers:
        name = h.get('name', '').lower()
        value = h.get('value', '')
        if name == 'from':
            from_addr = value.lower()
        if name == 'authentication-results':
            auth_results = value.lower()

    for sender in _VERIFIED_SENDERS:
        if sender['domain'] in from_addr:
            # Allow even without an auth-results header (some providers omit it)
            return sender['provider']

    return None


def sync_gmail(conn: sqlite3.Connection, *,
               query: str = _DEFAULT_QUERY,
               max_results: int = 250) -> dict:
    """Sync transaction emails from Gmail.

    Returns {synced, skipped, failed, errors}. Idempotent: already-processed
    message ids (source_key gmail:{provider}:{msg_id}) are skipped.
    """
    service = get_gmail_service()
    if not service:
        return {'ok': False, 'error': 'Gmail not configured (token missing or invalid)'}

    stats = {'synced': 0, 'skipped': 0, 'failed': 0, 'errors': []}

    try:
        results = service.users().messages().list(
            userId='me', q=query, maxResults=max_results
        ).execute()
        messages = results.get('messages', [])

        for msg in messages:
            msg_id = msg['id']
            try:
                _process_message(conn, service, msg_id, stats)
            except Exception as e:
                stats['failed'] += 1
                stats['errors'].append(f'{msg_id}: {e}')

        # Update history watermark + last sync timestamp
        profile = service.users().getProfile(userId='me').execute()
        store.set_sync_state(conn, 'gmail_history_id', profile.get('historyId', ''))
        store.set_sync_state(conn, 'gmail_last_sync', datetime.now().isoformat())

    except Exception as e:
        stats['errors'].append(f'Sync error: {e}')

    return {'ok': True, **stats}


def _process_message(conn: sqlite3.Connection, service, msg_id: str, stats: dict) -> None:
    """Process a single Gmail message into a ledger transaction (idempotent)."""
    message = _fetch_message(service, msg_id)

    headers = message.get('payload', {}).get('headers', [])

    # Verify sender
    provider = verify_sender(headers)
    if not provider:
        stats['skipped'] += 1
        return

    source_key = f'gmail:{provider}:{msg_id}'
    if store.source_doc_exists(conn, source_key=source_key):
        stats['skipped'] += 1
        return

    subject = next((h['value'] for h in headers if h['name'].lower() == 'subject'), '')
    from_addr = next((h['value'] for h in headers if h['name'].lower() == 'from'), '')
    date_str = next((h['value'] for h in headers if h['name'].lower() == 'date'), '')

    body_text = _extract_body(message.get('payload', {}))
    parsed = _parse_email(body_text, provider, subject, date_str)
    if not parsed:
        stats['skipped'] += 1
        return

    # Create source document (+ batch) and extracted row
    doc_id = store.add_source_document(conn,
        kind='gmail',
        source_key=source_key,
        fingerprint=f'gmail:{msg_id}',
        provider=provider,
        email_sender=from_addr,
        email_subject=subject,
        email_received_at=date_str,
        preview=body_text[:400])

    store.update_source_document(conn, doc_id, status='parsed', parser_version='3.0')

    batch_id = store.add_import_batch(conn, doc_id)
    ext_ids = store.add_extracted_rows(conn, batch_id, doc_id, parsed)
    store.update_import_batch(conn, batch_id, state='committed',
        stats_json=json.dumps({'total_rows': len(parsed), 'acct': 'gmail'}))

    # Auto-create a ledger row per extracted row so the transaction is visible
    # immediately (a split admin fee becomes its own fee ledger row).
    if ext_ids:
        from categorize import categorize_batch, apply_categorization
        uncategorized = False
        for i, ext_id in enumerate(ext_ids):
            p = dict(parsed[i], id=ext_id)
            amount = p.get('total_amount') or p.get('principal_amount') or 0
            ledger_id = store.add_ledger_row(
                conn, ext_id,
                amount=amount,
                direction=p.get('direction', 'out'),
                nature='needs_review',
                confidence='low', confidence_reason=f'email_regex_{provider}',
                evidence_json=json.dumps({'msg_id': msg_id, 'provider': provider}),
                review_status='ok',
            )
            results = categorize_batch(conn, [p])
            if results and results[0].get('category_id'):
                apply_categorization(conn, results[0], ledger_id)
            else:
                uncategorized = True
        store.update_source_document(conn, doc_id, parser_version='3.0',
            error='uncategorized' if uncategorized else '')

    stats['synced'] += 1


def _extract_body(payload: dict) -> str:
    """Recursively extract text from Gmail payload, stripping CSS/script noise."""
    import base64
    text = ''
    if payload.get('mimeType') == 'text/plain' and payload.get('body', {}).get('data'):
        data = base64.urlsafe_b64decode(payload['body']['data'])
        text = data.decode('utf-8', errors='replace')
    elif payload.get('mimeType') == 'text/html' and payload.get('body', {}).get('data'):
        data = base64.urlsafe_b64decode(payload['body']['data'])
        html = data.decode('utf-8', errors='replace')
        # Drop <style>/<script> blocks first so CSS doesn't pollute the text
        # (BNI/wondr emails embed a long style block before the real content).
        html = re.sub(r'<\s*style[^>]*>.*?<\s*/\s*style\s*>', ' ', html,
                      flags=re.IGNORECASE | re.DOTALL)
        html = re.sub(r'<\s*script[^>]*>.*?<\s*/\s*script\s*>', ' ', html,
                      flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'\s+', ' ', text).strip()
    else:
        for part in payload.get('parts', []):
            text += _extract_body(part)
    return text


PARSER_VERSION = '3.0'


def _parse_email(body: str, provider: str, subject: str,
                 date_str: str | None = None) -> Optional[list[dict]]:
    """Provider-aware transaction parser. Returns a list of row dicts
    (principal + optional split fee row), or None for junk/non-transactions."""

    if not body:
        return None

    subject_l = (subject or '').lower()
    body_l = body.lower()

    # Hard reject: failed transactions and non-transaction noise.
    if any(k in subject_l for k in _JUNK_SUBJECT_MARKERS):
        return None
    if any(k in body_l for k in _JUNK_BODY_MARKERS):
        return None

    occurred_at = _parse_date_header(date_str)

    if provider == 'bca':
        rows = _parse_bca(body, subject, occurred_at)
    elif provider == 'bni':
        rows = _parse_bni(body, subject, occurred_at)
    elif provider == 'gopay':
        rows = _parse_gopay(body, subject, occurred_at)
    else:
        return None

    # Normalise every row with parser version + mandatory masking so sensitive
    # fields (meter number, customer id, token, ref) never reach the UI/logs.
    for row in rows:
        row['parser_version'] = PARSER_VERSION
        row['raw_description'] = mask_sensitive(row.get('raw_description', '') or '')
        row['description'] = mask_sensitive(row.get('description', '') or '')
        row['merchant'] = mask_sensitive(row.get('merchant', '') or '')
    return rows


def _parse_bca(body: str, subject: str, occurred_at: str) -> list[dict]:
    body_l = body.lower()

    # myBCA transaction journal: has a Transaction Type label + Amount.
    if 'internet transaction journal' in body_l or 'you just made a transaction' in body_l:
        txn_type = _extract_label(body, ['Transaction Type', 'transaction type',
                                         'Jenis Transaksi', 'Mutation', 'mutasi'])
        amount = _amount_after(body, ['total payment', 'total_payment', 'amount', 'amt'])
        if amount <= 0:
            return []
        method = _label_value(body, 'Transaction Type') or _label_value(body, 'Tipe') or txn_type or ''
        # Direction from the journal's own wording.
        direction = 'in' if any(k in body_l for k in ['credit', 'received', 'dana masuk', 'kredit']) else 'out'

        tx_type, clean = _bca_clean(method, subject, body)
        return [_build_row('bca', clean, direction, amount,
                           _extract_txn_id(body), occurred_at,
                           raw_description=body[:2000], transaction_type=tx_type)]

    # Generic BCA notification: bare amount + bank markers.
    amount = _extract_amount(body)
    if amount <= 0:
        return []
    if not any(k in body_l for k in ['balance', 'saldo', 'transfer', 'trx', 'transaction', 'mutasi', 'qris']):
        return []
    direction = 'in' if any(k in body_l for k in ['credit', 'received', 'diterima', 'masuk', 'kredit']) else 'out'
    tx_type, clean = _bca_clean('', subject, body)
    return [_build_row('bca', clean, direction, amount,
                       _extract_txn_id(body), occurred_at,
                       raw_description=body[:2000], transaction_type=tx_type)]


def _bca_clean(method: str, subject: str, body: str) -> tuple[str, str]:
    """Build a clean description + transaction_type from BCA wording.

    Returns (transaction_type, description). QRIS merchant names come from the
    body (e.g. 'To : warung gorengan elis'); e-wallet top-up is detected by the
    journal's own labels; everything else falls back to the subject.
    """
    body_l = body.lower()
    m = method.lower() if method else ''

    if 'qris' in body_l or 'qris' in m:
        merchant = _label_value(body, 'To') or _label_value(body, 'Merchant')
        if not merchant:
            mm = re.search(r'qris[:\s]+([A-Za-z0-9 .&\-]{2,60})', body, re.IGNORECASE)
            merchant = mm.group(1).strip() if mm else ''
        if merchant:
            return 'qris', f'QRIS - {_display_case(merchant)}'
        return 'qris', 'QRIS'

    if any(k in body_l for k in ['gopay', 'ovo', 'dana', 'shopeepay', 'ewallet wallet', 'e-wallet']):
        wallet = next((w for w in ('GoPay', 'OVO', 'DANA', 'ShopeePay')
                       if w.lower() in body_l), 'e-Wallet')
        return 'top_up', f'{wallet} Top Up'

    if 'topup' in body_l or 'top up' in body_l:
        return 'top_up', 'Wallet Top Up'

    if 'transfer' in body_l or 'trsf' in body_l or 'trf ' in body_l:
        return 'transfer', _display_case(subject[:120] or 'Transfer')

    if method:
        return 'payment', _display_case(method[:120])
    return 'payment', _display_case((subject or 'BCA transaction')[:120])


def _parse_bni(body: str, subject: str, occurred_at: str) -> list[dict]:
    body_l = body.lower()
    subject_l = (subject or '').lower()

    # 1) TOP-UP (BNI -> own wallet like GoPay / OVO). Read the body: Nominal
    #    gives the principal and "Penerima" names the destination wallet.
    #    An explicit admin fee is split into its own Fees row.
    if any(k in subject_l for k in ['top-up', 'top up', 'topup']) or \
            any(k in body_l for k in ['top-up', 'top up', 'topup', 'isi saldo']):
        amount = _extract_amount(body)
        if amount <= 0:
            amount = _amount_after(body, ['nominal', 'amount', 'jumlah'])
        if amount <= 0:
            return []
        recipient = _bni_recipient(body) or 'e-Wallet'
        rows = [_build_row('bni', f'{_display_case(recipient)} Top Up', 'out',
                           amount, _extract_txn_id(body), occurred_at,
                           raw_description=_bni_snippet(body),
                           transaction_type='top_up', recipient=recipient)]
        fee = _bni_admin_fee(body)
        if fee > 0:
            rows[0]['fee_amount'] = fee
            rows[0]['total_amount'] = amount + fee
            rows.append(_build_row(
                'bni', f'{_display_case(recipient)} Top Up - Admin Fee', 'out',
                fee, f'{rows[0]["src_txn_id"]}-fee', occurred_at,
                raw_description=f'Biaya Admin Rp{fee:,}',
                transaction_type='fee',
                principal_amount=0, fee_amount=fee))
        return rows

    # 2a) CASH WITHDRAWAL (ATM / 'Kode tarik tunai').
    if any(k in body_l for k in ['tarik tunai', 'penarikan tunai', 'atm withdrawal',
                                 'cash withdrawal', 'kode transaksi']) \
            or any(k in subject_l for k in ['tarik tunai', 'penarikan tunai', 'withdrawal']):
        amount = _extract_amount(body)
        if amount <= 0:
            amount = _amount_after(body, ['nominal', 'amount', 'jumlah'])
        if amount <= 0:
            return []
        return [_build_row('bni', 'Tarik Tunai', 'out',
                           amount, _extract_txn_id(body), occurred_at,
                           raw_description=_bni_snippet(body),
                           transaction_type='withdrawal')]

    # 2b) QRIS payment. Merchant sits in the 'Penerima <Merchant>, <area>' row.
    if 'qris' in body_l:
        amount = _extract_amount(body)
        if amount <= 0:
            amount = _amount_after(body, ['nominal', 'amount', 'jumlah'])
        if amount <= 0:
            return []
        merchant = _bni_qris_merchant(body)
        clean = f'QRIS - {_display_case(merchant)}' if merchant else 'QRIS Payment'
        return [_build_row('bni', clean, 'out', amount,
                           _extract_txn_id(body), occurred_at,
                           raw_description=_bni_snippet(body),
                           transaction_type='qris', merchant=merchant)]

    # 2) TRANSFER (BNI -> account / person). Use body: recipient name + bank,
    #    notes, and amount. Subject stays only as fallback.
    amount = _extract_amount(body)
    if amount <= 0:
        amount = _amount_after(body, ['nominal', 'amount', 'jumlah'])
    if amount <= 0:
        return []
    if not any(k in body_l for k in ['transfer', 'kirim', 'rekening', 'penerima', 'nominal', 'debet', 'kredit']):
        return []
    # Ignore the disclaimer phrase when sniffing direction ('diterima'-adjacent
    # footer words like 'penerima yang sah' are not a credit).
    direction = ('in' if any(k in body_l for k in ['credit', 'receiv', 'diterima',
                                                   'dana masuk', 'kredit'])
                 else 'out')

    recipient = _bni_transfer_recipient(body)
    # Only the transaction-detail block (head) can carry a real note - the
    # wondr footer contains the word 'Berita' and would otherwise be captured.
    note = (_label_value(body[:800], 'Berita') or _label_value(body[:800], 'Keterangan')
            or _label_value(body[:800], 'Notes'))
    bank = _bni_bank(body)
    if note:
        # Notes like 'Belanja Sayur' read as the transfer purpose.
        clean = _display_case(note)
    elif recipient:
        clean = f'Transfer - {_display_case(recipient)}'
    elif direction == 'in':
        clean = 'Transfer Masuk'
    elif bank:
        clean = f'Transfer ke {_display_case(bank)}'
    else:
        clean = 'Transfer'
    rows = [_build_row('bni', clean, direction, amount,
                       _extract_txn_id(body), occurred_at,
                       raw_description=_bni_snippet(body),
                       transaction_type='transfer', recipient=recipient,
                       merchant=bank)]
    fee = _bni_admin_fee(body)
    if fee > 0:
        rows[0]['fee_amount'] = fee
        rows[0]['total_amount'] = amount + fee
        rows.append(_build_row(
            'bni', f'{clean} - Admin Fee', 'out', fee,
            f'{rows[0]["src_txn_id"]}-fee', occurred_at,
            raw_description=f'Biaya Admin Rp{fee:,}',
            transaction_type='fee',
            principal_amount=0, fee_amount=fee))
    return rows


def _bni_snippet(body: str) -> str:
    """Raw BNI body kept for the audit trail (trimmed to avoid bloat)."""
    return re.sub(r'\s+', ' ', body).strip()[:2000]


def _bni_recipient(body: str) -> str:
    """Extract the destination wallet from a BNI/wondr TOP-UP email
    (e.g. 'GoPay'). Returns '' when not found."""
    m = re.search(r'penerima(?::)?\s+.*?\b([A-Za-z0-9&.\-]+)\s*[•|]\s*\S+',
                  body, re.IGNORECASE | re.DOTALL)
    if m:
        name = m.group(1).strip()
        if len(name) >= 2 and name.lower() not in ('penerima', 'sumber', 'dana'):
            # Skip person names that precede the wallet (SAID ISKANDAR GoPay).
            parts = name.split()
            if parts and parts[0] in _OWNER_NAMES:
                return name.split(' ', 1)[-1] if ' ' in name else name
            return name[:40]
    return ''


def _bni_transfer_recipient(body: str) -> str:
    """Extract the PERSON a BNI transfer goes to, e.g.:
        'Penerima Dinda BCA'            -> 'Dinda'
        'Penerima CHANDRA ARDIKA - BCA' -> 'Chandra Ardika'
    QRIS merchant rows ('Penerima Lautan Juice...') return '' - use
    _bni_qris_merchant for those. Only the head of the body is read so
    wondr's legal footer ('penerima yang sah atas email...') never matches.
    """
    head = body[:700]
    m = re.search(r'penerima\s*[:\-]?\s*([A-Za-z][A-Za-z .\-]{1,50}?)\s*-\s*'
                  r'(?:BCA|BNI|BRI|Mandiri|GoPay|OVO|DANA)\b', head, re.IGNORECASE)
    if not m:
        m = re.search(r'penerima\s*[:\-]?\s*([A-Za-z][A-Za-z .\-]{1,40}?)\s+'
                      r'(?:BCA|BNI|BRI|Mandiri|GoPay|Go\s*Pay|OVO|DANA|ShopeePay)\b',
                      head, re.IGNORECASE)
    if not m:
        m = re.search(r'penerima\s*[:\-]?\s*([A-Za-z][A-Za-z .\-]{1,40}?)\s*[•|]',
                      head, re.IGNORECASE)
    return _clean_counterparty(m.group(1)) if m else ''


def _bni_qris_merchant(body: str) -> str:
    """QRIS merchant from the 'Penerima <Merchant>, <area>' row."""
    m = re.search(r'penerima\s*[:\-]?\s*([A-Za-z][A-Za-z0-9 .,&\-]{2,60}?)\s*'
                  r'(?:sumber dana|detail|#|$)',
                  body[:900], re.IGNORECASE | re.DOTALL)
    if not m:
        return ''
    name = m.group(1).split(',')[0].strip().rstrip('- ').strip()
    return name[:50]


def _clean_counterparty(name: str) -> str:
    """Normalize a recipient/biller name and reject only legal-footer phrases.

    Rejection uses multi-word footer phrases or structural row labels, never
    single words: 'Iskandar' contains 'anda' and 'Aini' contains 'ini', so a
    substring stop-word list would silently destroy legitimate names."""
    name = re.sub(r'\s+', ' ', name).strip().strip('- ').strip()
    low = name.lower()
    if not name or len(name) < 2:
        return ''
    # Structural labels - never a real counterparty.
    if any(w in low for w in ('transaksi', 'tanggal', 'nominal', 'rekening',
                              'total', 'detail', 'sumber dana', 'reference',
                              'biaya admin', 'pembayaran')):
        return ''
    # Legal footer openers (wondr / GoPay disclaimers).
    if any(p in low for p in ('yang sah atas email', 'bukan penerima',
                              'segera menghapus', 'mempercayai kerahasiaan',
                              'atas email ini', 'please delete', 'do not reply')):
        return ''
    # Sentence-like start from the disclaimer is never a name.
    if low.split()[0] in ('penerima', 'bukan', 'anda', 'yang', 'ini', 'dari',
                          'email', 'please', 'sah', 'atas', 'bila', 'kode',
                          'jika', 'biaya'):
        return ''
    return name[:40]


def _bni_bank(body: str) -> str:
    """Extract the destination bank from a BNI transfer email."""
    m = re.search(r'(?:bank tujuan|bank penerima)\s*[:\-]?\s*([A-Za-z .\-]{2,30})',
                  body, re.IGNORECASE)
    if m:
        val = m.group(1).strip()
        # Trim at model numbers / bullets.
        val = re.split(r'\s*(\u2022|•|\|\s*\d)', val)[0].strip()
        if len(val) >= 2:
            return val[:30]
    return ''


def _bni_admin_fee(body: str) -> int:
    """Explicit admin fee on a BNI/wondr transaction. 0 when not stated.
    Handles both 'Biaya Admin Rp2.500' and 'Biaya transaksi Rp1.000'."""
    m = re.search(r'biaya\s+(?:admin|transaksi|administrasi)(?:.{0,50}?)Rp\.?\s*([\d.,]+)',
                  body, re.IGNORECASE | re.DOTALL)
    if not m:
        m = re.search(r'adm\s*:\s*Rp\.?\s*([\d.,]+)', body, re.IGNORECASE)
    return _num_int(m.group(1)) if m else 0


def _parse_gopay(body: str, subject: str, occurred_at: str) -> list[dict]:
    body_l = body.lower()
    subject_l = (subject or '').lower()

    # GoPay emails we care about are receipts / transaction history forwards.
    if 'receipt' not in subject_l and 'riwayat transaksi' not in body_l \
            and 'struk' not in body_l and 'transaction history' not in body_l \
            and 'top up' not in body_l and 'topup' not in body_l \
            and 'gobills' not in body_l:
        return []

    merchant = _extract_biller(body, subject)
    is_bill = ('gobills' in body_l or 'payment receipt' in body_l
               or 'payment detail' in body_l or 'bills' in subject_l)

    # Bill / QRIS / e-money top-up payment: merchant present and (GOBILLS or
    # payment receipt). Admin fee is split into its own Fees row.
    if merchant and (is_bill or 'participant' in body_l or 'qris' in body_l):
        fee = _gopay_admin_fee(body)
        total = _gopay_total(body)
        princ = _gopay_principal(body, total, fee)
        if total <= 0:
            total, princ = princ, princ
        tx_type = 'qris' if ('qris' in body_l or 'qr ' in body_l) else 'bills'
        rows = [_build_row('gopay', _display_case(merchant), 'out',
                           princ, _extract_txn_id(body), occurred_at,
                           raw_description=body[:2000],
                           transaction_type=tx_type, merchant=merchant,
                           fee_amount=fee, total_amount=total)]
        if fee > 0:
            rows.append(_build_row(
                'gopay', f'{_display_case(merchant)} - Admin Fee', 'out',
                fee, f'{rows[0]["src_txn_id"]}-fee', occurred_at,
                raw_description='Admin Fee', transaction_type='fee',
                principal_amount=0, fee_amount=fee, total_amount=fee))
        return rows

    # Wallet top-up credit: GoPay received funding from a bank. Internal move.
    if any(k in body_l for k in ['top up', 'topup', 'isi ulang']):
        w_amount = _amount_after(body, ['total pembayaran', 'total_pembayaran',
                                        'nominal', 'total', 'amount'])
        if w_amount <= 0:
            w_amount = _extract_amount(body)
        if w_amount <= 0:
            return []
        direction = 'in' if any(k in body_l for k in ['dari bca', 'dari bni',
                                                      'dari mandiri', 'masuk',
                                                      'berhasil']) else 'out'
        return [_build_row('gopay', 'GoPay Top Up', direction, w_amount,
                           _extract_txn_id(body), occurred_at,
                           raw_description=body[:2000],
                           transaction_type='top_up', recipient=merchant)]

    amount = _amount_after(body, ['total pembayaran', 'total_pembayaran',
                                  'total', 'payment'])
    if amount <= 0:
        amount = _extract_amount(body)
    if amount <= 0:
        return []

    direction = 'in' if any(k in body_l for k in ['refund', 'cashback', 'credit', 'masuk']) else 'out'
    if merchant:
        clean = _display_case(merchant)
        tx_type = 'qris' if ('qris' in body_l or 'merchant' in body_l) else 'bills'
        return [_build_row('gopay', clean, direction, amount,
                           _extract_txn_id(body), occurred_at,
                           raw_description=body[:2000],
                           transaction_type=tx_type, merchant=clean)]

    # No merchant found - keep the subject but flag for review in the UI.
    clean = _display_case((subject or 'GoPay transaction')[:120])
    tx_type = 'refund' if direction == 'in' and 'refund' in body_l else 'payment'
    return [_build_row('gopay', clean, direction, amount,
                       _extract_txn_id(body), occurred_at,
                       raw_description=body[:2000], transaction_type=tx_type)]


def _gopay_admin_fee(body: str) -> int:
    """Explicit admin fee on a GoPay receipt ('Admin Fee Rp1.500')."""
    m = re.search(r'admin\s+fee|biaya\s+admin', body, re.IGNORECASE)
    if not m:
        return 0
    m = re.search(r'(?:admin\s+fee|biaya\s+admin)(?:.{0,40}?)Rp\.?\s*([\d.,]+)',
                  body, re.IGNORECASE | re.DOTALL)
    return _num_int(m.group(1)) if m else 0


def _gopay_total(body: str) -> int:
    """Final 'Total RpX' on a receipt (takes the LAST 'Total' so the match is
    'Total Rp51.500', not 'Total Amount Rp50.000')."""
    amts = re.findall(r'\bTotal\s+(?:Amount\s+)?Rp\.?\s*([\d.,]+)', body, re.IGNORECASE)
    if amts:
        return _num_int(amts[-1])
    m = re.search(r'(?:total pembayaran|total_pembayaran)(?:.{0,40}?)Rp\.?\s*([\d.,]+)',
                  body, re.IGNORECASE | re.DOTALL)
    if m:
        return _num_int(m.group(1))
    return _extract_amount(body)


def _gopay_principal(body: str, total: int, fee: int) -> int:
    """Explicit principal on a receipt ('Total Amount Rp50.000' / 'Top Up
    Rp50.000'); falls back to total minus fee when not split."""
    m = re.search(r'(?:total amount|total_amount|top up)(?:.{0,40}?)Rp\.?\s*([\d.,]+)',
                  body, re.IGNORECASE | re.DOTALL)
    if m:
        v = _num_int(m.group(1))
        if 0 < v < total:
            return v
    return max(total - fee, 0)


def _extract_biller(body: str, subject: str) -> str:
    """Extract biller/merchant name from a GoPay receipt.

    Prefers the GOBILLS tag ('GOBILLS PLN Token'), then explicit biller rows
    like 'Mandiri e-Money - GoPay Receipt' in subject or body, then 'To:'."""
    m = re.search(r'GOBILLS\s+([A-Za-z0-9 .&\-]{2,40}?)(?=\s+(?:Rp(?!\w)|Total|Riwayat|Struk|Pembayaran|$))',
                  body, re.IGNORECASE)
    if m:
        return m.group(1).replace('  ', ' ').strip()
    for hay in (subject or '', body):
        m = re.search(r'([A-Za-z0-9 .&\-]{2,40}?)\s*-\s*GoPay\s+Receipt', hay,
                      re.IGNORECASE)
        if m:
            return m.group(1).replace('  ', ' ').strip()
    m = re.search(r'\bTo:\s*([A-Za-z0-9 .\-]{2,40})', body, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return ''


def _build_row(provider: str, description: str, direction: str, amount: int,
               src_txn_id: str, occurred_at: str, *,
               raw_description: str = '', transaction_type: str = '',
               recipient: str = '', merchant: str = '',
               principal_amount: int = -1, fee_amount: int = 0,
               total_amount: int = 0) -> dict:
    principal = amount if principal_amount < 0 else principal_amount
    total = total_amount or (principal + fee_amount)
    return {
        'provider': provider,
        'description': description,
        'raw_description': raw_description,
        'transaction_type': transaction_type,
        'merchant': merchant,
        'recipient': recipient,
        'direction': direction,
        'principal_amount': principal,
        'fee_amount': fee_amount,
        'total_amount': total,
        'currency': 'IDR',
        'occurred_at': occurred_at,
        'src_txn_id': src_txn_id or f'gmail_{provider}_{int(datetime.now().timestamp())}',
    }


# ── display + masking helpers ─────────────────────────────────────────────

_DISPLAY_CAPS = {'pln', 'pdam', 'bpjs', 'bca', 'bni', 'bri', 'spbu', 'xl',
                 'idr', 'qr', 'qris', 'vw'}
_DISPLAY_MAP = {'gopay': 'GoPay', 'ovo': 'OVO', 'dana': 'DANA', 'emoney': 'e-Money',
                'e-money': 'e-Money', 'indihome': 'IndiHome', 'telkomsel': 'Telkomsel',
                'shopee': 'Shopee', 'tokopedia': 'Tokopedia',
                'mandiri': 'Mandiri', 'siapa': 'Siapa'}

# Sensitive patterns: masked before storage so they never reach UI / logs / AI.
_SENSITIVE_PATTERNS = [
    # 10-20 digit account / card numbers (masked last-4 style).
    re.compile(r'\b\d{10,16}\b'),
    # meter number / customer id / reference after a label.
    re.compile(r'((?:no\.?\s*(?:meter|pelanggan|rekening)|id\s*(?:pelanggan|customer)|'
               r'kode\s*(?:meter|pelanggan|customer)|referensi|no\.?\s*ref|noref)\s*[:\-]?\s*)'
               r'([A-Za-z0-9\-]{6,})', re.IGNORECASE),
    # 20-char electricity tokens.
    re.compile(r'\b([A-Z0-9]{4,6}(?:-[A-Z0-9]{4,6}){3,4})\b'),
]


def mask_sensitive(text: str) -> str:
    """Mask meter numbers, customer ids, payment refs and electricity tokens."""
    if not text:
        return text
    for pat in _SENSITIVE_PATTERNS:
        text = pat.sub(_mask_match, text)
    return text


def _mask_match(m: re.Match) -> str:
    if m.lastindex == 2:
        label = m.group(1)
        val = m.group(2)
        return f'{label}{val[:2]}***{val[-2:]}'
    raw = m.group(0)
    if len(raw) <= 6:
        return raw
    return f'{raw[:2]}***{raw[-2:]}'


def _num_int(s: str) -> int:
    return _extract_amount(f'Rp{s}') if s else 0


def _display_case(name: str) -> str:
    """Title-case a merchant/biller/counterparty name for display.
    'warung gorengan elis' -> 'Warung Gorengan Elis'; 'pln' -> 'PLN'.
    """
    if not name:
        return name
    words = re.sub(r'\s+', ' ', name).strip().split(' ')
    out = []
    for w in words:
        low = w.lower()
        if low in _DISPLAY_CAPS:
            out.append(low.upper())
        elif low in _DISPLAY_MAP:
            out.append(_DISPLAY_MAP[low])
        elif low.startswith('e-') and len(low) <= 12:
            out.append('e-' + w[2:4].capitalize() + w[4:].lower())
        else:
            out.append(w[:1].upper() + w[1:].lower())
    return ' '.join(out)


def _label_value(text: str, label: str) -> str:
    """Return the value after ``label``, trimmed at the next known label."""
    m = re.search(re.escape(label) + r'\s*[:\-]?\s*', text, re.IGNORECASE)
    if not m:
        return ''
    rest = text[m.end():]
    cut = len(rest)
    for stop in ('Rp', 'IDR', 'Total', 'Nominal', 'Amount', 'Amt', 'Status',
                 'Tanggal', 'Bank Tujuan', 'Reference', 'Ref', 'Rekening', 'Penerima',
                 'Sumber dana', 'Biaya', 'Detail', 'Pesan', 'Keterangan', 'Berita',
                 'No.', 'Waktu', 'Parkir'):
        i = rest.find(stop, 1)
        if 0 < i < cut:
            cut = i
    return rest[:cut].strip(' .,:;|•\u2022').strip()[:120]


def _extract_label(text: str, labels: list[str]) -> str:
    """Return the value following the first matching label, cleaned."""
    for label in labels:
        m = re.search(re.escape(label) + r'\s*[:\-]?\s*([^\n\r]{2,120})', text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return ''


def _amount_after(text: str, labels: list[str]) -> int:
    """First Rp amount following any of the given labels."""
    for label in labels:
        m = re.search(
            re.escape(label) + r'.{0,80}?Rp\.?\s*([\d.,]+)', text, re.IGNORECASE | re.DOTALL)
        if m:
            num_str = m.group(1).replace('.', '').replace(',', '')
            try:
                return int(num_str)
            except ValueError:
                pass
    return _extract_amount(text)


def _extract_amount(text: str) -> int:
    """Extract IDR amount from text. Handles both 'Rp 20.000' and 'IDR 20,000.00'."""
    m = re.search(r'(?:Rp|IDR)\s*([\d.,]+)', text, re.IGNORECASE)
    if not m:
        return 0
    raw = m.group(1)
    # Resolve the decimal separator by looking at the last separator position.
    has_dot   = '.' in raw
    has_comma = ',' in raw
    if has_dot and has_comma:
        if raw.rindex(',') > raw.rindex('.'):
            # "1.000,50" — Indonesian: comma is decimal
            raw = raw.replace('.', '').replace(',', '.')
        else:
            # "20,000.50" — English: dot is decimal
            raw = raw.replace(',', '')
    elif has_dot:
        parts = raw.rsplit('.', 1)
        if len(parts) == 2 and len(parts[1]) == 2:
            # "20,000.00" split already handled above; "20.50" → decimal
            raw = parts[0].replace('.', '') + '.' + parts[1]
        else:
            # "20.000" — thousands, no decimal
            raw = raw.replace('.', '')
    elif has_comma:
        parts = raw.rsplit(',', 1)
        if len(parts) == 2 and len(parts[1]) == 2:
            raw = parts[0].replace(',', '') + '.' + parts[1]
        else:
            raw = raw.replace(',', '')
    try:
        return int(float(raw))
    except (ValueError, OverflowError):
        return 0


def _extract_txn_id(text: str) -> str:
    """Extract transaction reference/ID."""
    patterns = [
        r'(?:ref|reference|no|number)[:\s]+([A-Z0-9-]+)',
        r'(?:trx|transaction)[:\s]+([A-Z0-9-]+)',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1)
    return ''


def _parse_date_header(date_str: str | None) -> str:
    """Normalise Gmail Date header to ISO. Falls back to now."""
    if date_str:
        try:
            dt = parsedate_to_datetime(date_str)
            if dt:
                return dt.strftime('%Y-%m-%dT%H:%M:%S')
        except Exception:
            pass
    return datetime.now().strftime('%Y-%m-%dT%H:%M:%S')