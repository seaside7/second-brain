"""Shared helpers for the statement-PDF parsers (GoPay / BCA / BNI)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pdfplumber


def open_pdf(path: str | Path, password: str = ''):
    """Open a statement PDF, allowing an optional decryption password.

    Unencrypted files open on the first attempt; encrypted files (BNI
    e-statements) need the password. A wrong/non-empty password never breaks
    an unencrypted file because the no-password attempt succeeds first.
    """
    try:
        return pdfplumber.open(str(path))
    except Exception:
        if password:
            return pdfplumber.open(str(path), password=password)
        raise


def parse_amount(text: str) -> int:
    """Parse an Indonesian rupiah amount to whole rupiah (int).

    Handles 'Rp 50.000', '-Rp50.000', '+19.393.125', '-29,990', '2,000.00'
    and '(1.000)'. Returns 0 when the text is not clearly a money amount.
    """
    if not text:
        return 0
    t = str(text).strip()
    neg = t.startswith('-') or t.startswith('(')
    t = t.replace('Rp.', '').replace('Rp', '').replace('IDR', '')
    t = t.replace(' ', '').replace('\u00a0', '').lstrip('+-').strip()
    if t.startswith('(') and t.endswith(')'):
        neg = True
        t = t[1:-1]
    if not re.fullmatch(r'[\d.,]+', t):
        return 0

    int_part, frac = t, ''
    if ',' in t and '.' in t:
        # Last separator wins as the decimal point (BCA: 1,234.56)
        if t.rfind('.') > t.rfind(','):
            int_part, frac = t[:t.rfind('.')], t[t.rfind('.') + 1:]
            int_part = int_part.replace(',', '')
        else:
            int_part, frac = t[:t.rfind(',')], t[t.rfind(',') + 1:]
            int_part = int_part.replace('.', '')
    elif ',' in t:
        head, tail = t.rsplit(',', 1)
        if len(tail) == 3 and re.fullmatch(r'\d{1,3}(?:,\d{3})*', t):
            int_part = t.replace(',', '')
        else:
            int_part, frac = head, tail
    elif '.' in t:
        head, tail = t.rsplit('.', 1)
        if len(tail) == 3 and re.fullmatch(r'\d{1,3}(?:\.\d{3})*', t):
            int_part = t.replace('.', '')
        else:
            int_part, frac = head, tail

    try:
        value = int(int_part or '0')
        if frac and frac.isdigit():
            value += int(frac) / (10 ** len(frac))
    except ValueError:
        return 0
    return int(round(value)) * (-1 if neg else 1)


_MONTHS_EN = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}
_MONTHS_ID = {
    'januari': 1, 'februari': 2, 'maret': 3, 'april': 4, 'mei': 5,
    'juni': 6, 'juli': 7, 'agustus': 8, 'september': 9, 'oktober': 10,
    'november': 11, 'desember': 12,
}

_PERIOD_RE = re.compile(
    r'(?:periode[:\s]+|periode\s+transaksi\s*:\s*)'
    r'(\d{1,2}(?:[-/ ]\s*)?\w+[-/ ]?\d{4}|\d{1,2}[-/ ]\d{1,2}[-/ ]\d{4})',
    re.IGNORECASE)


def iso_datetime(year: int, month: int, day: int,
                 time_s: str = '00:00') -> str:
    hh, mm, ss = '00', '00', '00'
    if time_s:
        parts = time_s.split(':')
        if len(parts) >= 1:
            hh = parts[0].zfill(2)
        if len(parts) >= 2:
            mm = parts[1].zfill(2)
        if len(parts) >= 3:
            ss = parts[2].zfill(2)
    return (f'{year:04d}-{int(month):02d}-{int(day):02d}'
            f'T{hh}:{mm}:{ss}')


def month_key(occurred_at: str) -> str:
    """Return the 'YYYY-MM' portion of an ISO datetime, or ''."""
    return (occurred_at or '')[:7]


def norm_amount_text(text: str) -> str:
    """Stub kept for symmetry; returns the input unchanged."""
    return text


def give_period_year(lines: list[str]) -> int:
    """Best-effort year from a statement header period line."""
    for line in lines[:40]:
        m = re.search(r'(20\d{2})', line)
        if m:
            return int(m.group(1))
    return -1