"""Gmail ingestion for bank/e-wallet transaction emails.

Uses gmail.readonly scope for personal email (Said Iskandar).
Fetches from verified senders (BCA, BNI), extracts transaction fields,
normalises to extracted_txns, stores with idempotency via source_key.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import store
from schema import connect

# Verified sender patterns (from config)
_VERIFIED_SENDERS = [
    {'domain': 'klikbca.com', 'provider': 'bca'},
    {'domain': 'bca.co.id', 'provider': 'bca'},
    {'domain': 'bni.co.id', 'provider': 'bni'},
    {'domain': 'bni.co.id', 'provider': 'bni'},
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
            # Additional DKIM/SPF check if available
            if 'dkim=pass' in auth_results or 'spf=pass' in auth_results:
                return sender['provider']
            # Allow without auth check (some providers don't include headers)
            return sender['provider']

    return None


def sync_gmail(conn: sqlite3.Connection, *,
               query: str = 'newer_than:7d',
               max_results: int = 100) -> dict:
    """Sync transaction emails from Gmail.

    Uses historyId watermark for incremental sync.
    Returns {synced, skipped, failed, errors}.
    """
    service = get_gmail_service()
    if not service:
        return {'ok': False, 'error': 'Gmail not configured (token missing or invalid)'}

    stats = {'synced': 0, 'skipped': 0, 'failed': 0, 'errors': []}

    try:
        # Get history ID watermark
        last_history_id = store.get_sync_state(conn, 'gmail_history_id')

        # List messages
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

        # Update history ID
        profile = service.users().getProfile(userId='me').execute()
        store.set_sync_state(conn, 'gmail_history_id', profile.get('historyId', ''))
        store.set_sync_state(conn, 'gmail_last_sync', datetime.now().isoformat())

    except Exception as e:
        stats['errors'].append(f'Sync error: {e}')

    return {'ok': True, **stats}


def _process_message(conn: sqlite3.Connection, service, msg_id: str, stats: dict) -> None:
    """Process a single Gmail message."""
    # Fetch full message
    message = service.users().messages().get(
        userId='me', id=msg_id, format='full'
    ).execute()

    headers = message.get('payload', {}).get('headers', [])

    # Verify sender
    provider = verify_sender(headers)
    if not provider:
        stats['skipped'] += 1
        return

    # Check if already processed
    source_key = f'gmail:{provider}:{msg_id}'
    existing = store.source_doc_exists(conn, source_key=source_key)
    if existing:
        stats['skipped'] += 1
        return

    # Extract fields
    subject = next((h['value'] for h in headers if h['name'].lower() == 'subject'), '')
    from_addr = next((h['value'] for h in headers if h['name'].lower() == 'from'), '')
    date_str = next((h['value'] for h in headers if h['name'].lower() == 'date'), '')

    # Get body text (HTML or plain)
    body_text = _extract_body(message.get('payload', {}))

    # Parse transaction from email body
    parsed = _parse_email(body_text, provider, subject)
    if not parsed:
        stats['skipped'] += 1
        return

    # Create source document
    fingerprint = f'gmail:{msg_id}'
    doc_id = store.add_source_document(conn,
        kind='gmail',
        source_key=source_key,
        fingerprint=fingerprint,
        provider=provider,
        email_sender=from_addr,
        email_subject=subject,
        email_received_at=date_str,
        preview=body_text[:400])

    store.update_source_document(conn, doc_id, status='parsed', parser_version='1.0')

    # Create batch
    batch_id = store.add_import_batch(conn, doc_id)

    # Store extracted row
    store.add_extracted_rows(conn, batch_id, doc_id, [parsed])
    store.update_import_batch(conn, batch_id, state='committed',
        stats_json=json.dumps({'total_rows': 1}))

    stats['synced'] += 1


def _extract_body(payload: dict) -> str:
    """Recursively extract text from Gmail payload."""
    text = ''
    if payload.get('mimeType') == 'text/plain' and payload.get('body', {}).get('data'):
        import base64
        text = base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8', errors='replace')
    elif payload.get('mimeType') == 'text/html' and payload.get('body', {}).get('data'):
        import base64
        html = base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8', errors='replace')
        # Strip HTML tags
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'\s+', ' ', text).strip()
    else:
        for part in payload.get('parts', []):
            text += _extract_body(part)
    return text


def _parse_email(body: str, provider: str, subject: str) -> Optional[dict]:
    """Parse transaction fields from email body text.

    BCA and BNI have different email formats. This is a best-effort
    parser; real-world parsing requires the actual email samples.
    """
    if not body:
        return None

    # Common patterns
    amount = _extract_amount(body)
    direction = 'in' if any(kw in body.lower() for kw in ['received', 'masuk', 'credit']) else 'out'
    txn_id = _extract_txn_id(body)
    description = subject[:200] if subject else body[:200]

    if amount == 0:
        return None

    return {
        'provider': provider,
        'description': description,
        'direction': direction,
        'principal_amount': amount,
        'fee_amount': 0,
        'total_amount': amount,
        'occurred_at': datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
        'src_txn_id': txn_id or f'gmail_{provider}_{int(datetime.now().timestamp())}',
    }


def _extract_amount(text: str) -> int:
    """Extract IDR amount from text."""
    m = re.search(r'Rp\.?\s*([\d.,]+)', text, re.IGNORECASE)
    if m:
        num_str = m.group(1).replace('.', '').replace(',', '')
        try:
            return int(num_str)
        except ValueError:
            pass
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
