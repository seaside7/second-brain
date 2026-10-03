"""Trips: named travel/event windows that group transactions.

A trip has its own date band (start_date..end_date), independent of when the
assigned payments actually hit the accounts. Assigning a ledger row to a trip
always writes a corrections row (field 'trip_id'), so reprocess never clears
the assignment, mirroring how category/nature corrections survive.

Trip economics: spend_total sums only expense-nature rows (bank fees and
internal transfers are excluded); income (refunds, deposit-returns) is never
counted as spend but is listed as part of the trip's movement.

Functions here operate on the owner's live DB by default (schema.connect with
no path) but every function accepts an explicit conn for tests/tooling.
"""
from __future__ import annotations

from typing import Optional

import store
from schema import connect


def _conn(conn):
    return conn if conn is not None else connect()


# ── lifecycle ───────────────────────────────────────────────────────────

def create_trip(conn=None, *, name: str, destination: str = '',
                start_date: str = '', end_date: str = '',
                notes: str = '') -> dict:
    """Create a trip and return {ok, trip_id, trip}."""
    conn = _conn(conn)
    name = (name or '').strip()
    if not name:
        return {'ok': False, 'error': 'Trip name is required'}
    for d in (start_date, end_date):
        if d and not str(d)[:10].count('-') == 2:
            return {'ok': False, 'error': f'Invalid date "{d}" (use YYYY-MM-DD)'}
    if start_date and end_date and end_date < start_date:
        return {'ok': False, 'error': 'end_date before start_date'}
    trip_id = store.add_trip(conn, name=name, destination=destination.strip(),
                             start_date=str(start_date)[:10],
                             end_date=str(end_date)[:10],
                             notes=(notes or '').strip())
    store.add_audit(conn, action='trip_create', entity='trips',
                    entity_id=trip_id, after_json=__import__('json').dumps({
                        'name': name, 'destination': destination,
                        'start_date': start_date, 'end_date': end_date}))
    return {'ok': True, 'trip_id': trip_id, 'trip': store.get_trip(conn, trip_id)}


def update_trip(conn=None, *, trip_id: int,
                name: Optional[str] = None,
                destination: Optional[str] = None,
                start_date: Optional[str] = None,
                end_date: Optional[str] = None,
                notes: Optional[str] = None) -> dict:
    conn = _conn(conn)
    trip = store.get_trip(conn, trip_id)
    if not trip:
        return {'ok': False, 'error': f'Trip {trip_id} not found'}
    fields = {k: v for k, v in
              {'name': name, 'destination': destination, 'start_date': start_date,
               'end_date': end_date, 'notes': notes}.items() if v is not None}
    if fields:
        store.update_trip(conn, trip_id, **fields)
        store.add_audit(conn, action='trip_update', entity='trips',
                        entity_id=trip_id,
                        before_json=__import__('json').dumps(
                            {k: trip.get(k, '') for k in fields}),
                        after_json=__import__('json').dumps(fields))
    return {'ok': True, 'trip': store.get_trip(conn, trip_id)}


def list_trips(conn=None) -> dict:
    conn = _conn(conn)
    trips = store.list_trips(conn)
    return {'ok': True, 'trips': trips, 'count': len(trips)}


def get_trip(conn=None, *, trip_id: int) -> dict:
    conn = _conn(conn)
    trip = store.get_trip(conn, trip_id)
    if not trip:
        return {'ok': False, 'error': f'Trip {trip_id} not found'}
    members = store.list_trip_members(conn, trip_id)
    spend = sum(m.get('amount', 0) for m in members
                if m.get('nature') == 'expense')
    return {'ok': True, 'trip': trip, 'members': members,
            'member_count': len(members), 'spend_total': spend}


# ── candidate search ────────────────────────────────────────────────────

def find_trip_candidates(conn=None, *, trip_id: int | None = None,
                         from_date: str = '', to_date: str = '',
                         keyword: str = '',
                         include_assigned: bool = False) -> dict:
    """Suggest ledger rows for a trip.

    Filters by payment-date window (from_date..to_date) and/or a keyword
    matched against description/merchant/recipient/notes. By default rows
    already on another trip are excluded; pass include_assigned=True to list
    them too (e.g. to move a row between trips).
    """
    conn = _conn(conn)
    trip = store.get_trip(conn, trip_id) if trip_id else None
    f, t = from_date, to_date
    if trip and not f and not t:
        f, t = trip.get('start_date', ''), trip.get('end_date', '')
    rows = store.list_ledger(
        conn,
        from_date=f or None,
        to_date=(t or '9999-12-31') if t else None,
        search=(keyword.strip() or None),
        limit=1000)
    if not include_assigned:
        rows = [r for r in rows if r.get('trip_id') is None]
    return {'ok': True, 'rows': rows, 'count': len(rows)}


# ── assign / unassign ───────────────────────────────────────────────────

def _write_assign(conn, ledger_id: int, from_trip, to_trip,
                  reason: str, actor: str) -> None:
    store.update_ledger(conn, ledger_id, trip_id=to_trip)
    store.add_correction(
        conn, ledger_id=ledger_id, field='trip_id',
        before_val=str(from_trip) if from_trip else '',
        after_val=str(to_trip) if to_trip else '',
        reason=reason, actor=actor)
    store.add_audit(conn, action='trip_assign', entity='ledger_txns',
                    entity_id=ledger_id,
                    before_json=__import__('json').dumps({'trip_id': from_trip}),
                    after_json=__import__('json').dumps({'trip_id': to_trip}),
                    actor=actor)


def assign(conn=None, *, trip_id: int, ledger_ids: list[int],
           actor: str = 'user', reason: str = 'trip_assign') -> dict:
    """Assign ledger rows to a trip. Idempotent per row; returns per-row results."""
    conn = _conn(conn)
    if not trip_id:
        return {'ok': False, 'error': 'Missing trip_id'}
    if not store.get_trip(conn, trip_id):
        return {'ok': False, 'error': f'Trip {trip_id} not found'}
    ids = [int(i) for i in (ledger_ids or [])]
    if not ids:
        return {'ok': False, 'error': 'No ledger_ids given'}
    assigned, skipped, missing = [], [], []
    for lid in ids:
        row = store.get_ledger(conn, lid)
        if not row:
            missing.append(lid)
            continue
        from_trip = row.get('trip_id')
        if from_trip == trip_id:
            skipped.append(lid)
            continue
        _write_assign(conn, lid, from_trip, trip_id, reason, actor)
        assigned.append(lid)
    return {'ok': True, 'assigned': assigned, 'already_assigned': skipped,
            'missing': missing}


def unassign(conn=None, *, ledger_ids: list[int], actor: str = 'user',
             reason: str = 'trip_unassign') -> dict:
    """Remove rows from their trips (trip_id -> NULL), writing corrections."""
    conn = _conn(conn)
    ids = [int(i) for i in (ledger_ids or [])]
    if not ids:
        return {'ok': False, 'error': 'No ledger_ids given'}
    removed, skipped, missing = [], [], []
    for lid in ids:
        row = store.get_ledger(conn, lid)
        if not row:
            missing.append(lid)
            continue
        if row.get('trip_id') in (None, ''):
            skipped.append(lid)
            continue
        _write_assign(conn, lid, row['trip_id'], None, reason, actor)
        removed.append(lid)
    return {'ok': True, 'unassigned': removed, 'already_unassigned': skipped,
            'missing': missing}


def move(conn=None, *, from_trip_id: int, to_trip_id: int,
         ledger_ids: list[int], actor: str = 'user') -> dict:
    """Shorthand: assign rows (which may already be on from_trip_id)."""
    return assign(conn, trip_id=to_trip_id, ledger_ids=ledger_ids, actor=actor,
                  reason='trip_assign:move')


# ── CLI affordance ──────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse

    ap = argparse.ArgumentParser(description='Manage transactions trips')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('list')
    p = sub.add_parser('create')
    p.add_argument('--name', required=True)
    p.add_argument('--destination', default='')
    p.add_argument('--start-date', default='')
    p.add_argument('--end-date', default='')
    p.add_argument('--notes', default='')

    p = sub.add_parser('assign')
    p.add_argument('--trip', type=int, required=True)
    p.add_argument('--ledger', type=int, nargs='+', required=True)

    p = sub.add_parser('unassign')
    p.add_argument('--ledger', type=int, nargs='+', required=True)

    p = sub.add_parser('candidates')
    p.add_argument('--trip', type=int)
    p.add_argument('--from', dest='from_date', default='')
    p.add_argument('--to', dest='to_date', default='')
    p.add_argument('--keyword', default='')

    args = ap.parse_args()
    import json as _json
    if args.cmd == 'list':
        print(_json.dumps(list_trips(), ensure_ascii=False))
    elif args.cmd == 'create':
        print(_json.dumps(create_trip(
            name=args.name, destination=args.destination,
            start_date=args.start_date, end_date=args.end_date,
            notes=args.notes), ensure_ascii=False))
    elif args.cmd == 'assign':
        print(_json.dumps(assign(trip_id=args.trip, ledger_ids=args.ledger),
                          ensure_ascii=False))
    elif args.cmd == 'unassign':
        print(_json.dumps(unassign(ledger_ids=args.ledger), ensure_ascii=False))
    elif args.cmd == 'candidates':
        print(_json.dumps(find_trip_candidates(
            trip_id=args.trip, from_date=args.from_date, to_date=args.to_date,
            keyword=args.keyword), ensure_ascii=False, default=str))