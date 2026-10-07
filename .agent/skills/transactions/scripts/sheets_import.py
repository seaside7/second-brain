"""One-time recap import into the existing Transactions database.

The owner maintains a Google Sheet recap of confirmed transactions (categories,
splits, trips, notes). This module performs a ONE-TIME reconcile-and-load of a
recap export into the ledger:

- Matches each recap bank debit against ledger rows already ingested from
  email/statements first; matched rows are only enriched (never duplicated).
- Inserts only recap rows that exist nowhere in the ledger; each insert goes
  through the normal source_documents / extracted_txns / ledger_txns chain so
  every bank row is preserved exactly once.
- Split allocations are imported as linked txn_splits child records on the
  parent bank debit; summary/chart/adjustment rows are excluded, never debits.
- Re-running the same recap file is a no-op (sheet_import_log keyed by a stable
  row hash).
- Imported confirmations are written as corrections rows (actor 'user', reason
  'recap_import'), so reprocess treats them like manual fixes and leaves them
  alone.

Flow mirrors import_engine: preview_recap() first (no writes), then
confirm_recap() applies everything in one transaction.

Input is a CSV export of the recap. Column names are matched case-insensitively
against RECAP_COLUMN_ALIASES; adjust those aliases to match the real sheet.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Optional

import store
from schema import connect

# Column-name aliases (lower-cased) -> canonical recap field, applied in order.
# Matches the real export from the owner's "Rekap Keuangan 25 September -
# 25 Oktober 2026" Google Sheet (Transaksi tab).
RECAP_COLUMN_ALIASES = {
    'date': {'date', 'tanggal', 'tgl', 'tanggal transaksi', 'tanggal posting',
             'transaction date', 'waktu', 'posted date'},
    'description': {'description', 'keterangan', 'deskripsi', 'deskripsi sumber',
                    'detail', 'transaksi', 'merchant', 'payee'},
    'debit': {'debit', 'debit / uang keluar (rp)', 'pengeluaran', 'uang keluar',
              'expense', 'nominal', 'jumlah', 'nilai', 'amount', 'amount_rp',
              'beban'},
    'credit': {'credit', 'kredit', 'kredit / uang masuk (rp)', 'uang masuk',
               'income', 'pemasukan'},
    'category': {'category', 'kategori', 'kategori (bisa diganti)', 'bucket',
                 'topik', 'group'},
    'confirmed': {'confirmed', 'user confirmed', 'disahkan', 'final'},
    'type': {'type', 'jenis', 'tipe', 'row type'},
    'split_of': {'split_of', 'parent row', 'bagian dari', 'parent'},
    'trip': {'trip', 'destinasi', 'trip context', 'lokasi', 'lokasi / konteks',
             'konteks'},
    'notes': {'notes', 'catatan', 'memo'},
    'reference': {'reference', 'ref', 'bank ref', 'no referensi', 'trx id'},
    'source': {'source', 'sumber'},
}

# Recap sheet category names -> English canonical system category. The recap
# taxonomy is Indonesian; the ledger taxonomy is English since 2026-10-03, so
# a re-import must never recreate Indonesian-named categories.
RECAP_CATEGORY_ALIASES = {
    'Bulanan Dinda': 'Dinda Allowance',
    'Makan dan minum': 'Food & Dining',
    'Biaya bank': 'Bank Fee',
    'Transfer internal': 'Internal Transfer',
    'Transfer keluarga': 'Family Transfer',
    'Pembayaran utang': 'Loan Payment',
    'Pembayaran Pinjol': 'Loan Payment',
    'Tagihan rumah': 'Household Bills',
    'Top-up ShopeePay': 'ShopeePay Top-up',
    'Top-up OVO': 'OVO Top-up',
    'Top-up e-money': 'E-money Top-up',
    'Belanja': 'Online Shopping',
    'Gaji': 'Salary',
    'Dana perjalanan kerja': 'Work Travel Allowance',
    'Pinjaman masuk': 'Loan Received',
    'KPR': 'Mortgage',
    'Cicilan kendaraan': 'Home Installment',
    'Pengembalian pinjaman': 'Loan Repayment',
    'Tiket kereta': 'Train Ticket',
    'Transportasi': 'Transportation',
    'Asuransi': 'Insurance',
    'Apotek': 'Pharmacy',
    'RS Anak': "Children's Hospital",
    'Tukang': 'Handyman',
    'Arif Pinjam': 'Arif Loan',
    'Perlu konfirmasi': 'Pending Confirmation',
    'Digital dan langganan': 'Digital & Subscriptions',
}

def _cat_name_for_recap(raw: str) -> str:
    """Map a recap sheet category value to the English canonical name."""
    name = (raw or '').strip()
    return RECAP_CATEGORY_ALIASES.get(name, name)

SUMMARY_TYPES = {'total', 'subtotal', 'summary', 'total biaya', 'total pengeluaran',
                 'total amount'}
ADJUSTMENT_TYPES = {'adjustment', 'adjust', 'chart', 'pivot', 'chart adjustment',
                    'penyesuaian', 'reclass'}
ALLOCATION_TYPES = {'allocation', 'alloc', 'split', 'alokasi', 'split child'}

_SUMMARY_RE = re.compile(r'^(sub[- ])?total\b', re.IGNORECASE)
# Allocation rows inside the recap ledger (e.g. "Alokasi transfer Dinda
# Rp1.467.000 - ..."): they are slices of a parent bank transfer, never
# separate debits, so they must not be inserted or matched.
_ALLOC_RE = re.compile(r'\balokasi\b', re.IGNORECASE)
# Family signals: an allocation row belongs to a transfer family when the
# description/notes name the split explicitly. Word-boundary "alokasi" avoids
# false positives like "teralokasi".
_ALLOC_FAMILY_RE = re.compile(
    r'(?i)(\balokasi\b|send total to dinda|bukan debit bank tambahan)')
_FAMILY_KEY_RE = re.compile(
    r'(?i)(?:total(?: to dinda)?|alokasi transfer dinda)\s*(?:rp\s*)?([0-9][0-9.,]*)')
_FAMILY_DATE_RE = re.compile(r'(?i)transfer\s+(?:bca\s+)?tanggal\s+(\d{1,2})\s+([a-z]+)')
_MONTHS = {m: i for i, m in enumerate(
    ['januari', 'februari', 'maret', 'april', 'mei', 'juni', 'juli', 'agustus',
     'september', 'oktober', 'november', 'desember'], start=1)}
_MSERIAL_RE = re.compile(r'^\d{4,5}$')
_MDY_RE = re.compile(r'^(\d{1,2})/(\d{1,2})/(\d{4})$')
_ISO_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})$')


def _norm_date(text) -> str:
    """Recap sheets mix US M/D/YYYY dates with Excel serials (e.g. 46290);
    normalise both to ISO YYYY-MM-DD so matching/inserts are ledger-consistent."""
    s = (text or '').strip()
    if not s:
        return ''
    m = _MDY_RE.match(s)
    if m:
        mm, dd, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            from datetime import date
            return date(yy, mm, dd).isoformat()
        except ValueError:
            return ''
    if _MSERIAL_RE.match(s):
        try:
            from datetime import datetime, timedelta
            return (datetime(1899, 12, 30) + timedelta(days=int(s))).date().isoformat()
        except (ValueError, OverflowError):
            return ''
    if _ISO_RE.match(s):
        return s
    return s[:10]


def _conn(conn):
    return conn if conn is not None else connect()


# ── loading + normalisation ─────────────────────────────────────────────

def _parse_amount(text) -> int:
    """Coerce recap amounts to int, handling the sheet's US formatting
    ('Rp 103,000.00', 'Rp 52,340,000.00'), plain integers, and the European
    variant ('1.234,56') as fallback.

    Disambiguation: when both separators are present the LAST one is the
    decimal separator (US: 103,000.00 -> 103000; EU: 1.234,56 -> 1234,56).
    A lone dot that closes exactly two digits is a decimal; more trailing
    digits mean grouping (5.000 -> 5000).
    """
    if text in (None, ''):
        return 0
    s = str(text).upper().replace('RP', '').replace('IDR', '').strip()
    if not s:
        return 0
    if ',' in s and '.' in s:
        if s.rfind('.') > s.rfind(','):
            s = s.replace(',', '')            # US: 103,000.00 -> 103000.00
        else:
            s = s.replace('.', '').replace(',', '.')  # EU: 1.234,56 -> 1234.56
    elif ',' in s:
        s = s.replace(',', '')                # grouping: 1,234 -> 1234
    elif s.count('.') >= 2:
        s = s.replace('.', '')                # grouping: 1.234.567 -> 1234567
    elif s.count('.') == 1 and (len(s) - s.find('.') - 1) > 2:
        s = s.replace('.', '')                # grouping: 5.000 -> 5000
    try:
        return int(round(float(s)))
    except (ValueError, TypeError):
        return 0


def _header_map(cols: list[str]) -> dict:
    """Map canonical recap field -> actual column header (first alias match)."""
    lowered = [str(c).strip().lower() for c in cols]
    out = {}
    for canon, aliases in RECAP_COLUMN_ALIASES.items():
        for i, col in enumerate(lowered):
            if col in aliases:
                out[canon] = cols[i]
                break
    return out


def _norm_row(record: dict, hm: dict, physical_row: int) -> dict:
    def pick(canon: str) -> str:
        col = hm.get(canon)
        return (record.get(col) or '').strip() if col else ''

    typ = (pick('type') or '').lower()
    desc = pick('description')
    if not desc and _SUMMARY_RE.search(pick('category') or ''):
        desc = 'total row'
    debit, credit = pick('debit'), pick('credit')
    db, cr = _parse_amount(debit), _parse_amount(credit)
    amount = -cr if cr else db  # credit wins when the recap sets both cols
    confirmed = (pick('confirmed') or '1').lower() not in ('', '0', 'false',
                                                           'no', 'n', 'unconfirmed')
    split_tag = pick('split_of')
    return {
        'row_no': physical_row,
        'occurred_at': _norm_date(pick('date')),
        'amount': amount,
        'direction': 'in' if amount < 0 else 'out',
        'description': desc,
        'category': pick('category'),
        'confirmed': confirmed,
        'type': typ,
        'split_of': int(split_tag) if re.match(r'^\d+$', split_tag) else None,
        'trip': pick('trip'),
        'notes': pick('notes'),
        'reference': pick('reference'),
        'source': pick('source'),
    }


def load_recap(path: str | Path) -> dict:
    """Read a recap CSV export (or JSON list) into normalised row dicts."""
    p = Path(path)
    if not p.exists():
        return {'ok': False, 'error': f'Recap file not found: {p}'}
    if p.suffix.lower() == '.json':
        return _load_json(p)
    return _load_csv(p)


def _load_json(p: Path) -> dict:
    try:
        data = json.loads(p.read_text(encoding='utf-8-sig'))
    except Exception as e:
        return {'ok': False, 'error': f'Bad JSON: {e}'}
    if isinstance(data, dict):
        data = data.get('rows') or data.get('data') or []
    if not isinstance(data, list):
        return {'ok': False, 'error': 'JSON must be a list of row objects'}
    rows = []
    for i, r in enumerate(data, start=2):
        flat = {str(k): v for k, v in r.items()}
        hm = _header_map(list(flat.keys()))
        rows.append(_norm_row(flat, hm, i))
    return {'ok': True, 'rows': rows, 'source': 'json'}


def _load_csv(p: Path) -> dict:
    try:
        with open(p, encoding='utf-8-sig', newline='') as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames:
                return {'ok': False, 'error': 'CSV has no header row'}
            hm = _header_map([h for h in reader.fieldnames if h])
            rows = []
            for i, record in enumerate(reader, start=2):
                if not any((v or '').strip() for v in record.values()):
                    continue
                rows.append(_norm_row(record, hm, i))
    except Exception as e:
        return {'ok': False, 'error': f'Bad CSV: {e}'}
    return {'ok': True, 'rows': rows, 'source': 'csv'}


def _row_hash(row: dict) -> str:
    h = hashlib.sha256()
    for k in ('occurred_at', 'amount', 'direction', 'description',
              'category', 'confirmed', 'split_of', 'reference'):
        h.update(f'{k}={row.get(k, "")}\n'.encode('utf-8'))
    return h.hexdigest()[:32]


# ── classification ──────────────────────────────────────────────────────

def classify(rows: list[dict]) -> dict:
    """Separate recap rows into bank debits, allocation children, exclusions."""
    parents, allocations, excluded = [], [], []
    for r in rows:
        typ = (r.get('type') or '').lower()
        desc = r.get('description') or ''
        if typ in SUMMARY_TYPES or _SUMMARY_RE.search(desc):
            excluded.append({**r, 'reason': 'summary'})
        elif typ in ADJUSTMENT_TYPES:
            excluded.append({**r, 'reason': 'adjustment/chart'})
        elif typ in ALLOCATION_TYPES or r.get('split_of') or \
                _ALLOC_RE.search(desc + ' ' + (r.get('notes') or '')) or \
                _ALLOC_FAMILY_RE.search(desc + ' ' + (r.get('notes') or '')):
            allocations.append(r)
        elif not r.get('occurred_at'):
            excluded.append({**r, 'reason': 'no date'})
        elif abs(_int_amt(r)) <= 0:
            excluded.append({**r, 'reason': 'no movement'})
        else:
            parents.append(r)
    return {'parents': parents, 'allocations': allocations, 'excluded': excluded}


def _int_amt(r) -> int:
    try:
        return int(r.get('amount', 0) or 0)
    except (TypeError, ValueError):
        return 0


# ── matching against existing ledger rows ───────────────────────────────

def _match_existing(conn, row: dict) -> dict:
    """Return {status, ledger_id, candidates} for a recap bank debit."""
    amount, day = abs(int(row.get('amount', 0) or 0)), (row.get('occurred_at') or '')[:10]
    if amount <= 0:
        return {'status': 'different', 'ledger_id': None}
    hits = [dict(r) for r in conn.execute(
        "SELECT l.id, l.direction, e.description "
        "FROM ledger_txns l JOIN extracted_txns e ON e.id=l.ext_id "
        "WHERE l.amount=? AND substr(e.occurred_at,1,10)=?",
        (amount, day)).fetchall()]
    if not hits:
        return {'status': 'different', 'ledger_id': None}

    def weight(h):
        hd = (h.get('description') or '').lower()
        rd = (row.get('description') or '').lower()
        tok = set(re.findall(r'[a-z0-9]{3,}', hd)) & set(re.findall(r'[a-z0-9]{3,}', rd))
        score = sum(len(t) for t in tok)
        same_dir = int((h.get('direction') or 'out') == (row.get('direction') or 'out'))
        return (same_dir, score)

    best = max(hits, key=weight)
    top = weight(best)
    ties = [h['id'] for h in hits if weight(h) == top]
    if len(ties) > 1:
        return {'status': 'ambiguous', 'ledger_id': None, 'candidates': ties}
    if top[0] and top[1] >= 2:
        return {'status': 'exact', 'ledger_id': best['id']}
    if top[0]:
        return {'status': 'likely', 'ledger_id': best['id']}
    return {'status': 'ambiguous', 'ledger_id': None, 'candidates': ties}


def _build_families(rows: list[dict]) -> list[dict]:
    """Group allocation rows into one synthetic parent debit per transfer.

    The recap splits a single bank transfer (e.g. Rp2.500.000 to Dinda) across
    several rows (R004 + R127, or R092 + R128..R134). Each slice keeps a bank
    debit amount that sums to the real transfer, so importing slices as debits
    would either double-count (when the parent also appears) or break the
    1:1 statement mapping. Reconstruct one parent row whose amount is the group
    total, and carry the slices as data for txn_splits on confirm.

    Returns list of {key, amount, date, label, notes, slices}.
    """
    groups = {}
    for a in rows:
        hay = ' '.join([a.get('description') or '', a.get('notes') or ''])
        m = _FAMILY_KEY_RE.search(hay)
        if not m:
            continue
        key = m.group(1).replace('.', '').replace(',', '')
        if not key.isdigit() or int(key) <= 0:
            continue
        groups.setdefault(key, []).append(a)
    out = []
    for key, slices in groups.items():
        total = sum(abs(_int_amt(s)) for s in slices)
        if total <= 0:
            continue
        label = slices[0].get('description') or 'Transfer (alokasi rekapan)'
        notes = next((s.get('notes') for s in slices if s.get('notes')), '')
        date = _family_date(notes) or min(
            (s.get('occurred_at') or '') for s in slices if s.get('occurred_at'))
        out.append({
            'key': key,
            'amount': total,
            'date': date,
            'label': label,
            'notes': notes,
        })
    return out


def _family_date(notes: str) -> str:
    m = _FAMILY_DATE_RE.search(notes or '')
    if not m:
        return ''
    day, month = int(m.group(1)), _MONTHS.get(m.group(2).lower())
    if not month:
        return ''
    try:
        from datetime import date
        return date(2026, month, day).isoformat()
    except ValueError:
        return ''


# ── preview ─────────────────────────────────────────────────────────────

def preview_recap(conn=None, *, path: str | Path = '',
                  rows: list[dict] | None = None) -> dict:
    """Dry-run: classify + match against the ledger; returns buckets + totals.

    Never writes. Matched rows (exact/likely) are enrichment candidates, new
    rows are missing, ambiguous rows are held for review, excluded rows are
    summary/adjustment/allocation noise that never becomes debits.
    """
    conn = _conn(conn)
    if path:
        loaded = load_recap(path)
        if not loaded.get('ok'):
            return loaded
        rows = loaded['rows']
    if not rows:
        return {'ok': False, 'error': 'No recap rows to preview'}

    seen = {r[0] for r in conn.execute("SELECT row_hash FROM sheet_import_log").fetchall()}
    bucket = classify(rows)

    matched, new_rows, ambiguous = [], [], []
    for row in bucket['parents']:
        h = _row_hash(row)
        if h in seen:
            continue
        m = _match_existing(conn, row)
        rec = {**row, 'hash': h}
        if m['status'] in ('exact', 'likely'):
            matched.append({**rec, 'status': m['status'], 'ledger_id': m['ledger_id']})
        elif m['status'] == 'ambiguous':
            ambiguous.append({**rec, 'candidates': m.get('candidates') or []})
        else:
            new_rows.append(rec)

    # Allocation rows may reference parents already in the ledger (enrich the
    # parent) or form transfer families that become one aggregated parent.
    alloc_map = {}
    for a in bucket['allocations']:
        alloc_map.setdefault(str(a.get('split_of')), []).append(a)

    for fam in _build_families(bucket['allocations']):
        slices = [a for a in bucket['allocations']
                  if _FAMILY_KEY_RE.search(' '.join(
                      [a.get('description') or '', a.get('notes') or ''])) and
                  _FAMILY_KEY_RE.search(' '.join(
                      [a.get('description') or '', a.get('notes') or '']))
                  .group(1).replace('.', '').replace(',', '') == fam['key']]
        row = {
            'row_no': 0,
            'occurred_at': fam['date'],
            'amount': fam['amount'],
            'direction': 'out',
            'description': fam['label'],
            'category': '',
            'confirmed': True,
            'type': '',
            'split_of': None,
            'trip': '',
            'notes': fam['notes'],
            'reference': '',
            'source': 'recap_family',
            'is_family': True,
            'family_slices': slices,
        }
        row['hash'] = _row_hash(row)
        new_rows.append(row)

    def amt(r):
        try:
            return abs(int(r.get('amount', 0) or 0))
        except (TypeError, ValueError):
            return 0

    family_total = sum(fam['amount'] for fam in _build_families(bucket['allocations']))

    return {
        'ok': True,
        'source': 'recap',
        'matched': matched,
        'new_rows': new_rows,
        'ambiguous': ambiguous,
        'excluded': bucket['excluded'],
        'allocations': bucket['allocations'],
        'families': _build_families(bucket['allocations']),
        'alloc_map': {str(k): [dict(v) for v in vals] for k, vals in alloc_map.items()},
        'totals': {
            'matched': sum(amt(r) for r in matched),
            'new': sum(amt(r) for r in new_rows),
            'ambiguous': sum(amt(r) for r in ambiguous),
            'excluded': sum(amt(r) for r in bucket['excluded']),
            'sheet_total': sum(amt(r) for r in bucket['parents']) +
                           sum(amt(r) for r in bucket['excluded']) +
                           family_total,
        },
    }


# ── apply ───────────────────────────────────────────────────────────────

def confirm_recap(conn=None, *, path: str | Path = '',
                  rows: list[dict] | None = None,
                  apply_hashes: list[str] | None = None,
                  resolutions: dict | None = None) -> dict:
    """Apply a previewed recap in ONE transaction (rollback on any error).

    apply_hashes: hashes of new-row entries to insert (ambiguous never
    auto-insert). resolutions: {row_hash: {'apply_category': bool,
    'apply_trip': bool}} for matched rows. A matched row whose ledger row has a
    manual category correction is a conflict and is skipped unless explicitly
    resolved.
    """
    conn = _conn(conn)
    prev = preview_recap(conn, path=path) if path else \
        (preview_recap(conn, rows=rows) if rows else None)
    if not prev or not prev.get('ok'):
        return prev or {'ok': False, 'error': 'Nothing to confirm'}

    resolutions = resolutions or {}
    apply_new = set(apply_hashes or [])
    stats = {'ok': True, 'enriched': 0, 'inserted': 0, 'held_review': 0,
             'conflicts': 0, 'skipped_dup': 0}

    conn.execute('BEGIN')
    try:
        for row in prev['matched']:
            h = row['hash']
            res = resolutions.get(h, {})
            applied = _enrich_matched(conn, row, res)
            if applied.get('conflict'):
                stats['conflicts'] += 1
                _log(conn, h, row['row_no'], 'enrich_conflict', row['ledger_id'], 0)
            else:
                stats['enriched'] += 1
                _log(conn, h, row['row_no'], 'enrich', row['ledger_id'], 1)
            allocs = prev['alloc_map'].get(str(row['row_no']), [])
            if allocs and row['ledger_id']:
                _apply_splits(conn, row['ledger_id'], allocs)

        for row in prev['new_rows']:
            h = row['hash']
            if h not in apply_new:
                _log(conn, h, row['row_no'], 'held_review', None, 0)
                stats['held_review'] += 1
                continue
            if h in {r[0] for r in conn.execute(
                    "SELECT row_hash FROM sheet_import_log").fetchall()}:
                stats['skipped_dup'] += 1
                continue
            lid = _insert_missing(conn, row)
            _log(conn, h, row['row_no'], 'insert', lid, 1)
            stats['inserted'] += 1
            allocs = prev['alloc_map'].get(str(row['row_no']), [])
            if allocs:
                _apply_splits(conn, lid, allocs)
            if row.get('family_slices'):
                _apply_splits(conn, lid, row['family_slices'])

        for row in prev['ambiguous']:
            _log(conn, row['hash'], row['row_no'], 'held_review', None, 0)
            stats['held_review'] += 1

        conn.commit()
    except Exception as e:
        conn.rollback()
        return {'ok': False, 'error': f'Confirm failed, rolled back: {e}'}
    return {**stats, 'excluded_count': len(prev['excluded'])}


def _enrich_matched(conn, row: dict, res: dict) -> dict:
    lid = row['ledger_id']
    ledger = store.get_ledger(conn, lid)
    out = {'applied': False, 'conflict': False}
    cat_name = (row.get('category') or '').strip()

    if cat_name and row.get('confirmed'):
        cat = store.get_or_create_category(conn, name=_cat_name_for_recap(cat_name))
        if cat != ledger.get('category_id'):
            if store.has_manual_correction(conn, lid, ('category_id',)):
                out['conflict'] = True
            elif res.get('apply_category', True):
                _raw_update(conn, lid, category_id=cat)
                _raw_correction(conn, lid, 'category_id',
                                str(ledger.get('category_id') or ''), str(cat))
                out['applied'] = True
    elif cat_name and not row.get('confirmed') and not ledger.get('category_id'):
        if res.get('apply_category', True):
            cat = store.get_or_create_category(conn, name=_cat_name_for_recap(cat_name))
            _raw_update(conn, lid, category_id=cat)
            _raw_correction(conn, lid, 'category_id', '', str(cat))
            out['applied'] = True

    note = (row.get('notes') or '').strip()
    if note and res.get('apply_notes', True):
        old = ledger.get('notes') or ''
        if note not in old:
            _raw_update(conn, lid, notes=(old + ' | ' + note).strip(' |'))
            out['applied'] = True

    trip_id = _match_trip(conn, row.get('trip') or '')
    if trip_id and trip_id != ledger.get('trip_id') and res.get('apply_trip', True):
        _raw_update(conn, lid, trip_id=trip_id)
        _raw_correction(conn, lid, 'trip_id', str(ledger.get('trip_id') or ''),
                        str(trip_id))
        out['applied'] = True

    if row.get('reference'):
        _raw_audit(conn, 'recap_evidence', 'ledger_txns', lid,
                   json.dumps({'source': 'recap', 'reference': row['reference']}))
    return out


def _match_trip(conn, hint: str) -> Optional[int]:
    hint = (hint or '').strip().lower()
    if not hint:
        return None
    for t in store.list_trips(conn):
        if hint in (t.get('name') or '').lower() or \
           hint in (t.get('destination') or '').lower():
            return t['id']
    return None


def _insert_missing(conn, row: dict) -> int:
    """Full source_documents -> extracted_txns -> ledger_txns insert (raw SQL,
    stays inside the caller's transaction)."""
    h = _row_hash(row)
    cur = conn.execute(
        "INSERT INTO source_documents(kind,source_key,fingerprint,provider,"
        "parser_version,preview) VALUES('sheet',?,?,?,?,?)",
        (f'recap:{h}', h, 'sheet', 'recap-v1',
         json.dumps({'row_no': row['row_no']})))
    doc = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO import_batches(source_document_id) VALUES(?)", (doc,))
    batch = cur.lastrowid
    amount = abs(int(row['amount']))
    cur = conn.execute(
        "INSERT INTO extracted_txns(doc_id,batch_id,ext_index,provider,"
        "src_txn_id,description,raw_description,transaction_type,merchant,"
        "recipient,phone_suffix,direction,principal_amount,fee_amount,"
        "total_amount,currency,occurred_at,bank_ref,source_page,raw1,raw2,"
        "parser_version,dup_status,duplicate_of) "
        "VALUES("
        "?, ?, 0, 'sheet', ?, ?, ?,"     " 'recap', '', '', '',"
        " ?, ?, 0, ?,"                    " 'IDR', ?, ?, 0,"
        " '', '', 'recap-v1', 'unique', NULL)",
        (doc, batch, f'sheet:{h[:16]}', row['description'],
         row['description'],
         row['direction'], amount, amount,
         row['occurred_at'], row.get('reference') or ''))
    ext = cur.lastrowid
    cat = None
    if row.get('category'):
        cat = store.get_or_create_category(conn, name=_cat_name_for_recap(row['category']))
    is_fam = bool(row.get('family_slices'))
    cur = conn.execute(
        "INSERT INTO ledger_txns(ext_id,account_id,amount,direction,nature,"
        "category_id,notes,confidence,confidence_reason,evidence_json,"
        "review_status,txn_status) VALUES(?,NULL,?,?,?,?,?,?,?,?,?,?)",
        (ext, amount, row['direction'],
         'expense' if (cat or is_fam) else 'needs_review', cat, row.get('notes') or '',
         'high' if row.get('confirmed') else 'medium', 'recap_import',
         json.dumps({'source': 'recap', 'sheet_row': row['row_no'],
                     'reference': row.get('reference') or '',
                     'family': f"recap_family:{row.get('is_family')}"}),
         'ok' if (cat or is_fam) else 'uncategorized', 'confirmed'))
    lid = cur.lastrowid
    if cat:
        _raw_correction(conn, lid, 'category_id', '', str(cat))
    trip_id = _match_trip(conn, row.get('trip') or '')
    if trip_id:
        _raw_update(conn, lid, trip_id=trip_id)
        _raw_correction(conn, lid, 'trip_id', '', str(trip_id))
    return lid


def _apply_splits(conn, parent_ledger_id: int, alloc_rows: list[dict]) -> None:
    """Create txn_splits children from allocation recap rows (linked, not
    separate debits). Sum mismatch is audited, never rebalanced."""
    allocations = []
    for a in alloc_rows:
        if not (a.get('category') or '').strip():
            continue
        cat = store.get_or_create_category(conn, name=_cat_name_for_recap(a['category']))
        allocations.append({'category_id': cat, 'amount': abs(int(a['amount']))})
    if not allocations:
        return
    conn.execute("DELETE FROM txn_splits WHERE parent_ledger_id=?",
                 (parent_ledger_id,))
    for n in allocations:
        conn.execute(
            "INSERT OR REPLACE INTO txn_splits"
            "(parent_ledger_id,category_id,trip_id,amount,notes) "
            "VALUES(?,?,?,?,?)",
            (parent_ledger_id, n['category_id'], None, n['amount'], 'recap_import'))
    _raw_audit(conn, 'recap_splits', 'ledger_txns', parent_ledger_id,
               json.dumps({'allocations': allocations,
                           'sum': sum(a['amount'] for a in allocations)}))


def _log(conn, row_hash: str, row_no: int, action: str,
         ledger_id: int | None = None, reviewed: int = 1) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO sheet_import_log(row_hash,sheet_row,action,ledger_id,reviewed) "
        "VALUES(?,?,?,?,?)",
        (row_hash, row_no, action, ledger_id, reviewed))


def _raw_update(conn, ledger_id: int, **fields) -> None:
    allowed = {'category_id', 'trip_id', 'notes', 'payment_method'}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    conn.execute(
        f"UPDATE ledger_txns SET {', '.join(f'{k}=?' for k in sets)}, "
        "updated_at=strftime('%Y-%m-%dT%H:%M:%S','now','localtime') WHERE id=?",
        (*sets.values(), ledger_id))


def _raw_correction(conn, ledger_id: int, field: str,
                    before_val: str, after_val: str, reason: str = 'recap_import') -> None:
    conn.execute(
        "INSERT INTO corrections(ledger_id,field,before_val,after_val,reason,actor) "
        "VALUES(?,?,?,?,?, 'user')",
        (ledger_id, field, before_val, after_val, reason))


def _raw_audit(conn, action: str, entity: str, entity_id: int | None,
               after_json: str) -> None:
    conn.execute(
        "INSERT INTO audit_log(action,entity,entity_id,before_json,after_json,"
        "actor,source) VALUES(?,?,?,'{}',?, 'user','recap')",
        (action, entity, entity_id, after_json))


# ── CLI affordance ──────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse

    ap = argparse.ArgumentParser(description='One-time recap import')
    ap.add_argument('file')
    ap.add_argument('--preview', action='store_true', help='preview only (no writes)')
    ap.add_argument('--apply-hash', action='append', default=[],
                    help='insert a specific new-row hash (repeatable)')
    args = ap.parse_args()

    if args.preview:
        print(json.dumps(preview_recap(path=args.file), ensure_ascii=False, default=str))
    else:
        prev = preview_recap(path=args.file)
        if not prev.get('ok'):
            print(json.dumps(prev, ensure_ascii=False))
        else:
            print(json.dumps(confirm_recap(path=args.file,
                                           apply_hashes=args.apply_hash),
                             ensure_ascii=False, default=str))