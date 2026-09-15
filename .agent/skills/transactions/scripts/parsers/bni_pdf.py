"""BNI statement PDF parser (e-statement 'Laporan Mutasi Rekening').

BNI exports a three-line record per transaction:

    DD Mon YYYY <Rincian Transaksi type>
    <+/-amount> <saldo>
    HH:MM:SS WIB <detail>

Direction comes from the amount sign. The whole file is encrypted and needs
the customer password (supplied via ``password=``).
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from . import _common as _c

PARSER_VERSION = '1.0'
PROVIDER = 'bni'

_ROW1_RE = re.compile(
    r'^(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+'
    r'(\d{4})\s+(.*)$',
    re.IGNORECASE)
_ROW2_RE = re.compile(r'^([+-]?[\d.,]+)\s+([\d.,]+)$')
_ROW3_RE = re.compile(r'^(\d{2}:\d{2}:\d{2})\s+WIB\s+(.*)$', re.IGNORECASE)
_MONTH_MAP = _c._MONTHS_EN

_TYPE_MAP = [
    ('pembayaran qris', 'payment'),
    ('pembayaran', 'payment'),
    ('transfer', 'transfer'),
    ('biaya', 'fee'),
    ('lainnya', 'other'),
    ('ewallet', 'ewallet_topup'),
]


def _record_type(text: str) -> str:
    low = text.lower()
    for key, txn_type in _TYPE_MAP:
        if low.startswith(key) or key in low:
            return txn_type
    return 'other'


def _parse_lines(lines: list[str], year_hint: int) -> list[dict]:
    """Parse BNI statement lines into extracted-row dicts."""
    rows: list[dict] = []
    i = 0
    n = len(lines)
    while i < n:
        m = _ROW1_RE.match(lines[i].strip())
        if not m:
            i += 1
            continue
        day, mon_name, year, rest = m.group(1), m.group(2), m.group(3), m.group(4)
        if not rest.strip():
            i += 1
            continue
        # Skip the 'Saldo Awal / Saldo Akhir' pseudo-records.
        low_rest = rest.lower()
        if 'saldo awal' in low_rest or 'saldo akhir' in low_rest:
            i += 1
            continue
        if (i + 2) >= n:
            i += 1
            continue
        m2 = _ROW2_RE.match(lines[i + 1].strip())
        m3 = _ROW3_RE.match(lines[i + 2].strip())
        if not m2 or not m3:
            i += 1
            continue

        raw_amount = m2.group(1)
        amount = _c.parse_amount(raw_amount)
        if amount == 0:
            i += 1
            continue

        time_s, detail = m3.group(1), m3.group(2).strip()
        detail = detail.lstrip('- ').strip()[:200]
        month = int(_MONTH_MAP.get(mon_name[:3].lower(), 1))
        occurred_at = _c.iso_datetime(int(year), month, int(day), time_s)

        txn_type = _record_type(rest)
        is_fee = txn_type == 'fee'
        src_key = hashlib.sha256(
            f'{occurred_at}:{abs(amount)}:{detail}:{time_s}'.encode()
        ).hexdigest()[:16]

        rows.append({
            'provider': PROVIDER,
            'src_txn_id': f'{PROVIDER}-{src_key}',
            'description': detail or rest.strip(),
            'raw_description': rest.strip(),
            'transaction_type': txn_type,
            'merchant': detail if txn_type in ('payment', 'other') else '',
            'recipient': detail if txn_type == 'transfer' else '',
            'direction': 'in' if raw_amount.lstrip().startswith('+') else 'out',
            'principal_amount': (0 if is_fee else abs(amount)),
            'fee_amount': (abs(amount) if is_fee else 0),
            'total_amount': abs(amount),
            'currency': 'IDR',
            'occurred_at': occurred_at,
            'bank_ref': '',
            'source_page': 0,
            'raw1': ' | '.join(lines[i:i + 3])[:500],
            'raw2': '',
            'parser_version': PARSER_VERSION,
        })
        i += 3
    return rows


def parse_bni_pdf(path: str | Path, password: str = '') -> dict:
    """Parse a BNI 'Laporan Mutasi Rekening' PDF (password required)."""
    path = Path(path)
    warnings: list[str] = []
    all_rows: list[dict] = []
    pages = 0
    account_name = ''
    statement_period = ''

    try:
        with _c.open_pdf(str(path), password) as pdf:
            pages = len(pdf.pages)
            for page_idx, page in enumerate(pdf.pages):
                text = page.extract_text() or ''
                lines = text.split('\n')
                if page_idx == 0:
                    pm = re.search(r'Periode\s*:\s*(.+)', text, re.IGNORECASE)
                    if pm:
                        statement_period = pm.group(1).strip()
                    am = re.search(r'([A-Z][A-Z0-9 .:\-]+)\s*-\s*\d{9,}',
                                   text)
                    if am:
                        account_name = am.group(1).strip()
                for row in _parse_lines(lines, 0):
                    row['source_page'] = page_idx + 1
                    all_rows.append(row)
    except Exception as e:
        warnings.append(f'PDF read error: {e}')

    parts = [str(r.get('src_txn_id', '')) for r in all_rows]
    fingerprint = hashlib.sha256(
        f'{PROVIDER}_pdf:{sorted(parts)}'.encode()).hexdigest()[:32]

    return {
        'rows': all_rows,
        'account_name': account_name,
        'phone_suffix': '',
        'statement_period': statement_period,
        'parser_version': PARSER_VERSION,
        'warnings': warnings,
        'page_count': pages,
        'fingerprint': fingerprint,
    }