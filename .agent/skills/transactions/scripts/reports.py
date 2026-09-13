"""Reports and overview data for the Transactions tab.

Provides spending summaries, cash-flow, category/account breakdowns,
fees, transfers, and pending review counts.
"""
from __future__ import annotations

import calendar
import sqlite3
from datetime import datetime, timedelta
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
            conds.append("e.occurred_at <= ?"); params.append(t)
        if category_ids:
            conds.append(f"l.category_id IN ({','.join('?' * len(category_ids))})")
            params += list(category_ids)
        if providers:
            conds.append(f"e.provider IN ({','.join('?' * len(providers))})")
            params += list(providers)
        where = "WHERE " + " AND ".join(conds)
        sql = (
            f"SELECT substr(e.occurred_at, 1, {cut}) AS bucket, "
            "       l.category_id AS category_id, "
            "       COALESCE(c.name, 'Uncategorized') AS name, "
            "       COALESCE(c.\"group\", 'Uncategorized') AS grp, "
            "       SUM(l.amount) AS total, COUNT(*) AS cnt "
            "FROM ledger_txns l "
            "LEFT JOIN extracted_txns e ON e.id = l.ext_id "
            "LEFT JOIN categories c ON c.id = l.category_id "
            f"{where} GROUP BY bucket, l.category_id ORDER BY bucket"
        )
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

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

    return {
        'granularity': granularity,
        'from_date': from_date, 'to_date': to_date,
        'cmp_from': cmp_from, 'cmp_to': cmp_to,
        'buckets': list(buckets.values()),
        'categories': categories,
        'mom': mom,
        'totals': totals,
    }


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
    conds = ["l.nature NOT IN ('void')"]
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

    Bounds are calendar-correct: current_month ends on the month's real last
    day, last_month covers exactly the previous calendar month.
    """
    now = datetime.now()
    if period == 'current_month':
        last_day = calendar.monthrange(now.year, now.month)[1]
        from_date = f'{now.year}-{now.month:02d}-01T00:00:00'
        to_date = f'{now.year}-{now.month:02d}-{last_day:02d}T23:59:59'
    elif period == 'last_month':
        first_this = now.replace(day=1)
        prev_end = first_this - timedelta(days=1)
        from_date = f'{prev_end.year}-{prev_end.month:02d}-01T00:00:00'
        to_date = (f'{prev_end.year}-{prev_end.month:02d}-{prev_end.day:02d}'
                   'T23:59:59')
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
