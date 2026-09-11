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
    message = service.users().messages().get(
        userId='me', id=msg_id, format='full'
    ).execute()

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

    store.update_source_document(conn, doc_id, status='parsed', parser_version='2.0')

    batch_id = store.add_import_batch(conn, doc_id)
    ext_ids = store.add_extracted_rows(conn, batch_id, doc_id, [parsed])
    store.update_import_batch(conn, batch_id, state='committed',
        stats_json=json.dumps({'total_rows': 1, 'acct': 'gmail'}))

    # Auto-create the ledger row so the transaction is visible immediately.
    amount = parsed.get('total_amount') or parsed.get('principal_amount') or 0
    if ext_ids:
        store.add_ledger_row(
            conn, ext_ids[0],
            amount=amount,
            direction=parsed.get('direction', 'out'),
            nature='needs_review',
            confidence='low', confidence_reason=f'email_regex_{provider}',
            evidence_json=json.dumps({'msg_id': msg_id, 'provider': provider}),
            review_status='ok',
        )

    stats['synced'] += 1


def _extract_body(payload: dict) -> str:
    """Recursively extract text from Gmail payload."""
    import base64
    text = ''
    if payload.get('mimeType') == 'text/plain' and payload.get('body', {}).get('data'):
        data = base64.urlsafe_b64decode(payload['body']['data'])
        text = data.decode('utf-8', errors='replace')
    elif payload.get('mimeType') == 'text/html' and payload.get('body', {}).get('data'):
        data = base64.urlsafe_b64decode(payload['body']['data'])
        html = data.decode('utf-8', errors='replace')
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'\s+', ' ', text).strip()
    else:
        for part in payload.get('parts', []):
            text += _extract_body(part)
    return text


def _parse_email(body: str, provider: str, subject: str,
                 date_str: str | None = None) -> Optional[dict]:
    """Provider-aware transaction parser. Returns None for junk/non-transactions."""
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
        return _parse_bca(body, subject, occurred_at)
    if provider == 'bni':
        return _parse_bni(body, subject, occurred_at)
    if provider == 'gopay':
        return _parse_gopay(body, subject, occurred_at)
    return None


def _parse_bca(body: str, subject: str, occurred_at: str) -> Optional[dict]:
    body_l = body.lower()
    subject_l = (subject or '').lower()

    if 'internet transaction journal' in body_l or 'you just made a transaction' in body_l:
        # myBCA transaction journal: Status / Transaction Type / Amount
        amount = _amount_after(body, ['total payment', 'total_payment', 'amount'])
        if amount <= 0:
            return None
        direction = 'in' if any(k in body_l for k in ['credit', 'received', 'dana masuk']) else 'out'
        txn_type = _extract_label(body, ['Transaction Type', 'transaction type', 'Jenis Transaksi'])
        desc = (subject[:180] or 'myBCA transaction') + (f' - {txn_type}' if txn_type else '')
        return _build_row('bca', desc, direction, amount, _extract_txn_id(body), occurred_at)

    # Generic BCA notification with a bare amount + bank markers.
    amount = _extract_amount(body)
    if amount <= 0:
        return None
    if not any(k in body_l for k in ['balance', 'saldo', 'transfer', 'trx', 'transaction', 'mutasi']):
        return None
    direction = 'in' if any(k in body_l for k in ['credit', 'received', 'diterima', 'masuk']) else 'out'
    return _build_row('bca', subject[:200] or 'BCA transaction', direction, amount,
                      _extract_txn_id(body), occurred_at)


def _parse_bni(body: str, subject: str, occurred_at: str) -> Optional[dict]:
    body_l = body.lower()
    subject_l = (subject or '').lower()

    if 'top-up berhasil' in subject_l:
        # wondr top-up: money left the BNI account into the wallet.
        amount = _extract_amount(body)
        if amount <= 0:
            return None
        desc = ' '.join((subject or '').split())[:160] or 'BNI top-up'
        return _build_row('bni', desc, 'out', amount, _extract_txn_id(body), occurred_at)

    amount = _extract_amount(body)
    if amount <= 0:
        return None
    if not any(k in body_l for k in ['nominal', 'amount', 'jumlah', 'transaksi', 'debet', 'kredit']):
        return None
    direction = 'in' if any(k in body_l for k in ['credit', 'masuk', 'received']) else 'out'
    return _build_row('bni', subject[:200] or 'BNI transaction', direction, amount,
                      _extract_txn_id(body), occurred_at)


def _parse_gopay(body: str, subject: str, occurred_at: str) -> Optional[dict]:
    body_l = body.lower()
    subject_l = (subject or '').lower()

    # GoPay receipts: "GoPay bills transaction receipt", "Riwayat transaksi tagihan GoPay", etc.
    if 'receipt' not in subject_l and 'riwayat transaksi' not in body_l \
            and 'struk' not in body_l and 'transaction history' not in body_l:
        return None

    amount = _amount_after(body, ['total pembayaran', 'total_pembayaran', 'total', 'payment'])
    if amount <= 0:
        return None
    direction = 'in' if any(k in body_l for k in ['refund', 'cashback', 'credit', 'masuk']) else 'out'
    return _build_row('gopay', subject[:200] or 'GoPay transaction', direction, amount,
                      _extract_txn_id(body), occurred_at)


def _build_row(provider: str, description: str, direction: str, amount: int,
               src_txn_id: str, occurred_at: str) -> dict:
    return {
        'provider': provider,
        'description': description,
        'direction': direction,
        'principal_amount': amount,
        'fee_amount': 0,
        'total_amount': amount,
        'currency': 'IDR',
        'occurred_at': occurred_at,
        'src_txn_id': src_txn_id or f'gmail_{provider}_{int(datetime.now().timestamp())}',
    }


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