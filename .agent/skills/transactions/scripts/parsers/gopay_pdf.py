"""GoPay PDF statement parser using pdfplumber.

Extracts transaction rows from GoPay (Gojek) statement PDFs.
Handles:
- Table-based statements with wrapped descriptions
- Saldo (cash balance) vs GoPay Coins
- Transaction ID extraction
- Both old and new layout variants
- Page-level parsing (multi-page statements)
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import pdfplumber

PARSER_VERSION = '1.0'

# Amount patterns: "Rp 50.000" → 50000, "-Rp 10.000" → 10000.
# The loose detector also matches dates ("14/06/2025"); _parse_amount()
# gates on anchored patterns so dates never parse as money.
_AMOUNT_RE = re.compile(r'(?:Rp\s*|IDR\s*)?(-?)\s*([\d.,]+)', re.IGNORECASE)
_RP_AMOUNT_RE = re.compile(r'(?:Rp|IDR)\s*\(?(-?)\s*([\d.,]+)\)?', re.IGNORECASE)
_BARE_AMOUNT_RE = re.compile(r'^\s*\(?(-?)\s*([\d]{1,3}(?:[.,]\d{3})+)\s*\)?\s*$')
_DATE_RE = re.compile(
    r'(\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|\d{4}[-/.]\d{1,2}[-/.]\d{1,2})'
    r'(?:\s+(\d{1,2}:\d{2}(?::\d{2})?))?'
)
_TXN_ID_RE = re.compile(r'[A-Z0-9]{8,}')
_GOJEEK_RE = re.compile(r'GoPay|Gojek|GO-PAY', re.IGNORECASE)


@dataclass
class ParsedTxn:
    """One transaction extracted from a GoPay PDF."""
    description: str = ''
    merchant: str = ''
    direction: str = 'out'          # 'in' or 'out'
    principal_amount: int = 0
    fee_amount: int = 0
    total_amount: int = 0
    occurred_at: str = ''           # ISO WIB: 2025-06-15T14:30:00
    src_txn_id: str = ''
    provider: str = 'gopay'
    source_page: int = 0
    wallet_type: str = 'saldo'      # 'saldo' or 'coins'
    balance_after: Optional[int] = None
    raw1: str = ''
    raw2: str = ''

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_amount(text: str) -> int:
    """Parse 'Rp 50.000' → 50000. Dots are thousands, commas are decimals.

    Requires an explicit Rp/IDR prefix, OR a bare number with thousands
    separators (e.g. '50.000').  Dates like '14/06/2025' and mixed text
    like '10:30Belanja' never match, so they parse to 0.
    """
    m = _RP_AMOUNT_RE.search(text)
    if not m:
        m = _BARE_AMOUNT_RE.match(text)
    if not m:
        return 0
    sign = -1 if m.group(1) else 1
    num_str = m.group(2).replace('.', '').replace(',', '')
    try:
        return sign * int(num_str)
    except ValueError:
        return 0


def _parse_date(text: str) -> str:
    """Parse dd/mm/yyyy or yyyy-mm-dd → ISO WIB datetime string."""
    m = _DATE_RE.search(text)
    if not m:
        return ''
    date_str = m.group(1).replace('/', '-').replace('.', '-')
    time_str = m.group(2) or '00:00'
    # Normalize dd-mm-yyyy → yyyy-mm-dd
    parts = date_str.split('-')
    if len(parts) == 3:
        if len(parts[2]) == 4:  # dd-mm-yyyy
            date_str = f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
        elif len(parts[0]) == 4:  # yyyy-mm-dd
            pass
        else:
            date_str = f"20{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
    return f"{date_str}T{time_str}:00" if len(time_str) == 5 else f"{date_str}T{time_str}"


def _is_coins_header(text: str) -> bool:
    return bool(re.search(r'coins|reward|coin\s*balance', text, re.IGNORECASE))


def parse_gopay_pdf(path: str | Path) -> dict:
    """Parse a GoPay statement PDF.

    Returns
    -------
    dict with keys: rows (list[ParsedTxn].to_dict()), account_name, phone_suffix,
    statement_period, wallet_type, parser_version, warnings, page_count.
    """
    path = Path(path)
    warnings: list[str] = []
    rows: list[dict] = []
    pages = 0

    try:
        with pdfplumber.open(str(path)) as pdf:
            pages = len(pdf.pages)
            account_name = ''
            phone_suffix = ''
            statement_period = ''
            current_wallet = 'saldo'

            for page_idx, page in enumerate(pdf.pages):
                text = page.extract_text() or ''

                # Extract account identity from header
                if page_idx == 0:
                    account_name, phone_suffix = _extract_identity(text)
                    statement_period = _extract_period(text)

                # Detect wallet type from section headers
                if _is_coins_header(text):
                    current_wallet = 'coins'

                # Word-based column parsing (handles borderless tables where
                # extract_tables() finds nothing — the real GoPay layout)
                parsed = _parse_page_words(page)
                if parsed:
                    for txn in parsed:
                        txn.wallet_type = current_wallet
                        txn.source_page = page_idx + 1
                        rows.append(txn.to_dict())
                else:
                    # Fallback: line-by-line text parsing
                    lines = text.split('\n')
                    i = 0
                    while i < len(lines):
                        txn = _parse_line_fallback(lines, i, page_idx + 1)
                        if txn:
                            txn.wallet_type = current_wallet
                            rows.append(txn.to_dict())
                            i += 2  # skip description + amount lines
                        else:
                            i += 1

    except Exception as e:
        warnings.append(f'PDF read error: {e}')

    # Post-process: extract txn IDs, normalize amounts
    for r in rows:
        if not r.get('src_txn_id'):
            r['src_txn_id'] = _extract_txn_id(r.get('description', '') + r.get('raw1', ''))
        if not r.get('occurred_at') and r.get('raw1'):
            r['occurred_at'] = _parse_date(r['raw1'])

    # Compute hash fingerprint (string-safe; used for tiebreak dedupe only)
    parts = [str(r.get('src_txn_id', '')), str(r.get('occurred_at', '')),
             str(r.get('total_amount', 0)), str(r.get('description', ''))]
    blob = str(sorted(parts))
    fingerprint = hashlib.sha256(f'gopay_pdf:{blob}'.encode()).hexdigest()[:32]

    return {
        'rows': rows,
        'account_name': account_name,
        'phone_suffix': phone_suffix,
        'statement_period': statement_period,
        'parser_version': PARSER_VERSION,
        'warnings': warnings,
        'page_count': pages,
        'fingerprint': fingerprint,
    }


def _extract_identity(text: str) -> tuple[str, str]:
    """Extract account name and phone suffix from header text."""
    name = ''
    phone = ''
    # Common patterns: "GoPay" + "Said Iskandar" or phone number
    for line in text.split('\n')[:15]:
        if _GOJEEK_RE.search(line):
            continue
        m = re.search(r'(\d{4,})\s*$', line)
        if m:
            phone = m.group(1)
        # Name line: capitalized words, no digits
        if re.match(r'^[A-Z][a-z]+(\s+[A-Z][a-z]+)+$', line.strip()):
            name = line.strip()
    return name, phone


def _extract_period(text: str) -> str:
    """Extract statement period (e.g., 'Juni 2025', '1-30 Jun 2025')."""
    m = re.search(r'(?:per\s+)?(\w+\s+\d{4}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4})',
                  text, re.IGNORECASE)
    return m.group(1) if m else ''


def _parse_page_words(page) -> list[ParsedTxn]:
    """Parse a page using word positions.

    Groups words by y-coordinate into visual lines, splits each line into
    columns by x-gaps, then maps columns to (date, description, txn_id,
    amount).  Returns [] when no amount-bearing rows are found.
    """
    words = page.extract_words(
        x_tolerance=1.5, y_tolerance=3, keep_blank_chars=False)
    if not words:
        return []

    # Group by rounded y ('top')
    lines: dict[int, list] = {}
    for w in words:
        key = round(w['top'] / 3.0) * 3
        lines.setdefault(key, []).append(w)

    results: list[ParsedTxn] = []
    for top in sorted(lines):
        ws = sorted(lines[top], key=lambda w: w['x0'])
        cols = _split_columns(ws)
        if not cols:
            continue
        full = ' '.join(cols)
        lower = full.lower()

        # Skip header / footer / basic rows
        if any(h in lower for h in ('tanggal', 'deskripsi', 'txn id', 'jumlah',
                                    'tipe', 'status', 'saldo', 'keterangan',
                                    'halaman', 'page', 'dicetak', 'printed')):
            continue
        if not _DATE_RE.search(full):
            continue

        txn = ParsedTxn()
        txn.raw1 = full

        # Amount: middle column usually "+/- Rp x / Rp x" or "-Rp x"
        amounts = []
        for c in cols:
            vals = [_parse_amount(c) for c in re.split(r'[|]', c)]
            amounts.extend(v for v in vals if v)
        txn.total_amount = amounts[-1] if amounts else 0
        txn.principal_amount = amounts[-1] if amounts else 0

        if len(amounts) >= 3:
            txn.principal_amount, txn.fee_amount, _ = amounts[-3:]

        # Date from first column
        for c in cols:
            d = _parse_date(c)
            if d:
                txn.occurred_at = d
                break

        # Description = everything except the date text (keep the date column's
        # trailing remainder), txn-id column, and amount columns.
        desc_parts = []
        for c in cols:
            dm = _DATE_RE.search(c)
            if dm:
                rest = c[dm.end():].strip()
                if rest:
                    desc_parts.append(rest)
                continue
            if _TXN_ID_RE.fullmatch(c.strip()):
                continue
            if _AMOUNT_RE.search(c) and _parse_amount(c):
                continue
            desc_parts.append(c)
        txn.description = ' '.join(desc_parts)[:200]

        # Direction from signs / keywords
        if any(kw in lower for kw in ('masuk', 'top up', 'received',
                                       'cashback', 'refund', 'incoming', '+ rp')):
            txn.direction = 'in'
        elif any(kw in lower for kw in ('keluar', 'pembayaran', 'transfer',
                                        '- rp', 'outgoing')):
            txn.direction = 'out'
        elif '-' in (cols[-1] if cols else ''):
            txn.direction = 'out'

        if txn.occurred_at or txn.total_amount:
            results.append(txn)

    # Assign source pages from caller context (filled by caller)
    return results


def _split_columns(ws: list, gap: float = 14.0) -> list[str]:
    """Split words into columns based on horizontal gaps."""
    if not ws:
        return []
    cols: list[list] = [[ws[0]]]
    for w in ws[1:]:
        if w['x0'] - cols[-1][-1]['x1'] > gap:
            cols.append([w])
        else:
            cols[-1].append(w)
    return [' '.join(w['text'] for w in c) for c in cols]


def _extract_txn_id(text: str) -> str:
    """Try to find a transaction ID in text."""
    for m in _TXN_ID_RE.finditer(text):
        candidate = m.group()
        if len(candidate) >= 8 and not candidate.isdigit():
            return candidate
    return ''


def _parse_table_row(row: list, page: int) -> Optional[ParsedTxn]:
    """Try to parse a pdfplumber table row into a ParsedTxn."""
    if not row or len(row) < 3:
        return None
    # Filter out empty cells
    cells = [str(c or '').strip() for c in row]
    if not any(cells):
        return None

    # Try to find date + description + amount in cells
    full_text = ' | '.join(cells)

    # Skip header rows
    if any(h in full_text.lower() for h in ['tanggal', 'date', 'deskripsi', 'amount', 'saldo']):
        return None

    txn = ParsedTxn()
    txn.raw1 = full_text
    txn.source_page = page

    # Date
    for cell in cells:
        d = _parse_date(cell)
        if d:
            txn.occurred_at = d
            break

    # Amount (last numeric cell usually)
    amounts = []
    for cell in reversed(cells):
        val = _parse_amount(cell)
        if val != 0:
            amounts.insert(0, val)

    if amounts:
        # Last amount is total; if 3 amounts: principal, fee, total
        if len(amounts) >= 3:
            txn.principal_amount = amounts[0]
            txn.fee_amount = amounts[1]
            txn.total_amount = amounts[2]
        elif len(amounts) == 2:
            txn.total_amount = amounts[-1]
            txn.principal_amount = amounts[0]
        else:
            txn.total_amount = amounts[0]
            txn.principal_amount = amounts[0]

        # Direction from sign or text
        full_lower = full_text.lower()
        if any(kw in full_lower for kw in ['masuk', 'top up', 'received', 'cashback', 'refund']):
            txn.direction = 'in'
        elif txn.total_amount > 0:
            txn.direction = 'out'

    # Description (remaining non-amount, non-date cells)
    desc_parts = []
    for cell in cells:
        if _parse_date(cell) or (_parse_amount(cell) and _AMOUNT_RE.search(cell)):
            continue
        if cell and not re.match(r'^[|/\\]+$', cell):
            desc_parts.append(cell)
    txn.description = ' | '.join(desc_parts)[:200]

    return txn if txn.occurred_at or txn.total_amount else None


def _parse_line_fallback(lines: list[str], idx: int, page: int) -> Optional[ParsedTxn]:
    """Fallback line-by-line parser for non-table layouts."""
    line = lines[idx].strip()
    if not line:
        return None

    # Check if line contains a date
    d = _parse_date(line)
    if not d:
        return None

    # Next line might be description + amount
    desc_line = lines[idx + 1].strip() if idx + 1 < len(lines) else ''
    amount_line = lines[idx + 2].strip() if idx + 2 < len(lines) else ''

    txn = ParsedTxn()
    txn.occurred_at = d
    txn.raw1 = line
    txn.raw2 = desc_line
    txn.source_page = page

    # Parse amount from desc_line or amount_line
    for candidate in [amount_line, desc_line]:
        val = _parse_amount(candidate)
        if val:
            txn.total_amount = val
            txn.principal_amount = val
            break

    # Description from the next line
    txn.description = desc_line[:200] if desc_line else ''

    if txn.total_amount:
        full_lower = line + ' ' + desc_line
        if any(kw in full_lower.lower() for kw in ['masuk', 'top up', 'received', 'cashback', 'refund']):
            txn.direction = 'in'

    return txn if txn.total_amount else None
