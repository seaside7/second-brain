"""Mandiri Tabungan e-Statement PDF parser (grid-based).

Mandiri e-statements are ruled tables whose multi-line 'Keterangan' cells
interleave with adjacent rows in plain text-line order, so line-by-line parsing
(as BCA/BNI do) breaks. The parser reads extracted word coordinates instead,
buckets them into the statement's physical columns (No / Tanggal / Keterangan /
Nominal / Saldo) and clusters each row around its line-number token.

Supported: password-encrypted PDFs (e.g. '01041988'). All monetary amounts are
Indonesian format, '.'-thousands ','-decimals, the Nominal column is signed
(+ in / - out) and the Saldo column is the running balance, so every row can be
validated for balance continuity.

Privacy: the full account number appears in the statement header and is never
stored - only the last 4 digits reach the extracted rows.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from . import _common as _c

PARSER_VERSION = '1.0'
PROVIDER = 'mandiri'

# Physical column bands (x0 of the word, in PDF points) on the e-Statement grid.
_X_NO_HI = 36
_X_DATE_LO, _X_DATE_HI = 40, 115
_X_REMARKS_LO, _X_REMARKS_HI = 115, 366
_X_AMOUNT_LO, _X_AMOUNT_HI = 366, 500
_X_BALANCE_LO = 505
# Vertical band around a row's line-number token that captures its whole cell
# (rows sit ~46pt apart, so +/-20pt can never bleed into a neighbouring row).
_ROW_BAND = 20

_MONTH_ABBR = {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
               'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12}

_DATE_RE = re.compile(r'^(\d{2})\s+([A-Za-z]{3})\s+(\d{4})$')
_TIME_RE = re.compile(r'^(\d{1,2}):(\d{2})(?::(\d{2}))?$')
_AMOUNT_RE = re.compile(r'^[+-]?[\d.,]+$')
_BALANCE_RE = re.compile(r'^[\d.,]+$')
_NO_RE = re.compile(r'^\d+$')
_REF_RE = re.compile(r'\b\d{10,16}\b')

_ACCOUNT_RE = re.compile(r'Nomor\s*Rekening/Account\s*Number\s*:\s*(\d+)', re.I)
_OPENING_RE = re.compile(r'Saldo\s*Awal/Initial\s*Balance\s*:\s*([+\-]?[\d.,]+)', re.I)
_CLOSING_RE = re.compile(r'Saldo\s*Akhir/Closing\s*Balance\s*:\s*([+\-]?[\d.,]+)', re.I)
_INCOMING_RE = re.compile(r'Dana\s*Masuk[\w\s/]*:\s*\+\s*([\d.,]+)', re.I)
_OUTGOING_RE = re.compile(r'Dana\s*Keluar[\w\s/]*:\s*-\s*([\d.,]+)', re.I)
_PERIOD_RE = re.compile(r'Periode/Period\s*:\s*(\d{2})\s+([A-Za-z]{3})\s+(\d{4})', re.I)


def _band(x0: float) -> str:
    if x0 < _X_NO_HI:
        return 'no'
    if x0 < _X_DATE_HI:
        return 'date'
    if x0 < _X_REMARKS_HI:
        return 'remarks'
    if x0 < _X_AMOUNT_HI:
        return 'amount'
    return 'balance'


def _row_from_anchor(words: list[dict], anchor_top: float) -> dict | None:
    """Assemble one statement row from the words within a vertical band."""
    cell = {'remarks': [], 'date_parts': [], 'time': '', 'no': '',
            'amount': 0, 'balance': 0, 'amount_sign': ''}
    money_ok = False
    for w in words:
        if abs(w['top'] - anchor_top) > _ROW_BAND:
            continue
        band = _band(w['x0'])
        text = w['text'].strip()
        if band == 'no' and _NO_RE.match(text):
            cell['no'] = text
        elif band == 'date':
            if text == 'WIB' or '.' in text:
                continue
            if _TIME_RE.match(text):
                cell['time'] = text
            else:
                cell['date_parts'].append((w['top'], w['x0'], text))
        elif band == 'remarks':
            cell['remarks'].append((w['top'], w['x0'], text))
        elif band == 'amount' and _AMOUNT_RE.match(text):
            val = _c.parse_amount(text)
            if val != 0 and not money_ok:
                cell['amount'] = abs(val)
                cell['amount_sign'] = '-' if text.startswith('-') else '+'
                money_ok = True
        elif band == 'balance' and _BALANCE_RE.match(text):
            val = _c.parse_amount(text)
            if val != 0:
                cell['balance'] = val
                money_ok = True
    return cell


def _clean_remarks(parts: list[tuple]) -> str:
    parts = sorted(parts, key=lambda t: (t[0], t[1]))
    tokens = [t for _, _, t in parts if t not in ('-', '—', '–', '|')]
    return ' '.join(tokens).strip()


def _type_fields(remarks: str) -> tuple[str, str]:
    """Return (transaction_type, description) from cleaned remarks."""
    up = remarks.upper()
    if any(k in up for k in ('BIAYA ADMINISTRASI', 'BIAYA TRANS', 'BIAYA LAYANAN',
                             'BIAYA SALDO')):
        return 'fee', 'Bank fee'
    if 'TRANSFER BI FAST' in up or 'TRANSFER KE' in up or up.startswith('TRANSFER'):
        return 'transfer', 'Transfer'
    if any(k in up for k in ('TOP-UP', 'TOP UP', 'TOPUP')):
        return 'top_up', 'Top-up e-money'
    if 'PEMBAYARAN QR' in up or 'QRIS' in up:
        return 'payment', 'QRIS payment'
    if any(k in up for k in ('TARIKAN TUNAI', 'PENARIKAN TUNAI', 'ATM')):
        return 'cash_withdrawal', 'ATM withdrawal'
    if 'PENDEDEBITAN' in up or 'AUTO COLL' in up or 'OTOMATIS' in up:
        return 'payment', 'Auto-debit payment'
    if 'ASURANSI' in up:
        return 'payment', 'Insurance premium'
    if up.startswith('PEMBAYARAN'):
        return 'payment', 'Biller payment'
    return '', 'Mandiri transaction'


def _counterparty(remarks: str, direction: str) -> str:
    """Best-effort counterparty name for transfers ('Dari X' / 'Ke X')."""
    for marker in ('DARI', 'KE'):
        m = re.search(rf'\b{marker}\s+([A-Z][^,\d-]*)', remarks, re.IGNORECASE)
        if m:
            name = m.group(1).strip()
            name = re.sub(r'\b\d+\b', '', name).strip(' -')
            return name
    return ''


def _ref(remarks: str) -> str:
    m = _REF_RE.search(remarks)
    return m.group(0) if m else ''


def _src_id(provider_id: str, occurred_at: str, amount: int,
            balance: int, remarks: str, idx: int) -> str:
    raw = hashlib.sha256(
        f'{provider_id}:{occurred_at}:{amount}:{balance}:{remarks[:120]}:{idx}'.encode()
    ).hexdigest()[:16]
    return f'{PROVIDER}-{raw}'


def _metadata(text: str) -> tuple[dict, list[str]]:
    meta = {
        'account_no': '', 'opening_balance': 0, 'closing_balance': 0,
        'incoming_total': 0, 'outgoing_total': 0, 'month_year': '',
    }
    warnings: list[str] = []
    if am := _ACCOUNT_RE.search(text):
        meta['account_no'] = am.group(1)
    if m := _OPENING_RE.search(text):
        meta['opening_balance'] = _c.parse_amount(m.group(1))
    if m := _CLOSING_RE.search(text):
        meta['closing_balance'] = _c.parse_amount(m.group(1))
    if m := _INCOMING_RE.search(text):
        meta['incoming_total'] = _c.parse_amount(m.group(1))
    if m := _OUTGOING_RE.search(text):
        meta['outgoing_total'] = _c.parse_amount(m.group(1))
    if m := _PERIOD_RE.search(text):
        mm = _MONTH_ABBR.get(m.group(2).lower())
        if mm:
            meta['month_year'] = f'{m.group(3)}-{mm:02d}'
    return meta, warnings


def _parse_page(page, idx: int) -> list[dict]:
    words = page.extract_words()
    rows: dict[float, list[dict]] = {}
    for w in words:
        rows.setdefault(w['top'], []).append(w)

    anchors = [w for w in words if _band(w['x0']) == 'no' and _NO_RE.match(w['text'].strip())]
    rows_out: list[dict] = []
    for anchor in sorted(anchors, key=lambda w: w['top']):
        cell = _row_from_anchor(words, anchor['top'])
        if cell is None or not cell.get('amount') or not cell.get('balance'):
            continue
        direction = 'in' if cell['amount_sign'] == '+' else 'out'
        occurred_at = ''
        if cell.get('date_parts'):
            joined = ' '.join(t for _, _, t in sorted(cell['date_parts']))
            m = _DATE_RE.match(joined)
            if m:
                mm = _MONTH_ABBR.get(m.group(2).lower())
                if mm:
                    dd, yyyy = int(m.group(1)), int(m.group(3))
                    occurred_at = _c.iso_datetime(yyyy, mm, dd,
                                                  cell['time'] or '00:00:00')
        remarks = _clean_remarks(cell.get('remarks', []))
        if not occurred_at:
            continue
        txn_type, desc = _type_fields(remarks)
        is_fee = txn_type == 'fee'
        amount = cell['amount']
        ref = _ref(remarks)
        if txn_type == 'transfer':
            recipient = _counterparty(remarks, direction)
            merchant = ''
        else:
            merchant = _clean_remarks(cell.get('remarks', [])).strip(' -')
            recipient = ref
        if is_fee:
            merchant = remarks.replace(f' {ref}', '') if ref else remarks
            merchant = merchant.strip(' -')
            recipient = ''
        rows_out.append({
            'provider': PROVIDER,
            'src_txn_id': _src_id(f'{PROVIDER}:', occurred_at, amount,
                                  cell['balance'], remarks, idx),
            'description': desc,
            'raw_description': remarks,
            'transaction_type': txn_type,
            'merchant': merchant[:120] or '',
            'recipient': recipient[:120] or '',
            'direction': direction,
            'principal_amount': 0 if is_fee else amount,
            'fee_amount': amount if is_fee else 0,
            'total_amount': amount,
            'currency': 'IDR',
            'occurred_at': occurred_at,
            'bank_ref': ref,
            'source_page': 0,
            'raw1': f'{cell.get("no","")} {remarks}',
            'raw2': f'{cell.get("date","")} {cell.get("time","")} '
                    f'{("-" if direction=="out" else "+")}{amount} {cell["balance"]}',
            'parser_version': PARSER_VERSION,
        })
        idx += 1
    return rows_out, idx


def parse_mandiri_pdf(path: str | Path, password: str = '') -> dict:
    """Parse a Mandiri Tabungan e-Statement PDF."""
    path = Path(path)
    warnings: list[str] = []
    all_rows: list[dict] = []
    pages = 0
    statement_period = ''
    account_suffix = ''

    try:
        with _c.open_pdf(str(path), password) as pdf:
            pages = len(pdf.pages)
            idx = 0
            first_text = pdf.pages[0].extract_text() or ''
            meta, warnings = _metadata(first_text)
            account_suffix = meta['account_no'][-4:] if meta['account_no'] else ''
            if meta['month_year']:
                statement_period = meta['month_year']
            for page_idx, page in enumerate(pdf.pages):
                page_text = page.extract_text() or ''
                if 'Saldo Berjalan' in page_text or not page_text.strip():
                    continue
                page_rows, idx = _parse_page(page, idx)
                for row in page_rows:
                    row['source_page'] = page_idx + 1
                    all_rows.append(row)
    except Exception as e:
        warnings.append(f'PDF read error: {e}')

    # Balance-continuity + statement-total validation (never fatal).
    if all_rows:
        prev = meta.get('opening_balance', 0)
        for r in all_rows:
            amount = r['total_amount']
            balance = _c.parse_amount(r['raw2'].split()[-1]) if r['raw2'] else 0
            expected = prev + (amount if r['direction'] == 'in' else -amount)
            if balance and expected != balance:
                warnings.append(
                    f'Row {r["src_txn_id"]}: balance continuity broken '
                    f'({expected} != {balance})')
            prev = balance or prev
        checked_in = sum(r['total_amount'] for r in all_rows if r['direction'] == 'in')
        checked_out = sum(r['total_amount'] for r in all_rows if r['direction'] == 'out')
        if meta.get('incoming_total') and checked_in != meta['incoming_total']:
            warnings.append(f'Incoming sum mismatch ({checked_in} != {meta["incoming_total"]})')
        if meta.get('outgoing_total') and checked_out != meta['outgoing_total']:
            warnings.append(f'Outgoing sum mismatch ({checked_out} != {meta["outgoing_total"]})')
        if meta.get('closing_balance') and prev and prev != meta['closing_balance']:
            warnings.append(f'Closing balance mismatch ({prev} != {meta["closing_balance"]})')

    parts = [str(r.get('src_txn_id', '')) for r in all_rows]
    fingerprint = hashlib.sha256(
        f'{PROVIDER}_pdf:{sorted(parts)}'.encode()).hexdigest()[:32]

    return {
        'rows': all_rows,
        'account_name': 'Tabungan Mandiri',
        'phone_suffix': account_suffix,
        'statement_period': statement_period,
        'parser_version': PARSER_VERSION,
        'warnings': warnings,
        'page_count': pages,
        'fingerprint': fingerprint,
    }