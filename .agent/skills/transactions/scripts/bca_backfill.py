"""BCA statement one-time history backfill + cross-source dedup.

Two jobs:

1. Import a myBCA transaction statement (1-13 Sep 2026) as a one-shot,
   idempotent batch. A source document keyed `bca_statement:<end_date>` guards
   the import; row-level matching guards against the same transaction already
   being present from an email import.

2. Shared matcher so the Gmail pipeline can finalize a *pending* statement row
   (evidence_json.pending=True) when the confirmed email lands, instead of
   inserting a duplicate.

Matching never keys on amount+description alone. Confidence comes from:
  account/date + direction + exact amount  (base)
  + transaction reference match            (strong)
  + normalized recipient/merchant tokens   (strong)
Uncertain matches are inserted as NEW rows flagged for Review (never silently
merged). The raw multiline statement text is stored in `raw1` (never
`raw_description`, which would leak "TRSF E-BANKING" into the categorizer).
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
from datetime import datetime
from typing import Optional

import store

# Transferred money between OUR own accounts on both banks is one event
# (mirrors categorize._OWN_NAMES).
_OWN_NAMES = ['said iskandar', 'iskandar']
_OWN_VA_DISPLAY = {'gopay topup': 'GoPay Top Up', 'p gadai indo': 'Pegadaian',
                   'spaylater': 'SPayLater', 'dana': 'DANA',
                   'spinjam': 'Spinjam', 'polytron ev': 'POLYTRON EV'}

_DATE_RE = re.compile(r'^(\d{2})/(\d{2})/(\d{4})\b')
_PEND_RE = re.compile(r'^PEND\b')
_MONEY_TOKEN_RE = re.compile(r'^[0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})?$')
_REF_RE = re.compile(r'(\d{4}/[A-Z]{2,5}/[^\s]+)')       # 0109/FTSCY/WS95031
_MONEY_ONLY_RE = re.compile(r'^[\d.,]+$')
_PHONE_RE = re.compile(r'^\+?\d{9,16}$')
_BILLER_RE = re.compile(r'/\s*([A-Za-z0-9][A-Za-z0-9 ]{0,40}?)(?=-)')
_AMOUNT_GLUE_RE = re.compile(r'\b\d[\d,]*\.\d{2}(?=[A-Za-z]|\b)')
_QR_MERCHANT_RE = re.compile(
    r'(?:QRC?\s+\d{2,4}\s+[\d.,]+\s*)([A-Za-z0-9][A-Za-z0-9 .&\'\-]*)$')

_STRUCTURAL_MARKERS = ('TRSF', 'BI-FAST', 'FTSCY', 'FTFVA', 'TRB', 'TRANSFER',
                       'QR ', 'BIAYA', 'SETORAN', 'TRANSAKSI', 'TGL:',
                       'TOP UP', 'TOPUP', 'ISSUED', 'ADM')


def _branch_match(line: str):
    """Return (fragment, branch, amount, direction, balance) or None.

    Statement rows end in `[fragment] <branch> <amount> <DB|CR> <balance>`.
    The paste is tab-delimited; a fragment-less line starts with the branch.
    Whitespace fallback keeps the parser robust to pastes that lost tabs.
    """
    parts = [p.strip() for p in line.split('\t')]
    if len(parts) == 5:
        frag, branch, amt, d, bal = parts
    elif len(parts) == 4:
        frag, branch, amt, d, bal = '', *parts
    else:
        frag = branch = amt = d = bal = None
    if (frag is not None and d in ('DB', 'CR')
            and re.fullmatch(r'\d{4}', branch)
            and _MONEY_TOKEN_RE.match(amt) and _MONEY_TOKEN_RE.match(bal)):
        return frag, branch, amt, d, bal
    if '\t' in line:
        return None
    m = re.match(
        r'^([^\t0-9][^\t]*?)?[\t ]+([0-9]{4})[\t ]+'
        r'([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})?)[\t ]+(DB|CR)[\t ]+'
        r'([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})?)$', line)
    if m:
        return (m.group(1) or ''), m.group(2), m.group(3), m.group(4), m.group(5)
    return None


def _money(s: str) -> int:
    s = s.strip().replace(',', '')
    if '.' in s:
        s = s.split('.')[0]
    return int(re.sub(r'\D', '', s)) or 0


def _date_iso(dd: str, mm: str, yyyy: str) -> str:
    return f'{yyyy}-{mm}-{dd}'


def _build_source_key(end_date: str) -> str:
    return f'bca_statement:{end_date}'


def statement_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:32]


def _name_tokens(text: str) -> set:
    return {t for t in re.findall(r"[a-z0-9']+", (text or '').lower())
            if len(t) >= 4 and not t.isdigit()}


def _structural_line(line: str) -> bool:
    up = line.upper().strip()
    if not up or up in ('-', 'N/A', 'NA', 'NONE', '0'):
        return True
    if _MONEY_ONLY_RE.match(up) or _PHONE_RE.match(up):
        return True
    if _REF_RE.search(up):
        return True
    return any(m in up for m in _STRUCTURAL_MARKERS)


def _name_lines(dlines: list) -> list:
    """Description sub-lines that are likely names/notes (non-structural)."""
    return [l.strip() for l in dlines if not _structural_line(l)]


def _va_biller(text: str, ref: str = '') -> str:
    """Extract a VA biller display name from the FTFVA description text (e.g.
    '.../GOPAY TOPUP - - 081998986707' -> 'GoPay Top Up'), or '' when unclear."""
    raw = text or ''
    m = _BILLER_RE.search(raw)
    biller = (m.group(1).strip() if m else '')
    if not biller or len(biller) < 3 or biller.lower() in ('db', 'cr'):
        return ''
    key = biller.lower()
    for k, v in _OWN_VA_DISPLAY.items():
        if k in key:
            return v
    if any(ch.isalpha() for ch in biller):
        return biller.title()
    return ''


# ── parser ─────────────────────────────────────────────────────────────────

def parse_pasted_statement(text: str, *, end_date: str = '2026-09-13') -> tuple[list, dict]:
    """Parse a pasted (tab/space-formatted) myBCA statement.

    Rows span newlines (multiline description cells); a row ends at its
    `<branch> <amount> <DB|CR> <balance>` line. Returns (rows, meta).
    """
    rows: list[dict] = []
    meta = {'raw_rows': 0, 'pending': 0, 'parsed': 0, 'errors': []}
    cur: Optional[dict] = None
    desc_lines: list[str] = []

    for raw in text.splitlines():
        line = raw.rstrip('\r').rstrip()
        if not line.strip():
            continue
        m = _DATE_RE.match(line)
        is_pend = bool(_PEND_RE.match(line.strip()))
        if m or is_pend:
            if cur is not None:
                meta['errors'].append(f'unterminated row before {line[:20]}')
            meta['raw_rows'] += 1
            if is_pend:
                dd, mm, yyyy = end_date.split('-')[::-1]
                pending = True
            else:
                dd, mm, yyyy = m.group(1), m.group(2), m.group(3)
                pending = False
            meta['pending'] += int(pending)
            cur = {'date': _date_iso(dd, mm, yyyy), 'pending': pending}
            desc_lines = []
            parts = line.split(maxsplit=1)
            rest = parts[1].strip() if len(parts) > 1 else ''
            if rest:
                desc_lines.append(rest)
            continue
        if cur is None:
            continue
        bm = _branch_match(line)
        if bm:
            fragment, branch, amt, dr, bal = bm
            amount = _money(amt)
            direction = 'in' if dr == 'CR' else 'out'
            balance = _money(bal)
            if fragment:
                desc_lines.append(fragment)
            cur['amount'] = amount
            cur['direction'] = direction
            cur['branch'] = branch
            cur['balance'] = balance
            cur['desc'] = [d for d in desc_lines if d.strip()]
            desc_lines = []
            try:
                rows.append(_make_row(cur))
            except Exception as e:  # pragma: no cover
                meta['errors'].append(f'{cur["date"]}: {e}')
            meta['parsed'] += 1
            cur = None
        elif line.strip():
            desc_lines.append(line.strip())
    if cur is not None:
        meta['errors'].append('final row unterminated (missing branch/amount)')
    return rows, meta


def _extract_name(body: str) -> tuple[str, str]:
    """Return (name, notes) from a stripped FTSCY/BI-FAST body.

    Names are trailing uppercase word-runs (BCA truncates to ~16 chars).
    Mixed-case / dashed prefixes (DP New Software, PR-260905006, bu yaya) are
    treated as notes. A literal trailing IN/OUT transaction code is dropped.
    """
    body = re.sub(_AMOUNT_GLUE_RE, ' ', body)
    body = re.sub(r'\bTRSF\s+E-BANKING\s+(DB|CR)\b', ' ', body, flags=re.I)
    body = re.sub(r'\bBI-FAST\s+(DB|CR)\b', ' ', body, flags=re.I)
    body = re.sub(r'\bTRANSFER\b', ' ', body, flags=re.I)
    body = re.sub(r'\bDR\s+\d{3}\b', ' ', body, flags=re.I)
    body = re.sub(r'\bKE\s+\d{3,}\b', ' ', body, flags=re.I)
    body = re.sub(r'\b(009|008|014)\b', ' ', body)
    body = re.sub(r'\s+', ' ', body).strip()
    if not body:
        return '', ''
    tokens = body.split(' ')
    i = len(tokens)
    while i > 0 and tokens[i - 1].isupper() and tokens[i - 1].isalpha():
        i -= 1
    name = tokens[i:]
    if len(name) > 1 and name[-1] in ('IN', 'OUT'):
        name = name[:-1]
    rest = tokens[:i]
    return ' '.join(name).strip(), ' '.join(rest).strip()


def _classify_desc(desc: str, direction: str, amount: int, *,
                   pending: bool = False, balance: int = 0,
                   date_iso: str = '') -> dict:
    """Classify one statement description string into an extraction row."""
    text = re.sub(r'\s+', ' ', (desc or '')).strip()
    joined = text.upper()

    rec = {
        'provider': 'bca',
        'src_txn_id': '',
        'description': '',
        'raw_description': '',
        'transaction_type': '',
        'merchant': '',
        'recipient': '',
        'phone_suffix': '',
        'direction': direction,
        'principal_amount': amount,
        'fee_amount': 0,
        'total_amount': amount,
        'currency': 'IDR',
        'occurred_at': f'{date_iso}T00:00:00',
        'bank_ref': '',
        'source_page': 0,
        'raw1': desc or '',
        'raw2': f'balance={balance}',
        'parser_version': 'bca-stmt-1',
    }
    if pending:
        rec['pending'] = True

    if 'BIAYA' in joined:  # BIAYA ADM / BIAYA TXN
        rec['transaction_type'] = 'fee'
        rec['principal_amount'] = 0
        rec['fee_amount'] = amount
        rec['description'] = 'Bank Fee'
        return rec

    if joined.startswith('TRSF E-BANKING') or 'BI-FAST' in joined:
        rec['transaction_type'] = 'transfer'
        refm = _REF_RE.search(text)
        rec['bank_ref'] = refm.group(1) if refm else ''
        body = text
        if refm:
            body = body.replace(refm.group(1), ' ', 1)

        if refm and 'FTFVA' in refm.group(1).upper():
            biller = _va_biller(text, refm.group(1))
            if biller:
                rec['transaction_type'] = 'va_payment'
                rec['recipient'] = biller
                rec['description'] = f'VA - {biller}'
                return rec

        if 'DOMPET ANAK BANGSA' in joined or 'GOPAY BANK TRANSFE' in joined:
            # Withdrawal from our own GoPay wallet into the bank.
            rec['recipient'] = 'GoPay Wallet'
            rec['description'] = 'Transfer - GoPay Wallet'
            return rec

        if any(n in (text or '').lower() for n in _OWN_NAMES):
            rec['recipient'] = 'Said Iskandar'
            rec['description'] = 'Transfer - Said Iskandar'
            return rec

        m = re.search(r'\b(?:KE\s+008|TRANSFER\s+KE\s+008)\s+([A-Z][A-Z .\-]{2,40})', text, re.I)
        if m:
            recipient = re.sub(r'\s+M-BCA\s*$', '', m.group(1)).strip()
            rec['recipient'] = recipient
            rec['description'] = f'Transfer - {recipient}' if recipient else 'Transfer'
            return rec

        name, notes = _extract_name(body)
        if name:
            rec['recipient'] = name
            rec['description'] = f'Transfer - {name}'
            if notes:
                rec['raw2'] = (rec['raw2'] + ' | notes=' + notes)
            return rec

        rec['description'] = 'Transfer'
        return rec

    if 'SETORAN VIA CDM' in joined:
        rec['transaction_type'] = 'deposit'
        rec['description'] = 'Cash Deposit'
        return rec

    if 'TRANSAKSI DEBIT' in joined:
        rec['transaction_type'] = 'qris'
        m = _QR_MERCHANT_RE.search(text)
        merchant = (m.group(1) if m else '').strip()
        rec['merchant'] = merchant[:80]
        rec['description'] = f'QRIS - {merchant}' if merchant else 'QRIS'
        return rec

    rec['transaction_type'] = 'statement_unknown'
    rec['description'] = text[:120]
    return rec


def _make_row(cur: dict) -> dict:
    desc = ' '.join(x.strip() for x in cur['desc'])
    return _classify_desc(desc, cur['direction'], cur['amount'],
                          pending=cur.get('pending', False),
                          balance=cur.get('balance', 0),
                          date_iso=cur['date'])


def _looks_like_date_or_pend(field: str) -> bool:
    f = field.lstrip("'").strip()
    return bool(_DATE_RE.match(f)) or bool(_PEND_RE.match(f.upper()))


def parse_csv_statement(text: str, *, end_date: str = '2026-09-13') -> tuple[list, dict]:
    """Parse a myBCA CSV export (SAIDISKA*.CSV) into extraction row dicts."""
    import csv as _csv

    rows: list[dict] = []
    meta = {'raw_rows': 0, 'pending': 0, 'parsed': 0, 'errors': []}
    reader = _csv.reader(io.StringIO(text))
    for line in reader:
        if not line:
            continue
        f = (line[0] or '').lstrip("'").strip()
        if not (_looks_like_date_or_pend(f) and len(line) >= 6):
            continue
        meta['raw_rows'] += 1
        is_pend = bool(_PEND_RE.match(f.upper()))
        if is_pend:
            dd, mm, yyyy = end_date.split('-')[::-1]
        else:
            m = _DATE_RE.match(f)
            if not m:
                meta['errors'].append(f'bad date field: {f}')
                continue
            dd, mm, yyyy = m.group(1), m.group(2), m.group(3)
        pending = is_pend
        meta['pending'] += int(pending)
        date_iso = _date_iso(dd, mm, yyyy)
        desc = line[1] or ''
        direction = 'in' if (line[4] or '').upper() in ('CR', 'C') else 'out'
        amount = _money(line[3] or '0')
        balance = _money(line[5] or '0')
        rows.append(_classify_desc(desc, direction, amount,
                                   pending=pending, balance=balance,
                                   date_iso=date_iso))
        meta['parsed'] += 1
    return rows, meta


def parse_statement(text: str, *, end_date: str = '2026-09-13') -> tuple[list, dict]:
    """Parse a myBCA statement - either the CSV export (auto-detected) or a
    raw pasted table. Returns (rows, meta)."""
    probe = [l for l in text.splitlines() if l.strip()][:40]
    csvish = any(re.match(r"^\s*'?(?:\d{2}/\d{2}/\d{4}|PEND)\s*,",
                          l, re.I) for l in probe)
    if csvish and probe:
        return parse_csv_statement(text, end_date=end_date)
    return parse_pasted_statement(text, end_date=end_date)


# ── dedup / matching ──────────────────────────────────────────────────

_BASE_CAND_SQL = """
SELECT l.id AS ledger_id, e.id AS ext_id, e.provider, e.description,
       e.merchant, e.recipient, e.bank_ref, e.transaction_type,
       e.direction, e.total_amount, e.principal_amount, e.fee_amount,
       substr(e.occurred_at, 1, 10) AS day, l.nature, l.review_status,
       l.txn_status, l.evidence_json
FROM ledger_txns l JOIN extracted_txns e ON e.id = l.ext_id
WHERE date(e.occurred_at) = date(?)
"""


def _candidates_same_bank(conn: sqlite3.Connection, cand: dict) -> list[dict]:
    return [dict(r) for r in conn.execute(
        _BASE_CAND_SQL + " AND e.provider = 'bca' AND e.direction = ? "
        "AND e.total_amount = ?",
        (cand['occurred_at'], cand['direction'], cand['total_amount']))]


def _candidates_cross_account(conn: sqlite3.Connection, cand: dict) -> list[dict]:
    """Same-owner interbank transfers. Only fires when the statement
    counterparty is one of our own names, matching a bank row whose direction
    is opposite and whose amount equals our principal. Never amount-only."""
    if not any(n in ' '.join([cand.get('recipient', '') or '',
                              cand.get('description', '') or '']).lower()
               for n in _OWN_NAMES):
        return []
    want = cand['total_amount']
    sign = 'in' if cand['direction'] == 'out' else 'out'
    rows = [dict(r) for r in conn.execute(
        _BASE_CAND_SQL + " AND e.provider IN ('bca','bni','bri','mandiri') "
        "AND date(e.occurred_at) BETWEEN date(?, '-1 day') AND date(?, '+1 day') "
        "AND e.direction = ?",
        (cand['occurred_at'], cand['occurred_at'],
         cand['occurred_at'], sign))]
    return [r for r in rows
            if any(n in (r['description'] or ' ').lower()
                   or n in (r['recipient'] or ' ').lower()
                   or n in (r['merchant'] or ' ').lower()
                   for n in _OWN_NAMES)
            and abs((r['principal_amount'] or r['total_amount'] or 0) - want) <= 1]


def _is_pending(ledger: dict) -> bool:
    try:
        return bool((json.loads(ledger.get('evidence_json') or '{}')).get('pending'))
    except Exception:
        return False


def _ref_ok(existing: dict, cand: dict) -> bool:
    a, b = (existing.get('bank_ref') or ''), (cand.get('bank_ref') or '')
    if not a or not b:
        return False
    ka, kb = _REF_RE.search(a), _REF_RE.search(b)
    return bool(ka and kb and ka.group(1) == kb.group(1))


def _cp_overlap(existing: dict, cand: dict) -> int:
    e = _name_tokens(existing.get('recipient')) | _name_tokens(existing.get('merchant'))
    c = _name_tokens(cand.get('recipient')) | _name_tokens(cand.get('merchant'))
    if not c:
        return 0
    return len(e & c)


def match_existing(conn: sqlite3.Connection, cand: dict) -> dict:
    """Return {status, ledger_id, ext_id, reason, candidate}.

    status: 'new' | 'confident' | 'confident_pending' | 'uncertain'.
    """
    cbs = _candidates_same_bank(conn, cand)
    for p in [c for c in cbs if _is_pending(c)]:
        return {'status': 'confident_pending', 'ledger_id': p['ledger_id'],
                'ext_id': p['ext_id'], 'reason': 'pending statement row',
                'candidate': p}
    # Admin/fee rows carry no counterparty: same day + amount + provider + type
    # is enough (both sides are identifiable fee rows).
    if (cand.get('transaction_type') or '').lower() == 'fee':
        mesh = [c for c in cbs
                if (c.get('transaction_type') or '').lower() == 'fee']
        if len(mesh) == 1:
            return {'status': 'confident', 'ledger_id': mesh[0]['ledger_id'],
                    'ext_id': mesh[0]['ext_id'], 'reason': 'fee admin row',
                    'candidate': mesh[0], 'cross_account': False}
    for c in cbs:
        if _ref_ok(c, cand):
            return {'status': 'confident', 'ledger_id': c['ledger_id'],
                    'ext_id': c['ext_id'], 'reason': 'bank reference match',
                    'candidate': c, 'cross_account': False}
    with_cp = [c for c in cbs if _cp_overlap(c, cand) > 0]
    if len(with_cp) == 1:
        return {'status': 'confident', 'ledger_id': with_cp[0]['ledger_id'],
                'ext_id': with_cp[0]['ext_id'],
                'reason': 'counterparty token match',
                'candidate': with_cp[0], 'cross_account': False}
    if with_cp and len(with_cp) > 1:
        return {'status': 'uncertain', 'ledger_id': None, 'ext_id': None,
                'reason': f'{len(with_cp)} ambiguous candidates',
                'candidate': None}
    if len(cbs) == 1:
        return {'status': 'uncertain', 'ledger_id': None, 'ext_id': None,
                'reason': 'amount+date+direction only (no ref/counterparty)',
                'candidate': None}
    xb = _candidates_cross_account(conn, cand)
    if len(xb) == 1:
        return {'status': 'confident', 'ledger_id': xb[0]['ledger_id'],
                'ext_id': xb[0]['ext_id'],
                'reason': 'same-owner interbank transfer',
                'candidate': xb[0], 'cross_account': True}
    if len(xb) > 1:
        return {'status': 'uncertain', 'ledger_id': None, 'ext_id': None,
                'reason': 'multiple same-owner interbank candidates',
                'candidate': None}
    return {'status': 'new', 'ledger_id': None, 'ext_id': None,
            'reason': 'no existing row', 'candidate': None}


# ── apply helpers (merge / finalize / insert) ────────────────────────────

def _push_provenance(conn: sqlite3.Connection, ledger_id: int,
                     source_key: str, info: dict) -> None:
    r = conn.execute("SELECT evidence_json FROM ledger_txns WHERE id=?",
                     (ledger_id,)).fetchone()
    try:
        ev = json.loads(r[0] or '{}') if r else {}
    except Exception:
        ev = {}
    ev[f'bca_statement_{source_key}'] = info
    store.update_ledger(conn, ledger_id, evidence_json=json.dumps(ev))


def merge_provenance(conn: sqlite3.Connection, ledger_id: int,
                     cand: dict, match: dict,
                     source_key: str = 'matched',
                     finalize_pending: bool = False) -> dict:
    """Merge missing identity fields + provenance into an existing ledger row.
    Never overwrites description or manually corrected categories."""
    existing = conn.execute(
        "SELECT e.*, l.evidence_json FROM ledger_txns l "
        "JOIN extracted_txns e ON e.id = l.ext_id WHERE l.id = ?",
        (ledger_id,)).fetchone()
    if existing is None:
        return {'ok': False, 'error': 'row not found'}
    ex = dict(existing)

    merged = False
    for field in ('recipient', 'merchant', 'bank_ref'):
        if not (ex.get(field) or '') and (cand.get(field) or ''):
            store.update_extracted(conn, ex['id'], **{field: cand[field]})
            ex[field] = cand[field]
            merged = True

    try:
        ev = json.loads(ex.get('evidence_json') or '{}')
    except Exception:
        ev = {}
    if finalize_pending:
        ev.pop('pending', None)
        ev[f'bca_statement_{source_key}'] = {'finalized': True,
                                             'ref': cand.get('bank_ref', ''),
                                             'at': datetime.now().isoformat(timespec='seconds')}
        store.update_ledger(conn, ledger_id, evidence_json=json.dumps(ev),
                            review_status='ok')
    else:
        ev[f'bca_statement_{source_key}'] = {'matched': True,
                                             'reason': match.get('reason', ''),
                                             'at': datetime.now().isoformat(timespec='seconds')}
        store.update_ledger(conn, ledger_id, evidence_json=json.dumps(ev))

    if merged and not store.has_manual_correction(conn, ledger_id):
        from categorize import categorize_batch, apply_categorization
        row = {'id': ex['id'], 'provider': ex.get('provider', 'bca'),
               'direction': ex['direction'],
               'total_amount': ex['total_amount'],
               'principal_amount': ex['principal_amount'],
               'transaction_type': ex.get('transaction_type') or 'transfer',
               'description': ex.get('description', ''),
               'recipient': ex.get('recipient', ''),
               'merchant': ex.get('merchant', ''),
               'raw_description': ex.get('raw_description', '')}
        res = categorize_batch(conn, [row])
        if res and res[0].get('category_id'):
            apply_categorization(conn, res[0], ledger_id)

    store.add_audit(conn, action='bca_backfill_merge', entity='ledger_txns',
                    entity_id=ledger_id, before_json='{}',
                    after_json=json.dumps({'source_key': source_key,
                                           'finalized': finalize_pending,
                                           'reason': match.get('reason', '')}))
    return {'ok': True, 'ledger_id': ledger_id}


def insert_new(conn: sqlite3.Connection, batch_id: int, doc_id: int,
               rec: dict, idx: int = 0) -> int:
    """Insert one new statement row (extracted + ledger) with categorization."""
    rec = dict(rec, ext_index=idx)
    bank_ref = rec.get('bank_ref') or ''
    rec['src_txn_id'] = f'{bank_ref}|{idx:02d}' if bank_ref else f'row{idx:02d}'
    ext_id = store.add_extracted_rows(conn, batch_id, doc_id, [rec])[0]
    p = dict(rec, id=ext_id)
    evidence = {'bca_statement': True}
    if rec.get('pending'):
        evidence['pending'] = True
    review = 'review' if (rec.get('pending') or rec.get('uncertain')) else 'uncategorized'
    ledger_id = store.add_ledger_row(
        conn, ext_id,
        amount=rec.get('total_amount', 0),
        direction=rec.get('direction', 'out'),
        nature='needs_review',
        confidence='medium',
        confidence_reason='bca_statement',
        evidence_json=json.dumps(evidence),
        review_status=review,
        txn_status='needs_review',
    )
    from categorize import categorize_batch, apply_categorization
    res = categorize_batch(conn, [p])
    if res and res[0].get('category_id'):
        apply_categorization(conn, res[0], ledger_id)
    return ledger_id


# ── top level: preview + apply ────────────────────────────────────────────

def preview(conn: sqlite3.Connection, text: str,
            end_date: str = '2026-09-13') -> dict:
    """Non-destructive report of what the backfill would do."""
    rows, meta = parse_statement(text, end_date=end_date)
    source_key = _build_source_key(end_date)
    out = {'source_key': source_key,
           'already_imported': bool(store.source_doc_exists(conn, source_key=source_key)),
           'meta': meta,
           'total_rows': len(rows),
           'duplicate_matches': [],
           'pending_rows': [],
           'proposed_inserts': [],
           'uncertain': []}
    for rec in rows:
        info = {'date': rec['occurred_at'][:10], 'dir': rec['direction'],
                'amount': rec['total_amount'], 'desc': rec['description']}
        m = match_existing(conn, rec)
        if m['status'] == 'confident':
            out['duplicate_matches'].append({**info, 'matches_ledger': m['ledger_id'],
                                             'reason': m['reason'],
                                             'cross_account': m.get('cross_account', False)})
        elif m['status'] == 'confident_pending':
            out['pending_rows'].append({**info, 'matches_ledger': m['ledger_id']})
        elif m['status'] == 'uncertain':
            out['uncertain'].append({**info, 'reason': m['reason']})
        else:
            out['proposed_inserts'].append({**info, 'pending': bool(rec.get('pending'))})
    out['summary'] = {
        'import_count': len(rows),
        'confident_merges': len(out['duplicate_matches']),
        'pending_finalizable': len(out['pending_rows']),
        'uncertain_review': len(out['uncertain']),
        'proposed_inserts': len(out['proposed_inserts']),
    }
    return out


def run_backfill(conn: sqlite3.Connection, text: str,
                 end_date: str = '2026-09-13') -> dict:
    """Apply the backfill. Idempotent: an existing source doc skips the batch."""
    rows, meta = parse_statement(text, end_date=end_date)
    source_key = _build_source_key(end_date)
    fp = statement_fingerprint(text)
    if (store.source_doc_exists(conn, source_key=source_key)
            or store.source_doc_exists(conn, fingerprint=fp)):
        return {'ok': False, 'error': 'already imported', 'source_key': source_key}

    doc_id = store.add_source_document(
        conn, kind='manual', source_key=source_key,
        fingerprint=fp,
        provider='bca', parser_version='bca-stmt-1', preview=text[:400])
    store.update_source_document(conn, doc_id, status='parsed')
    batch_id = store.add_import_batch(conn, doc_id)

    stats = {'import_count': len(rows), 'merged': 0, 'inserted': 0,
             'uncertain': 0, 'pending_finalized': 0}
    for i, rec in enumerate(rows):
        m = match_existing(conn, rec)
        if m['status'] == 'confident':
            merge_provenance(conn, m['ledger_id'], rec, m, source_key)
            stats['merged'] += 1
        elif m['status'] == 'confident_pending':
            merge_provenance(conn, m['ledger_id'], rec, m, source_key,
                             finalize_pending=True)
            stats['pending_finalized'] += 1
        else:
            rec['uncertain'] = m['status'] == 'uncertain'
            insert_new(conn, batch_id, doc_id, rec, i)
            if m['status'] == 'uncertain':
                stats['uncertain'] += 1
            elif rec.get('pending'):
                stats['pending_finalized'] += 1
            else:
                stats['inserted'] += 1

    store.update_import_batch(conn, batch_id, state='committed',
                              stats_json=json.dumps(stats))
    store.add_audit(conn, action='bca_backfill', entity='source_documents',
                    entity_id=doc_id, before_json='{}',
                    after_json=json.dumps(stats))
    return {'ok': True, 'doc_id': doc_id, 'batch_id': batch_id, 'stats': stats}


def finalize_gmail_match(conn: sqlite3.Connection,
                         parsed_row: dict) -> Optional[dict]:
    """Gmail-pipeline hook: if a freshly parsed email row already exists (from
    the statement backfill), merge provenance and mark pending statement rows
    finalized instead of inserting a duplicate. Returns the match dict or None.
    """
    if not parsed_row:
        return None
    m = match_existing(conn, parsed_row)
    if m['status'] not in ('confident', 'confident_pending'):
        pend = _pending_candidate(conn, parsed_row)
        if pend is not None:
            m = {'status': 'confident_pending', 'ledger_id': pend['ledger_id'],
                 'ext_id': pend['ext_id'],
                 'reason': 'pending finalize from email',
                 'candidate': pend}
        else:
            return None
    merge_provenance(conn, m['ledger_id'], parsed_row, m, 'gmail_followup',
                     finalize_pending=m['status'] == 'confident_pending')
    return m


def _pending_candidate(conn: sqlite3.Connection, cand: dict) -> Optional[dict]:
    for r in conn.execute(
            _BASE_CAND_SQL + " AND e.provider = 'bca' AND e.direction = ? "
            "AND e.total_amount = ?",
            (cand['occurred_at'], cand['direction'], cand['total_amount'])):
        d = dict(r)
        if _is_pending(d):
            return d
    return None