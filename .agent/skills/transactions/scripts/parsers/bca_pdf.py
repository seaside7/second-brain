"""BCA statement PDF parser (e-statement 'Rekening Tahapan').

Extracts rows from the text layout BCA emails/export:

    DD/MM <description> <amount> [DB] [<saldo>]
    <continuation lines: merchant / receiver / QR references>

    Types seen: TRANSAKSI DEBIT (QRIS), BIAYA ADM, TRSF E-BANKING CR/DB,
    BI-FAST CR, TARIKAN ATM, BYR VIA E-BANKING, GOPAY TOPUP.
    Direction is read from the DB/CR marker (DB = debit/out, CR = credit/in).
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from . import _common as _c

PARSER_VERSION = '1.0'
PROVIDER = 'bca'

_ROW_RE = re.compile(r'^(\d{1,2})/(\d{1,2})\s+(.*)$')
_DB_MARK_RE = re.compile(r'\sDB(?:\s|$)')
_CR_MARK_RE = re.compile(r'\bCR\b')
_MONEY_RE = re.compile(r'(?:Rp|IDR)?\s*[\d.,]+')
_REF_RE = re.compile(
    r'(\d{4}/[A-Z]+/WS\d{5,}|WSID\d+|'
    r'\d{4}/\d{2,6}|[A-Z0-9]{5,}/[A-Z0-9][A-Z0-9 ]{2,}|'
    r'\d{2,6}/(?:GOPAY|POLYTRON|P\s*GADAI|KREDIVO)[A-Z0-9 ]*)',
    re.IGNORECASE)
_BODY_HEADER = 'TANGGAL KETERANGAN CBG MUTASI SALDO'


def _money_tokens(text: str) -> list[tuple[int, int, int]]:
    """Return [(start, end, value)] for parseable money tokens in text."""
    out = []
    for m in _MONEY_RE.finditer(text):
        val = _c.parse_amount(m.group(0))
        if val != 0:
            out.append((m.start(), m.end(), val))
    return out


def _direction_and_amount(rest: str) -> tuple[str, int]:
    """Return (direction, amount) from a BCA transaction-line tail."""
    # References (e.g. '0208/FTSCY/WS95031') contain digit runs that would
    # otherwise masquerade as money tokens - strip them before scanning.
    stripped = _REF_RE.sub(' ', rest)
    moneys = _money_tokens(stripped)
    if not moneys:
        return '', 0
    if _DB_MARK_RE.search(rest):
        # '<amount> DB [<saldo>]' - amount is the token before the DB marker,
        # which is second-to-last when a trailing saldo is present.
        if len(moneys) >= 2 and re.search(r'\sDB\s+[\d.,]+', rest):
            return 'out', moneys[-2][2]
        return 'out', moneys[-1][2]
    cr = _CR_MARK_RE.search(rest)
    if cr:
        # Bi-FAST credit rows carry 'BIF TRANSFER DR <amount> [<saldo>]'
        dr = re.search(r'BIF\s+TRANSFER\s+DR\s+([\d.,]+)', rest, re.IGNORECASE)
        if dr:
            val = _c.parse_amount(dr.group(1))
            if val:
                return 'in', val
        # Plain credit rows: '<amount> [<saldo>]' - mutasi is the second-to-last
        # token when the running balance shares the line.
        if len(moneys) >= 2:
            return 'in', moneys[-2][2]
        return 'in', moneys[-1][2]
    return '', 0


def _bank_ref(rest: str) -> str:
    m = _REF_RE.search(rest)
    return (m.group(1).strip() if m else '')


def _clean_cont(lines: list[str]) -> str:
    """Join continuation lines into a readable merchant/receiver string."""
    parts = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if s.lower() in ('bersambung ke halaman berikut', 'bersambung'):
            continue
        s = re.sub(r'^TANGGAL\s*:\s*\d{1,2}/\d{1,2}(?:/\d{4})?', '', s).strip()
        s = re.sub(r'(?i)^QRC?\s*\d*', '', s).strip()
        # amounts glued to the name ('00000.00Geprek Gob'): no trailing word
        # boundary required because the name may start right after the digits
        s = re.sub(r'\b\d[\d.,]*\.\d{2}', '', s)
        s = re.sub(r'(?:^|\s)\d{1,4}(?=\s|$)', ' ', s)   # QR branch codes
        s = re.sub(r'\b\d{6,13}\b', '', s)               # phones / long refs
        s = re.sub(r'\b[A-Z0-9]{12,}\b', '', s)          # GoPay transfer IDs
        s = re.sub(r'\s+', ' ', s).strip(' -.,')
        if s:
            parts.append(s)
    return ' '.join(parts)[:200]


def _type_info(rest: str) -> tuple[str, str, str]:
    """Return (transaction_type, description_start, is_fee) from row text."""
    up = rest.upper()
    if 'TRANSAKSI DEBIT' in up:
        return 'payment', 'QRIS payment', False
    if 'BIAYA ADM' in up:
        return 'fee', 'Admin fee', True
    if 'TARIKAN ATM' in up:
        return 'cash_withdrawal', 'ATM withdrawal', False
    if 'BYR VIA E-BANKING' in up:
        return 'payment', 'e-banking payment', False
    if 'BI-FAST CR' in up:
        return 'transfer', 'Bi-FAST transfer', False
    if 'TRSF E-BANKING CR' in up:
        return 'transfer', 'e-banking transfer', False
    if 'TRSF E-BANKING DB' in up:
        return 'transfer', 'e-banking transfer', False
    return '', '', False


def _parse_page(lines: list[str], year: int,
                start_idx: int = 0) -> tuple[list[dict], int]:
    """Parse one page's worth of extracted lines into rows.

    Returns (rows, next_occurrence_index). The occurrence index is folded into
    each row's src_txn_id so identical-looking rows (same date/amount/line)
    stay unique within a statement.
    """
    rows: list[dict] = []
    idx = start_idx
    i = 0
    n = len(lines)
    # Skip the repeated header block until the body column header.
    while i < n and _BODY_HEADER not in lines[i].upper():
        i += 1
    if i < n:
        i += 1

    cur: dict | None = None
    cont: list[str] = []

    def flush():
        nonlocal cur, cont
        if cur is None:
            cur, cont = None, []
            return
        detail = _clean_cont(cont)
        cur['recipient'] = detail[:120]
        if cur['transaction_type'] in ('payment', 'cash_withdrawal',
                                       'fee', 'e-banking payment'):
            cur['merchant'] = detail[:120]
        if not cur['description']:
            cur['description'] = detail or cur['description']
        cur['raw2'] = ' '.join(cont)[:500]
        rows.append(cur)
        cur, cont = None, []

    while i < n:
        line = lines[i].strip()
        up = line.upper()
        # Footer / next-page markers stop this page.
        if (up.startswith('SALDO AWAL :') or up.startswith('SALDO AKHIR :')
                or up.startswith('MUTASI CR') or up.startswith('MUTASI DB')
                or up.startswith('BERSAMBUNG')):
            break
        m = _ROW_RE.match(line)
        if m:
            flush()
            dd, mm = int(m.group(1)), int(m.group(2))
            rest = m.group(3)
            if re.search(r'\bSALDO AWAL\b', rest, re.IGNORECASE):
                i += 1
                continue
            if re.search(r'\bSALDO AKHIR\b', rest, re.IGNORECASE):
                i += 1
                continue
            direction, amount = _direction_and_amount(rest)
            if not direction or amount <= 0:
                i += 1
                continue
            txn_type, desc, is_fee = _type_info(rest)
            occurred_at = _c.iso_datetime(year, mm, dd)
            raw = line
            src_id = hashlib.sha256(
                f'{PROVIDER}:{occurred_at}:{amount}:{raw[:80]}:{idx}'.encode()
            ).hexdigest()[:16]
            idx += 1
            cur = {
                'provider': PROVIDER,
                'src_txn_id': f'{PROVIDER}-{src_id}',
                'description': desc,
                'raw_description': rest,
                'transaction_type': txn_type,
                'merchant': '',
                'recipient': '',
                'direction': direction,
                'principal_amount': (0 if is_fee else amount),
                'fee_amount': (amount if is_fee else 0),
                'total_amount': amount,
                'currency': 'IDR',
                'occurred_at': occurred_at,
                'bank_ref': _bank_ref(rest),
                'source_page': 0,
                'raw1': raw,
                'raw2': '',
                'parser_version': PARSER_VERSION,
            }
            cont = []
        elif cur is not None:
            cont.append(line)
        i += 1
    flush()
    return rows, idx


def parse_bca_pdf(path: str | Path, password: str = '') -> dict:
    """Parse a BCA 'Rekening Tahapan' statement PDF."""
    path = Path(path)
    warnings: list[str] = []
    all_rows: list[dict] = []
    pages = 0
    year = -1
    account_no = ''
    statement_period = ''

    try:
        with _c.open_pdf(str(path), password) as pdf:
            pages = len(pdf.pages)
            occurrence = 0
            for page_idx, page in enumerate(pdf.pages):
                text = page.extract_text() or ''
                lines = text.split('\n')
                if page_idx == 0:
                    y = _c.give_period_year(lines)
                    year = y if y > 0 else 2026
                    pmatch = re.search(
                        r'PERIODE\s*:\s*([A-Za-z]+)\s+(\d{4})', text,
                        re.IGNORECASE)
                    if pmatch:
                        statement_period = (
                            f'{pmatch.group(1)} {pmatch.group(2)}')
                    am = re.search(r'NO\.\s*REKENING\s*:\s*(\d+)', text,
                                   re.IGNORECASE)
                    if am:
                        account_no = am.group(1)
                page_rows, occurrence = _parse_page(lines, year, occurrence)
                for row in page_rows:
                    row['source_page'] = page_idx + 1
                    all_rows.append(row)
    except Exception as e:
        warnings.append(f'PDF read error: {e}')

    parts = [str(r.get('src_txn_id', '')) for r in all_rows]
    fingerprint = hashlib.sha256(
        f'{PROVIDER}_pdf:{sorted(parts)}'.encode()).hexdigest()[:32]

    return {
        'rows': all_rows,
        'account_name': '',
        'phone_suffix': account_no,
        'statement_period': statement_period,
        'parser_version': PARSER_VERSION,
        'warnings': warnings,
        'page_count': pages,
        'fingerprint': fingerprint,
    }