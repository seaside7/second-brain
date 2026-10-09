"""Reports and overview data for the Transactions tab.

Provides spending summaries, cash-flow, category/account breakdowns,
fees, transfers, and pending review counts.
"""
from __future__ import annotations

import calendar
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Optional

import store

# ── spend semantics (single source of truth for every card/chart) ──────────
# Spend      = nature 'expense'  (+ nature 'fee' when include_fees is on)
# Income     = nature 'income' + 'refund' + 'cashback'
# Net cash   = income - spend - fees
# Excluded from spend, always: 'internal_transfer', 'top_up', 'needs_review',
# 'void'. 'transfer_to_person' is excluded UNTIL the owner assigns a real
# (non-Transfers) category, which flips the row to 'expense'.
SPEND_NATURES = ('expense',)
FEE_NATURES = ('fee',)
INCOME_NATURES = ('income', 'refund', 'cashback')
TRIP_EXCLUDED_NATURES = store.TRIP_EXCLUDED_NATURES
TRIP_INCOME_NATURES = store.TRIP_INCOME_NATURES


def period_bounds(period: str) -> tuple[Optional[str], Optional[str]]:
    """Public wrapper for named ranges ('current_month' | 'last_month' |
    'current_year' | 'all'). Returns (from_date, to_date) ISO strings; a
    None means unbounded. Unknown periods fall back to unbounded."""
    return _period_bounds(period)


def overview(conn: sqlite3.Connection, *,
             period: str = 'current_month',
             from_date: str | None = None,
             to_date: str | None = None) -> dict:
    """Return dashboard overview data.

    period: 'current_month' | 'last_month' | 'current_year' | 'all'
    Explicit from_date/to_date (ISO strings) override the named period.
    """
    if from_date is None and to_date is None:
        from_date, to_date = _period_bounds(period)

    summary = store.spending_summary(conn, from_date=from_date, to_date=to_date)

    # Pending / review counts
    pending_review = store.count_ledger(conn, review_status='review')
    pending_reconcile = store.count_ledger(conn, txn_status='reconciling')
    uncategorized = store.count_ledger(conn, review_status='uncategorized')

    # Transfer summary
    suggested = conn.execute(
        "SELECT COUNT(*) FROM transfers WHERE status='suggested'"
    ).fetchone()[0]

    # Recent transactions (last 5)
    recent = store.list_ledger(conn, limit=5)

    return {
        'period': period,
        'from_date': from_date,
        'to_date': to_date,
        'expense': summary['expense'],
        'fee': summary['fee'],
        'income': summary['income'],
        'refund': summary['refund'],
        'cashback': summary['cashback'],
        'transfer_to_person': summary['transfer_to_person'],
        'internal_transfer': summary['internal_transfer'],
        'top_up': summary['top_up'],
        'total_count': summary['total_count'],
        # Net cash: all money in minus spend and fees.
        'net': (summary['income'] + summary['refund'] + summary['cashback']
               - summary['expense'] - summary['fee']),
        'by_category': summary['by_category'],
        'by_account': summary['by_account'],
        'pending_review': pending_review,
        'pending_reconcile': pending_reconcile,
        'uncategorized': uncategorized,
        'suggested_transfers': suggested,
        'recent': recent,
    }


def spending_breakdown(conn: sqlite3.Connection, *,
                       from_date: str | None = None,
                       to_date: str | None = None) -> dict:
    """Spending by category for charts."""
    conds = ["l.nature = 'expense'"]
    params: list[Any] = []
    if from_date:
        conds.append("e.occurred_at >= ?"); params.append(from_date)
    if to_date:
        conds.append("e.occurred_at <= ?"); params.append(to_date)
    where = "WHERE " + " AND ".join(conds)

    sql = (
        "SELECT COALESCE(c.name, 'Uncategorized') as category_name, "
        "       SUM(l.amount) as total, COUNT(*) as count "
        "FROM ledger_txns l "
        "LEFT JOIN extracted_txns e ON e.id = l.ext_id "
        "LEFT JOIN categories c ON c.id = l.category_id "
        f"{where} GROUP BY l.category_id ORDER BY total DESC"
    )
    return {'by_category': [dict(r) for r in conn.execute(sql, params).fetchall()]}


def analytics(conn: sqlite3.Connection, *,
              from_date: str | None = None,
              to_date: str | None = None,
              cmp_from: str | None = None,
              cmp_to: str | None = None,
              category_ids: list[int] | None = None,
              providers: list[str] | None = None,
              include_fees: bool = True,
              include_transfers: bool = False,
              granularity: str = 'auto') -> dict:
    """Finance-overview dataset: time buckets with per-category spend series,
    month-on-month per-category deltas, and reconciled totals.

    Spend follows SPEND_NATURES (+ fees / person-transfers per the toggles).
    Totals are computed from the SAME bucket rows the charts render, so cards
    and charts always agree with each other (and with the list view, which
    filters on the identical predicates).
    """
    spend_natures = list(SPEND_NATURES)
    if include_fees:
        spend_natures += [n for n in FEE_NATURES if n not in spend_natures]
    if include_transfers:
        spend_natures.append('transfer_to_person')

    days = _range_days(from_date, to_date)
    if granularity == 'auto':
        granularity = 'day' if (days is None or days <= 62) else 'month'
    cut = 7 if granularity == 'month' else 10  # 'YYYY-MM' | 'YYYY-MM-DD'

    def _spend_rows(f: str | None, t: str | None) -> list[dict]:
        conds = [f"l.nature IN ({','.join('?' * len(spend_natures))})"]
        params: list[Any] = list(spend_natures)
        if f:
            conds.append("e.occurred_at >= ?"); params.append(f)
        if t:
            next_day = (date.fromisoformat(t[:10]) + timedelta(days=1)).isoformat()
            conds.append("e.occurred_at < ?"); params.append(next_day)
        if category_ids:
            conds.append(f"l.category_id IN ({','.join('?' * len(category_ids))})")
            params += list(category_ids)
        if providers:
            conds.append(f"e.provider IN ({','.join('?' * len(providers))})")
            params += list(providers)
        where = "WHERE " + " AND ".join(conds)
        where += " AND l.id NOT IN (SELECT parent_ledger_id FROM txn_splits)"
        base_cols = (
            f"SELECT substr(e.occurred_at, 1, {cut}) AS bucket, "
            "       l.category_id AS category_id, "
            "       COALESCE(c.name, 'Uncategorized') AS name, "
            "       COALESCE(c.\"group\", 'Uncategorized') AS grp, "
            "       SUM(l.amount) AS total, COUNT(*) AS cnt "
            "FROM ledger_txns l "
            "LEFT JOIN extracted_txns e ON e.id = l.ext_id "
            "LEFT JOIN categories c ON c.id = l.category_id "
            f"{where} AND l.category_id IS NOT NULL "
            "GROUP BY bucket, l.category_id HAVING COUNT(*) > 0"
        )
        split_cols = (
            f"SELECT substr(e.occurred_at, 1, {cut}) AS bucket, "
            "       s.category_id AS category_id, "
            "       COALESCE(c.name, 'Uncategorized') AS name, "
            "       COALESCE(c.\"group\", 'Uncategorized') AS grp, "
            "       SUM(s.amount) AS total, COUNT(*) AS cnt "
            "FROM txn_splits s "
            "JOIN ledger_txns l ON l.id = s.parent_ledger_id "
            "LEFT JOIN extracted_txns e ON e.id = l.ext_id "
            "LEFT JOIN categories c ON c.id = s.category_id "
            f"WHERE l.nature IN ({','.join('?' * len(spend_natures))}) "
            f"AND s.category_id IS NOT NULL "
            "GROUP BY bucket, s.category_id HAVING COUNT(*) > 0"
        )
        sp: list[Any] = list(spend_natures)
        if f:
            split_cols += " AND e.occurred_at >= ?"; sp.append(f)
        if t:
            next_day = (date.fromisoformat(t[:10]) + timedelta(days=1)).isoformat()
            split_cols += " AND e.occurred_at < ?"; sp.append(next_day)
        if category_ids:
            split_cols += f" AND s.category_id IN ({','.join('?' * len(category_ids))})"
            sp += list(category_ids)
        if providers:
            split_cols += f" AND e.provider IN ({','.join('?' * len(providers))})"
            sp += list(providers)
        sql = f"{base_cols} UNION ALL {split_cols}"
        all_params = params + sp
        rows = [dict(r) for r in conn.execute(sql, all_params).fetchall()]
        aggregated: dict[tuple, dict] = {}
        for r in rows:
            key = (r['bucket'], r['category_id'])
            if key in aggregated:
                aggregated[key]['total'] += r['total']
                aggregated[key]['cnt'] += r['cnt']
            else:
                aggregated[key] = dict(r)
        return list(aggregated.values())

    cur = _spend_rows(from_date, to_date)
    prev = _spend_rows(cmp_from, cmp_to) if (cmp_from or cmp_to) else []

    buckets: dict[str, dict] = {}
    categories: dict[Any, dict] = {}
    for r in cur:
        b = buckets.setdefault(r['bucket'], {'key': r['bucket'], 'spend': 0,
                                             'count': 0, 'by_category': {}})
        b['spend'] += r['total'] or 0
        b['count'] += r['cnt'] or 0
        b['by_category'][r['category_id']] = r['total'] or 0
        categories[r['category_id']] = {'id': r['category_id'],
                                        'name': r['name'], 'group': r['grp']}
    for r in prev:
        categories.setdefault(r['category_id'], {'id': r['category_id'],
                                                 'name': r['name'],
                                                 'group': r['grp']})

    cur_by_cat: dict[Any, float] = {}
    for r in cur:
        cur_by_cat[r['category_id']] = cur_by_cat.get(r['category_id'], 0) + (r['total'] or 0)
    prev_by_cat: dict[Any, float] = {}
    for r in prev:
        prev_by_cat[r['category_id']] = prev_by_cat.get(r['category_id'], 0) + (r['total'] or 0)

    mom = []
    for cid in set(cur_by_cat) | set(prev_by_cat):
        c, p = cur_by_cat.get(cid, 0), prev_by_cat.get(cid, 0)
        mom.append({'category_id': cid,
                    'name': categories[cid]['name'],
                    'group': categories[cid]['group'],
                    'current': c, 'previous': p, 'delta': c - p,
                    'pct': (None if p == 0 else round((c - p) / p * 100, 1))})
    mom.sort(key=lambda m: m['current'], reverse=True)

    totals = _movement_totals(conn, from_date, to_date, providers,
                              include_fees, include_transfers)
    totals['spend'] = sum(b['spend'] for b in buckets.values())
    totals['count'] = sum(b['count'] for b in buckets.values())

    bucket_keys = sorted(buckets.keys())
    return {
        'granularity': granularity,
        'from_date': from_date, 'to_date': to_date,
        'cmp_from': cmp_from, 'cmp_to': cmp_to,
        'first_bucket': bucket_keys[0] if bucket_keys else None,
        'last_bucket': bucket_keys[-1] if bucket_keys else None,
        'buckets': list(buckets.values()),
        'categories': categories,
        'mom': mom,
        'totals': totals,
    }


def trip_breakdown(conn: sqlite3.Connection, *, trip_id: int) -> dict:
    """Per-trip spend report under the resolved trip accounting rules.

    Members come from two sources, never double-counted:
    - whole-row members: ledger rows with l.trip_id = trip
    - split members:    txn_splits allocations pinned to the trip

    A row that carries trip-pinned splits contributes ONLY its split amounts,
    never its own amount. 'internal_transfer'/'top_up'/'void' never count even
    when assigned. Income natures are reported as movement, not spend.

    Returns {ok, trip, spend_total, member_count, members, by_category,
    movement, excluded}.
    """
    trip = store.get_trip(conn, trip_id)
    if not trip:
        return {'ok': False, 'error': f'Trip {trip_id} not found'}

    rows = [dict(r) for r in conn.execute(
        "SELECT l.id, l.amount, l.direction, l.nature, l.category_id, "
        "       l.review_status, l.notes, "
        "       e.occurred_at, e.provider, e.description, e.merchant, "
        "       e.recipient, "
        "       c.name AS category_name, c.\"group\" AS category_group "
        "FROM ledger_txns l "
        "LEFT JOIN extracted_txns e ON e.id=l.ext_id "
        "LEFT JOIN categories c ON c.id=l.category_id "
        "WHERE l.trip_id=?", (trip_id,)).fetchall()]

    splits = [dict(r) for r in conn.execute(
        "SELECT s.id AS split_id, s.parent_ledger_id AS id, s.amount, "
        "       s.category_id, s.notes, "
        "       c.name AS category_name, c.\"group\" AS category_group, "
        "       l.nature, l.direction, l.review_status, "
        "       e.occurred_at, e.provider, e.description, e.merchant, "
        "       e.recipient "
        "FROM txn_splits s "
        "LEFT JOIN ledger_txns l ON l.id=s.parent_ledger_id "
        "LEFT JOIN extracted_txns e ON e.id=l.ext_id "
        "LEFT JOIN categories c ON c.id=s.category_id "
        "WHERE s.trip_id=?", (trip_id,)).fetchall()]

    # A row that has ANY split allocations is never counted as a whole member
    # (its split amounts carry the spend, each pinned to its own trip). The
    # exclusion applies even when the split belongs to a different trip, so
    # every rupiah is counted exactly once across trips.
    split_parents = {r[0] for r in conn.execute(
        "SELECT DISTINCT parent_ledger_id FROM txn_splits").fetchall()}

    members: list[dict] = []
    by_cat: dict[Any, dict] = {}
    movement: dict[str, int] = {}
    excluded: list[dict] = []

    def _emit(cat_key: Any, amount: int, name: str, group: str) -> None:
        b = by_cat.setdefault(cat_key, {'category_id': cat_key,
                                        'category_name': name or 'Uncategorized',
                                        'group': group or 'Uncategorized',
                                        'total': 0, 'count': 0})
        b['total'] += amount
        b['count'] += 1

    def _row(ledger_id: int, kind: str, nature: str,
             split_id: int | None = None) -> dict:
        return {
            'id': ledger_id,
            'kind': kind,
            'split_id': split_id,
            'amount': 0, 'direction': 'out', 'nature': nature or 'expense',
            'category_id': None, 'category_name': None, 'group': None,
            'notes': '', 'occurred_at': '', 'provider': '',
            'description': 'Unknown', 'review_status': 'ok',
        }

    # Whole-row members first (their split amounts are handled below).
    for r in rows:
        if r['id'] in split_parents:
            continue
        nature = r['nature']
        amount = int(r['amount'])
        m = _row(r['id'], 'row', nature)
        m.update({'amount': amount,
                  'direction': r.get('direction', 'out'),
                  'category_id': r.get('category_id'),
                  'category_name': r.get('category_name'),
                  'group': r.get('category_group'),
                  'notes': r.get('notes') or '',
                  'occurred_at': r.get('occurred_at') or '',
                  'provider': r.get('provider') or '',
                  'description': (r.get('description') or r.get('merchant')
                                  or r.get('recipient') or 'Unknown'),
                  'review_status': r.get('review_status') or 'ok'})
        if nature in TRIP_EXCLUDED_NATURES:
            excluded.append({'id': r['id'], 'amount': amount,
                             'description': m['description'],
                             'reason': f'{nature} never counts as trip spend'})
            continue
        if nature in TRIP_INCOME_NATURES:
            movement[nature] = movement.get(nature, 0) + amount
            members.append(m)
            continue
        members.append(m)
        _emit(r.get('category_id'), amount, r.get('category_name'),
              r.get('category_group'))

    # Split members (partial allocations pinned to this trip).
    for s in splits:
        amount = int(s['amount'])
        m = _row(s['id'], 'split', s.get('nature') or 'expense',
                 split_id=s.get('split_id'))
        m.update({'amount': amount,
                  'direction': s.get('direction', 'out'),
                  'category_id': s.get('category_id'),
                  'category_name': s.get('category_name'),
                  'group': s.get('category_group'),
                  'notes': s.get('notes') or '',
                  'occurred_at': s.get('occurred_at') or '',
                  'provider': s.get('provider') or '',
                  'description': (s.get('description') or s.get('merchant')
                                  or s.get('recipient') or 'Unknown'),
                  'review_status': s.get('review_status') or 'ok'})
        members.append(m)
        _emit(s.get('category_id'), amount, s.get('category_name'),
              s.get('category_group'))

    members.sort(key=lambda m: (m.get('occurred_at') or '', m['id']),
                 reverse=True)
    by_category = sorted(by_cat.values(), key=lambda b: b['total'], reverse=True)
    spend_total = sum(m['amount'] for m in members
                      if m['nature'] not in TRIP_EXCLUDED_NATURES
                      and m['nature'] not in TRIP_INCOME_NATURES)

    return {'ok': True, 'trip': trip,
            'spend_total': spend_total,
            'member_count': len(members),
            'members': members,
            'by_category': by_category,
            'movement': movement,
            'excluded': excluded}


def _range_days(from_date: str | None, to_date: str | None) -> int | None:
    """Whole days between two ISO bounds; None when unbounded."""
    try:
        if not from_date or not to_date:
            return None
        a = datetime.fromisoformat(from_date[:10])
        b = datetime.fromisoformat(to_date[:10])
        return max(0, (b - a).days)
    except (ValueError, TypeError):
        return None


def _movement_totals(conn: sqlite3.Connection, from_date: str | None,
                     to_date: str | None, providers: list[str] | None,
                     include_fees: bool, include_transfers: bool) -> dict:
    """Income / movement-line totals over a range (provider filter applies)."""
    conds = ["l.txn_status NOT IN ('void')"]
    params: list[Any] = []
    if from_date:
        conds.append("e.occurred_at >= ?"); params.append(from_date)
    if to_date:
        conds.append("e.occurred_at <= ?"); params.append(to_date)
    if providers:
        conds.append(f"e.provider IN ({','.join('?' * len(providers))})")
        params += list(providers)
    where = "WHERE " + " AND ".join(conds)
    rows = [dict(r) for r in conn.execute(
        "SELECT l.nature, SUM(l.amount) AS total, COUNT(*) AS cnt "
        "FROM ledger_txns l "
        "LEFT JOIN extracted_txns e ON e.id = l.ext_id "
        f"{where} GROUP BY l.nature", params).fetchall()]
    by_nature = {r['nature']: (r['total'] or 0) for r in rows}
    spend_extra = (by_nature.get('fee', 0) if include_fees else 0)
    if include_transfers:
        spend_extra += by_nature.get('transfer_to_person', 0)
    income = sum(by_nature.get(n, 0) for n in INCOME_NATURES)
    spend = by_nature.get('expense', 0) + spend_extra
    uncategorized = conn.execute(
        "SELECT COUNT(*) FROM ledger_txns l "
        "LEFT JOIN extracted_txns e ON e.id = l.ext_id "
        f"{where} AND l.review_status = 'uncategorized'", params).fetchone()[0]
    pending_review = conn.execute(
        "SELECT COUNT(*) FROM ledger_txns WHERE review_status='review'"
    ).fetchone()[0]
    suggested = conn.execute(
        "SELECT COUNT(*) FROM transfers WHERE status='suggested'"
    ).fetchone()[0]
    return {
        'income': income,
        'fees': by_nature.get('fee', 0),
        'transfer_out': by_nature.get('transfer_to_person', 0),
        'internal': by_nature.get('internal_transfer', 0),
        'top_up': by_nature.get('top_up', 0),
        'income_minus_spend': income - spend,
        'uncategorized': uncategorized or 0,
        'pending_review': pending_review or 0,
        'suggested_transfers': suggested or 0,
    }


def fees_total(conn: sqlite3.Connection, *,
               from_date: str | None = None,
               to_date: str | None = None) -> dict:
    """Total fees over a period."""
    conds = ["l.nature = 'fee'"]
    params: list[Any] = []
    if from_date:
        conds.append("e.occurred_at >= ?"); params.append(from_date)
    if to_date:
        conds.append("e.occurred_at <= ?"); params.append(to_date)
    where = "WHERE " + " AND ".join(conds)

    sql = (
        "SELECT SUM(l.amount) as total, COUNT(*) as count "
        "FROM ledger_txns l "
        "LEFT JOIN extracted_txns e ON e.id = l.ext_id "
        f"{where}"
    )
    r = conn.execute(sql, params).fetchone()
    return {'total': r[0] if r else 0, 'count': r[1] if r else 0}


def pending_review_list(conn: sqlite3.Connection, *,
                        days: int = 7) -> list[dict]:
    """List transactions awaiting review."""
    return store.list_ledger(conn, review_status='review', limit=50)


def pending_transfers_list(conn: sqlite3.Connection) -> list[dict]:
    """List suggested transfers awaiting decision."""
    return store.list_transfers(conn, status='suggested', limit=50)


def _period_bounds(period: str) -> tuple[Optional[str], Optional[str]]:
    """Return (from_date, to_date) ISO strings for a period.

    Salary-cycle months: from 25th of the previous month to 24th of the
    current month (salary received on the 25th).
    """
    now = datetime.now()
    if period == 'current_month':
        from_day = 25
        to_day = 24
        if now.day >= 25:
            from_month = now
            to_month = now.replace(day=1) + timedelta(days=32)
            to_month = to_month.replace(day=24)
        else:
            prev_month = now.replace(day=1) - timedelta(days=1)
            from_month = prev_month.replace(day=25)
            to_month = now.replace(day=24)
        from_date = f'{from_month.year}-{from_month.month:02d}-25T00:00:00'
        to_date = f'{to_month.year}-{to_month.month:02d}-24T23:59:59'
    elif period == 'last_month':
        prev_month = now.replace(day=1) - timedelta(days=1)
        from_month = prev_month.replace(day=25)
        to_month = prev_month.replace(day=24)
        from_date = f'{from_month.year}-{from_month.month:02d}-25T00:00:00'
        to_date = f'{to_month.year}-{to_month.month:02d}-24T23:59:59'
    elif period == 'current_year':
        from_date = f'{now.year}-01-01T00:00:00'
        to_date = f'{now.year}-12-31T23:59:59'
    elif period == 'all':
        from_date = None
        to_date = None
    else:
        from_date = None
        to_date = None
    return from_date, to_date
