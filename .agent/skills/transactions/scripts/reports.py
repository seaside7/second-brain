"""Reports and overview data for the Transactions tab.

Provides spending summaries, cash-flow, category/account breakdowns,
fees, transfers, and pending review counts.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Optional

import store


def overview(conn: sqlite3.Connection, *,
             period: str = 'current_month') -> dict:
    """Return dashboard overview data.

    period: 'current_month' | 'last_month' | 'current_year' | 'all'
    """
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
        'net': summary['income'] + summary['refund'] - summary['expense'] - summary['fee'],
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


def cashflow(conn: sqlite3.Connection, *,
             from_date: str | None = None,
             to_date: str | None = None) -> dict:
    """Cash-flow: total in vs total out by nature."""
    conds = ["l.nature NOT IN ('void',)"]
    params: list[Any] = []
    if from_date:
        conds.append("e.occurred_at >= ?"); params.append(from_date)
    if to_date:
        conds.append("e.occurred_at <= ?"); params.append(to_date)
    where = "WHERE " + " AND ".join(conds)

    sql = (
        "SELECT l.nature, l.direction, SUM(l.amount) as total "
        "FROM ledger_txns l "
        "LEFT JOIN extracted_txns e ON e.id = l.ext_id "
        f"{where} GROUP BY l.nature, l.direction"
    )
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    inflow = sum(r['total'] for r in rows if r['direction'] == 'in')
    outflow = sum(r['total'] for r in rows if r['direction'] == 'out')

    return {'inflow': inflow, 'outflow': outflow, 'net': inflow - outflow, 'rows': rows}


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
    """Return (from_date, to_date) ISO strings for a period."""
    now = datetime.now()
    if period == 'current_month':
        from_date = now.replace(day=1).strftime('%Y-%m-01T00:00:00')
        to_date = now.strftime('%Y-%m-31T23:59:59')
    elif period == 'last_month':
        if now.month == 1:
            from_date = f'{now.year-1}-12-01T00:00:00'
            to_date = f'{now.year-1}-12-31T23:59:59'
        else:
            from_date = now.replace(month=now.month-1, day=1).strftime('%Y-%m-01T00:00:00')
            to_date = now.replace(day=1).strftime('%Y-%m-%dT23:59:59')
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
