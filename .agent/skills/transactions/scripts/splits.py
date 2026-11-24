"""Splits: allocate one ledger row across categories (and optionally trips).

A parent ledger row keeps its full amount; a split set records how that amount
is apportioned by category (per allocation, optionally pinned to a trip). The
parent still counts once in balance and net. Reports attribute rows by their
splits when present, else by the parent's own category.

Rules honoured here:
- The allocation key is (category_id, trip_id): a parent can have at most one
  split per (category, trip) pair.
- Setting a split set is atomic: the old set is replaced in one transaction.
- A sum mismatch (allocations != parent amount) is flagged in the result, never
  silently rebalanced. The caller decides whether that is acceptable.
"""
from __future__ import annotations

import json
from typing import Optional

import store
from schema import connect


def _conn(conn):
    return conn if conn is not None else connect()


# ── read ────────────────────────────────────────────────────────────────

def list_splits(conn=None, *, ledger_id: int | None = None) -> dict:
    conn = _conn(conn)
    if ledger_id:
        rows = [dict(r) for r in conn.execute(
            "SELECT s.*, c.name AS category_name, "
            "       t.name AS trip_name "
            "FROM txn_splits s "
            "LEFT JOIN categories c ON c.id=s.category_id "
            "LEFT JOIN trips t ON t.id=s.trip_id "
            "WHERE s.parent_ledger_id=? ORDER BY s.id", (ledger_id,)).fetchall()]
    else:
        rows = [dict(r) for r in conn.execute(
            "SELECT s.*, c.name AS category_name, "
            "       t.name AS trip_name "
            "FROM txn_splits s "
            "LEFT JOIN categories c ON c.id=s.category_id "
            "LEFT JOIN trips t ON t.id=s.trip_id "
            "ORDER BY s.parent_ledger_id, s.id").fetchall()]
    return {'ok': True, 'splits': rows}


# ── write ───────────────────────────────────────────────────────────────

def set_splits(conn=None, *, ledger_id: int,
               allocations: list[dict], actor: str = 'user') -> dict:
    """Atomically replace the split set on a ledger row.

    allocations: [{category_id, trip_id (optional), amount, notes (optional)}].
    Returns {ok, splits, allocated_total, parent_amount, mismatch: bool}.
    """
    conn = _conn(conn)
    parent = store.get_ledger(conn, ledger_id)
    if not parent:
        return {'ok': False, 'error': f'Ledger row {ledger_id} not found'}

    if not allocations:
        return {'ok': True, 'splits': [], 'allocated_total': 0,
                'parent_amount': parent.get('amount', 0), 'mismatch': True}

    norm = []
    seen = set()
    for i, a in enumerate(allocations):
        try:
            cat_id = int(a.get('category_id') or 0)
        except (TypeError, ValueError):
            cat_id = 0
        try:
            amount = int(round(float(a.get('amount') or 0)))
        except (TypeError, ValueError):
            amount = 0
        trip_id = None
        if a.get('trip_id') not in (None, ''):
            try:
                trip_id = int(a['trip_id']) or None
            except (TypeError, ValueError):
                trip_id = None
        if not cat_id:
            return {'ok': False, 'error': f'Allocation #{i+1} missing category_id'}
        if not store.get_category(conn, cat_id):
            return {'ok': False, 'error': f'Allocation #{i+1}: category {cat_id} not found'}
        if trip_id and not store.get_trip(conn, trip_id):
            return {'ok': False, 'error': f'Allocation #{i+1}: trip {trip_id} not found'}
        key = (cat_id, trip_id)
        if key in seen:
            return {'ok': False, 'error':
                    f'Duplicate allocation (category {cat_id}, trip {trip_id})'}
        seen.add(key)
        norm.append({'category_id': cat_id, 'trip_id': trip_id,
                     'amount': amount, 'notes': (a.get('notes') or '').strip()})

    allocated = sum(n['amount'] for n in norm)
    parent_amount = int(parent.get('amount', 0))
    mismatch = allocated != parent_amount

    store.add_audit(conn, action='splits_set', entity='ledger_txns',
                    entity_id=ledger_id,
                    before_json=_splits_json(conn, ledger_id),
                    after_json=__import__('json').dumps(
                        {'allocations': norm, 'allocated_total': allocated},
                        ensure_ascii=False),
                    actor=actor)

    conn.execute('BEGIN')
    try:
        conn.execute("DELETE FROM txn_splits WHERE parent_ledger_id=?",
                     (ledger_id,))
        for n in norm:
            conn.execute(
                "INSERT OR REPLACE INTO txn_splits"
                "(parent_ledger_id,category_id,trip_id,amount,notes) "
                "VALUES(?,?,?,?,?)",
                (ledger_id, n['category_id'], n['trip_id'], n['amount'],
                 n['notes']))
        conn.execute(
            "UPDATE ledger_txns SET review_status='ok', txn_status='void', nature='needs_review' "
            "WHERE id=?",
            (ledger_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {'ok': True, 'splits': list_splits(conn, ledger_id=ledger_id)['splits'],
            'allocated_total': allocated, 'parent_amount': parent_amount,
            'mismatch': mismatch}


def update_split(conn=None, *, split_id: int, category_id: int | None = ...,
                 notes: str | None = None, actor: str = 'user') -> dict:
    """Edit ONE allocation (its category and/or note) in place.

    The All Txns list shows each allocation as its own row, so editing a row
    must change that allocation, not the parent ledger row (which a split
    parent never displays). `category_id=None` clears nothing: a split always
    keeps a category, so pass an id to change it. The allocation key is
    (category_id, trip_id): moving to a category a sibling allocation already
    uses would break it, so that is refused with a clear message instead of
    silently merging amounts."""
    conn = _conn(conn)
    row = conn.execute("SELECT * FROM txn_splits WHERE id=?", (split_id,)).fetchone()
    if not row:
        return {'ok': False, 'error': f'Split {split_id} not found'}
    sets, params = [], []
    if category_id is not ...:
        if not category_id:
            return {'ok': False, 'error': 'A split needs a category'}
        if not conn.execute("SELECT 1 FROM categories WHERE id=?", (category_id,)).fetchone():
            return {'ok': False, 'error': f'Category {category_id} not found'}
        clash = conn.execute(
            "SELECT id FROM txn_splits WHERE parent_ledger_id=? AND category_id=? "
            "AND trip_id IS ? AND id<>?",
            (row['parent_ledger_id'], category_id, row['trip_id'], split_id)).fetchone()
        if clash:
            return {'ok': False, 'error': 'Another part of this transaction already uses '
                                          'that category. Use Split to merge them.'}
        sets.append('category_id=?'); params.append(int(category_id))
    if notes is not None:
        sets.append('notes=?'); params.append(str(notes))
    if not sets:
        return {'ok': True, 'split_id': split_id}
    sets.append("updated_at=strftime('%Y-%m-%dT%H:%M:%S','now','localtime')")
    before = dict(row)
    conn.execute(f"UPDATE txn_splits SET {', '.join(sets)} WHERE id=?", params + [split_id])
    store.add_audit(conn, action='split_edited', entity='txn_splits', entity_id=split_id,
                    before_json=json.dumps({k: before[k] for k in ('category_id', 'notes')}),
                    after_json=json.dumps({'category_id': category_id if category_id is not ... else before['category_id'],
                                           'notes': notes if notes is not None else before['notes']}),
                    actor=actor)
    conn.commit()
    return {'ok': True, 'split_id': split_id, 'ledger_id': row['parent_ledger_id']}


def unsplit(conn=None, *, ledger_id: int, actor: str = 'user') -> dict:
    """Remove every allocation on a ledger row (back to whole-amount)."""
    conn = _conn(conn)
    if not store.get_ledger(conn, ledger_id):
        return {'ok': False, 'error': f'Ledger row {ledger_id} not found'}
    store.add_audit(conn, action='splits_removed', entity='ledger_txns',
                    entity_id=ledger_id,
                    before_json=_splits_json(conn, ledger_id),
                    actor=actor)
    conn.execute("DELETE FROM txn_splits WHERE parent_ledger_id=?", (ledger_id,))
    conn.commit()
    return {'ok': True, 'splits': []}


# ── helpers ─────────────────────────────────────────────────────────────

def _splits_json(conn, ledger_id: int) -> str:
    rows = conn.execute(
        "SELECT category_id, trip_id, amount, notes FROM txn_splits "
        "WHERE parent_ledger_id=?", (ledger_id,)).fetchall()
    return __import__('json').dumps([dict(r) for r in rows], ensure_ascii=False)


def split_balance(conn=None, *, ledger_id: int) -> dict:
    conn = _conn(conn)
    rows = conn.execute(
        "SELECT COALESCE(SUM(amount),0) AS allocated "
        "FROM txn_splits WHERE parent_ledger_id=?", (ledger_id,)).fetchone()
    parent = store.get_ledger(conn, ledger_id)
    allocated = int(rows['allocated'] or 0)
    return {'ok': True, 'allocated_total': allocated,
            'parent_amount': int(parent['amount']) if parent else 0,
            'mismatch': bool(parent) and allocated != int(parent['amount'])}


# ── CLI affordance ──────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse
    import json as _json

    ap = argparse.ArgumentParser(description='Manage split allocations')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('list')
    p.add_argument('--ledger', type=int)
    p = sub.add_parser('set')
    p.add_argument('--ledger', type=int, required=True)
    p.add_argument('--allocs', required=True,
                   help='JSON: [{"category_id":1,"amount":5000}, ...]')
    p = sub.add_parser('remove')
    p.add_argument('--ledger', type=int, required=True)
    p = sub.add_parser('balance')
    p.add_argument('--ledger', type=int, required=True)

    args = ap.parse_args()
    if args.cmd == 'list':
        print(_json.dumps(list_splits(
            ledger_id=args.ledger or None), ensure_ascii=False, default=str))
    elif args.cmd == 'set':
        print(_json.dumps(set_splits(
            ledger_id=args.ledger,
            allocations=_json.loads(args.allocs)), ensure_ascii=False, default=str))
    elif args.cmd == 'remove':
        print(_json.dumps(unsplit(ledger_id=args.ledger), ensure_ascii=False))
    elif args.cmd == 'balance':
        print(_json.dumps(split_balance(ledger_id=args.ledger), ensure_ascii=False))