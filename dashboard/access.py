"""Role policies: what a signed-in user may reach on the dashboard.

`owner` is unrestricted and never consults this module. Every other role is a
restricted viewer with an explicit allowlist, so a new endpoint is closed to
them until it is listed here (deny by default).

The `dinda` role (family viewer) sees one menu, Transactions, with two views:

* **All Txns**  - only rows that mention "dinda" in the recipient, description,
  merchant or note, whatever their category or review status.
* **Overview**  - the analytics for every category except the hidden ones.

It is read-only: no POST route is reachable except sign-out and the password
change. Row-level scoping is applied in the database layer (`scoped_connection`)
with TEMP VIEWs that shadow the real tables, so every existing report query is
filtered without being modified, and a query that forgets a filter cannot leak.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any

OWNER = 'owner'

# Always reachable for a signed-in user, whatever the role (GET and POST).
ACCOUNT_ROUTES = frozenset({'/api/session', '/api/logout', '/api/change-password'})

# Static files a restricted viewer may load. Page HTML other than the app shell
# is refused (the public folder also holds unrelated standalone pages).
_STATIC_EXT = ('.js', '.css', '.svg', '.png', '.jpg', '.jpeg', '.ico', '.woff',
               '.woff2', '.ttf', '.otf', '.map', '.webmanifest')
_SHELL_ROUTES = frozenset({'/', '/index.html', '/login', '/change-password',
                           '/logout', '/auth.css'})

POLICIES: dict[str, dict[str, Any]] = {
    'dinda': {
        'label': 'Dinda',
        # Sidebar tabs and Transactions sub-views the UI should show.
        'tabs': ['transactions'],
        'tx_views': ['overview', 'all'],
        # API surface (exact paths). Everything else is 403.
        'get': frozenset({
            '/api/transactions/list',
            '/api/transactions/analytics',
            '/api/transactions/categories',
            '/api/transactions/cycle',
        }),
        'post': frozenset(),
        # Row scope per endpoint: 'person' = rows mentioning `person_term`,
        # 'categories' = everything except `hidden_categories`.
        'scope': {
            '/api/transactions/list': 'person',
            '/api/transactions/analytics': 'categories',
            '/api/transactions/categories': 'categories',
            '/api/transactions/cycle': 'categories',
        },
        'person_term': 'dinda',
        'hidden_categories': ('Project Payment', 'Project Cost',
                              'Friend Repayment', 'Arif Loan', 'Friend Loan'),
        # Identifiers removed from rows sent to this role.
        'strip_row_fields': ('account_masked', 'phone_suffix', 'bank_ref',
                             'src_txn_id', 'email_subject', 'raw_description',
                             'trip_name', 'trip_destination'),
    },
}


def is_restricted(role: str | None) -> bool:
    return bool(role) and role != OWNER


def policy(role: str | None) -> dict[str, Any] | None:
    return POLICIES.get(role or '')


def ui_profile(role: str | None) -> dict[str, Any]:
    """What /api/session tells the frontend so it can hide what is closed."""
    if not is_restricted(role):
        return {'role': role or OWNER, 'restricted': False}
    pol = policy(role) or {'tabs': [], 'tx_views': []}
    return {'role': role, 'restricted': True,
            'tabs': list(pol.get('tabs', [])),
            'tx_views': list(pol.get('tx_views', []))}


def allows(role: str | None, method: str, route: str) -> bool:
    """May this role call `method route`? Owners always may."""
    if not is_restricted(role):
        return True
    if route in ACCOUNT_ROUTES:
        return True
    pol = policy(role)
    if pol is None:
        return False
    m = method.upper()
    if m in ('GET', 'HEAD'):
        if route.startswith('/api/'):
            return route in pol['get']
        if route in _SHELL_ROUTES:
            return True
        return route.lower().endswith(_STATIC_EXT)
    if m == 'POST':
        return route in pol['post']
    return False


# ── row-level scoping ────────────────────────────────────────────────────
_TERM_RE = re.compile(r'^[a-z0-9 ]{1,40}$')


def _hidden_ids(conn: sqlite3.Connection, names: tuple[str, ...]) -> list[int]:
    if not names:
        return []
    q = ','.join('?' * len(names))
    return [r[0] for r in conn.execute(
        f'SELECT id FROM main.categories WHERE name IN ({q})', names).fetchall()]


def category_scope(role: str | None) -> tuple[str, ...]:
    pol = policy(role)
    return tuple(pol['hidden_categories']) if pol else ()


def scoped_connection(conn: sqlite3.Connection, role: str | None, endpoint: str):
    """Shadow ledger_txns / txn_splits / categories with filtered TEMP VIEWs on
    `conn` for the role's scope at `endpoint`. Returns the connection.

    TEMP objects take precedence over main ones for unqualified names, so every
    query already written against `ledger_txns` sees only permitted rows. The
    views are read-only: a write through them fails, which is what we want."""
    pol = policy(role)
    if pol is None:
        raise PermissionError('no policy for role')
    kind = pol['scope'].get(endpoint)
    if kind is None:
        raise PermissionError(f'{endpoint} is not available to this role')

    hidden = _hidden_ids(conn, tuple(pol['hidden_categories']))
    hid = ','.join(str(int(i)) for i in hidden) or 'NULL'

    if kind == 'person':
        term = str(pol['person_term']).lower()
        if not _TERM_RE.match(term):
            raise ValueError('invalid person term')
        like = "'%" + term + "%'"
        ledger_where = (
            "lower(coalesce(e.description,'') || ' ' || coalesce(e.raw_description,'') "
            "|| ' ' || coalesce(e.recipient,'') || ' ' || coalesce(e.merchant,'') "
            f"|| ' ' || coalesce(l.notes,'')) LIKE {like}")
    else:  # 'categories': hide the listed categories, including via splits
        ledger_where = (
            f"(l.category_id IS NULL OR l.category_id NOT IN ({hid})) "
            f"AND l.id NOT IN (SELECT parent_ledger_id FROM main.txn_splits "
            f"WHERE category_id IN ({hid}))")

    conn.execute(
        "CREATE TEMP VIEW ledger_txns AS "
        "SELECT l.* FROM main.ledger_txns l "
        "LEFT JOIN main.extracted_txns e ON e.id = l.ext_id "
        f"WHERE {ledger_where}")
    split_cat = '' if kind == 'person' else f" AND s.category_id NOT IN ({hid})"
    conn.execute(
        "CREATE TEMP VIEW txn_splits AS "
        "SELECT s.* FROM main.txn_splits s "
        f"WHERE s.parent_ledger_id IN (SELECT id FROM temp.ledger_txns){split_cat}")
    conn.execute(
        "CREATE TEMP VIEW categories AS "
        f"SELECT * FROM main.categories WHERE id NOT IN ({hid})")
    return conn


def strip_row(role: str | None, row: dict) -> dict:
    pol = policy(role)
    if pol:
        for k in pol.get('strip_row_fields', ()):
            if k in row:
                row[k] = ''
    return row
