"""SQLite schema and connection helpers for the Transactions feature.

DB lives at `.agent/workspaces/personal/state/transactions.db` (WAL mode,
stdlib sqlite3).  Every table is created here via ``ensure_tables``; the
module also exposes ``connect`` and ``WS_DB`` helpers so callers never
construct paths themselves.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

_DB_DIR = Path(os.environ.get(
    'TRANSACTIONS_DB_DIR',
    Path(__file__).resolve().parent.parent.parent.parent / 'workspaces' / 'personal' / 'state'
))

DB_PATH = _DB_DIR / 'transactions.db'

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def connect(path: Path | str | None = None, *,
            timeout: float = 30.0,
            journal: str = 'WAL',
            foreign: bool = True,
            busy: int = 5000) -> sqlite3.Connection:
    """Open a connection to the transactions database.

    Parameters
    ----------
    path : str | Path | None
        Override DB file path.  ``None`` (default) uses ``DB_PATH``.
    journal : str
        Journal mode applied at open.  Default ``'WAL'``.
    foreign : bool
        Enable ``PRAGMA foreign_keys = ON``.
    busy : int
        ``PRAGMA busy_timeout`` in milliseconds.
    """
    p = Path(path) if path else DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=timeout, isolation_level='DEFERRED')
    conn.row_factory = sqlite3.Row
    conn.execute(f'PRAGMA journal_mode={journal}')
    conn.execute(f'PRAGMA busy_timeout={busy}')
    if foreign:
        conn.execute('PRAGMA foreign_keys=ON')
    return conn


# ---------------------------------------------------------------------------
# Schema – idempotent
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 4

_SOURCE_DOC_CREATE = """
CREATE TABLE IF NOT EXISTS source_documents (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    kind             TEXT    NOT NULL CHECK(kind IN ('gmail','gopay_pdf','bca_pdf','bni_pdf','manual')),
    source_key       TEXT    NOT NULL,
    fingerprint      TEXT    NOT NULL,
    provider         TEXT    NOT NULL DEFAULT '',
    fetched_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
    parser_version   TEXT    NOT NULL DEFAULT '',
    email_sender     TEXT    NOT NULL DEFAULT '',
    email_subject    TEXT    NOT NULL DEFAULT '',
    email_received_at TEXT   NOT NULL DEFAULT '',
    preview          TEXT    NOT NULL DEFAULT '',
    status           TEXT    NOT NULL DEFAULT 'new' CHECK(status IN ('new','parsed','failed')),
    error            TEXT    NOT NULL DEFAULT '',
    upload_path      TEXT    NOT NULL DEFAULT '',
    UNIQUE(source_key),
    UNIQUE(fingerprint)
);
"""

_DDL = f"""
-- accounts: one row per bank/e-wallet/account
CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT    NOT NULL CHECK(type IN ('bank','ewallet','card','cash','other')),
    provider    TEXT    NOT NULL DEFAULT '',
    alias       TEXT    NOT NULL,
    masked      TEXT    NOT NULL DEFAULT '',
    owner_name  TEXT    NOT NULL DEFAULT '',
    currency    TEXT    NOT NULL DEFAULT 'IDR',
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
    updated_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))
);

{_SOURCE_DOC_CREATE}

-- import_batches: each upload / gmail sync batch
CREATE TABLE IF NOT EXISTS import_batches (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source_document_id INTEGER NOT NULL REFERENCES source_documents(id) ON DELETE CASCADE,
    created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
    state             TEXT    NOT NULL DEFAULT 'preview'
                            CHECK(state IN ('preview','confirmed','cancelled','committed')),
    stats_json        TEXT    NOT NULL DEFAULT '{{}}'
);

-- extracted_txns: immutable original records (one-to-one with source)
CREATE TABLE IF NOT EXISTS extracted_txns (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id           INTEGER NOT NULL REFERENCES source_documents(id) ON DELETE CASCADE,
    batch_id         INTEGER NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
    ext_index        INTEGER NOT NULL DEFAULT 0,
    provider         TEXT    NOT NULL DEFAULT '',
    src_txn_id       TEXT    NOT NULL DEFAULT '',
    description      TEXT    NOT NULL DEFAULT '',
    raw_description  TEXT    NOT NULL DEFAULT '',
    transaction_type TEXT    NOT NULL DEFAULT '',
    merchant         TEXT    NOT NULL DEFAULT '',
    recipient        TEXT    NOT NULL DEFAULT '',
    phone_suffix     TEXT    NOT NULL DEFAULT '',
    direction        TEXT    NOT NULL CHECK(direction IN ('in','out')),
    principal_amount INTEGER NOT NULL DEFAULT 0,
    fee_amount       INTEGER NOT NULL DEFAULT 0,
    total_amount     INTEGER NOT NULL DEFAULT 0,
    currency         TEXT    NOT NULL DEFAULT 'IDR',
    occurred_at      TEXT    NOT NULL DEFAULT '',
    bank_ref         TEXT    NOT NULL DEFAULT '',
    source_page      INTEGER NOT NULL DEFAULT 0,
    raw1             TEXT    NOT NULL DEFAULT '',
    raw2             TEXT    NOT NULL DEFAULT '',
    parser_version   TEXT    NOT NULL DEFAULT '',
    dup_status       TEXT    NOT NULL DEFAULT 'unique'
                            CHECK(dup_status IN ('unique','exact_dup','possible')),
    duplicate_of     INTEGER DEFAULT NULL,
    UNIQUE(doc_id, src_txn_id),
    UNIQUE(doc_id, source_page, ext_index)
);

-- ledger_txns: mutable normalised records (one-to-one with extracted)
CREATE TABLE IF NOT EXISTS ledger_txns (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ext_id           INTEGER NOT NULL UNIQUE REFERENCES extracted_txns(id) ON DELETE CASCADE,
    account_id       INTEGER DEFAULT NULL REFERENCES accounts(id),
    amount           INTEGER NOT NULL DEFAULT 0,
    direction        TEXT    NOT NULL CHECK(direction IN ('in','out')),
    nature           TEXT    NOT NULL DEFAULT 'needs_review'
                           CHECK(nature IN ('expense','income','internal_transfer',
                                            'transfer_to_person','top_up','fee',
                                            'refund','cashback','debt_repayment',
                                            'pending_reconcile','needs_review')),
    category_id      INTEGER DEFAULT NULL,
    notes            TEXT    NOT NULL DEFAULT '',
    confidence       TEXT    NOT NULL DEFAULT 'none'
                           CHECK(confidence IN ('high','medium','low','none')),
    confidence_reason TEXT   NOT NULL DEFAULT '',
    evidence_json    TEXT    NOT NULL DEFAULT '{{}}',
    review_status    TEXT    NOT NULL DEFAULT 'ok'
                           CHECK(review_status IN ('ok','review','uncategorized')),
    txn_status       TEXT    NOT NULL DEFAULT 'confirmed'
                           CHECK(txn_status IN ('confirmed','needs_review','reconciling',
                                                'matched','void')),
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
    updated_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))
);

-- transfers: reconciliation link between two ledger rows
CREATE TABLE IF NOT EXISTS transfers (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    from_ledger_id   INTEGER NOT NULL UNIQUE REFERENCES ledger_txns(id) ON DELETE CASCADE,
    to_ledger_id     INTEGER NOT NULL UNIQUE REFERENCES ledger_txns(id) ON DELETE CASCADE,
    principal_amount INTEGER NOT NULL DEFAULT 0,
    fee_ledger_id    INTEGER DEFAULT NULL REFERENCES ledger_txns(id),
    status           TEXT    NOT NULL DEFAULT 'unmatched'
                           CHECK(status IN ('confirmed','suggested','manual','rejected','unmatched')),
    matched_by       TEXT    NOT NULL DEFAULT '',
    score            REAL    NOT NULL DEFAULT 0.0,
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))
);

-- categories: built-in + user-created
CREATE TABLE IF NOT EXISTS categories (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT    NOT NULL UNIQUE,
    "group" TEXT    NOT NULL DEFAULT '',
    builtin INTEGER NOT NULL DEFAULT 0
);

-- category_rules: remembered merchant/recipient -> category + nature
CREATE TABLE IF NOT EXISTS category_rules (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    merchant_or_recipient TEXT NOT NULL NOT NULL,
    category_id        INTEGER NOT NULL REFERENCES categories(id),
    nature             TEXT    NOT NULL DEFAULT 'expense',
    source             TEXT    NOT NULL DEFAULT 'manual'
                             CHECK(source IN ('manual','ai')),
    confidence         REAL   NOT NULL DEFAULT 1.0,
    active             INTEGER NOT NULL DEFAULT 1,
    times_applied      INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))
);

-- corrections: every manual change to a ledger row
CREATE TABLE IF NOT EXISTS corrections (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ledger_id  INTEGER NOT NULL REFERENCES ledger_txns(id) ON DELETE CASCADE,
    field      TEXT    NOT NULL,
    before_val TEXT    NOT NULL DEFAULT '',
    after_val  TEXT    NOT NULL DEFAULT '',
    reason     TEXT    NOT NULL DEFAULT '',
    actor      TEXT    NOT NULL DEFAULT 'user',
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))
);

-- audit_log: append-only event log
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
    actor       TEXT    NOT NULL DEFAULT 'user',
    action      TEXT    NOT NULL DEFAULT '',
    entity      TEXT    NOT NULL DEFAULT '',
    entity_id   INTEGER DEFAULT NULL,
before_json TEXT    NOT NULL DEFAULT '{{}}',
after_json  TEXT    NOT NULL DEFAULT '{{}}',
    source      TEXT    NOT NULL DEFAULT 'web'
);

-- sync_state: key-value store for Gmail historyId, timestamps, etc.
CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

-- schema_version marker (single row, updated on migration)
CREATE TABLE IF NOT EXISTS _meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""


def _rebuild_table(conn: sqlite3.Connection, name: str, create_sql: str) -> None:
    """Replace table ``name`` with ``create_sql``, preserving all rows and ids."""
    import re

    cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{name}")').fetchall()]
    rows = conn.execute(f'SELECT * FROM "{name}"').fetchall()
    tmp = f'{name}__mig2'
    conn.execute(f'DROP TABLE IF EXISTS "{tmp}"')
    create_sql = re.sub(rf'CREATE TABLE\s+"?{name}"?',
                        f'CREATE TABLE "{tmp}"', create_sql, count=1)
    conn.execute(create_sql)
    if rows:
        colspec = ', '.join(f'"{c}"' for c in cols)
        ph = ', '.join(['?'] * len(cols))
        conn.executemany(f'INSERT INTO "{tmp}" ({colspec}) VALUES ({ph})', rows)
    conn.execute(f'DROP TABLE "{name}"')
    conn.execute(f'ALTER TABLE "{tmp}" RENAME TO "{name}"')


def _repair_dangling_fks(conn: sqlite3.Connection) -> None:
    """Rebuild any table whose FK was rewritten to ``source_documents__old``.

    ``ALTER TABLE source_documents RENAME TO source_documents__old`` rewrites
    foreign-key references in dependent tables (import_batches, extracted_txns)
    to the new name. After the old table is dropped those references dangle, so
    rebuild such tables pointing back at the rebuilt source_documents.
    """
    for name, sql in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' "
            "AND sql LIKE '%source_documents__old%'").fetchall():
        _rebuild_table(conn, name, sql.replace('source_documents__old',
                                               'source_documents'))


def _migrate_source_documents_kind(conn: sqlite3.Connection) -> None:
    """Widen the source_documents.kind CHECK on pre-existing databases.

    PRAGMA foreign_keys cannot change mid-transaction, so hold it OFF for the
    whole migration and repair any dependent-table FKs before re-enabling it.
    """
    conn.commit()
    conn.execute('PRAGMA foreign_keys=OFF')
    try:
        sd = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='source_documents'").fetchone()
        has_old = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='source_documents__old'").fetchone()
        sd_sql = (sd[0] or '') if sd else ''
        kind_ok = all(k in sd_sql for k in ('bca_pdf', 'bni_pdf'))

        if sd and not kind_ok and not has_old:
            # normal upgrade path: rename-rebuild preserves ids for FKs
            conn.execute('ALTER TABLE source_documents RENAME TO '
                         'source_documents__old')
            conn.execute(_SOURCE_DOC_CREATE)
            conn.execute('INSERT INTO source_documents SELECT * '
                         'FROM source_documents__old')
            conn.execute('DROP TABLE source_documents__old')
        elif not sd and has_old:
            # crashed mid-rename: source_documents is missing, recreate it
            conn.execute(_SOURCE_DOC_CREATE)
            conn.execute('INSERT INTO source_documents SELECT * '
                         'FROM source_documents__old')
            conn.execute('DROP TABLE source_documents__old')
        elif sd and kind_ok and has_old:
            # leftover old table from a partial run: drop it
            conn.execute('DROP TABLE source_documents__old')

        _repair_dangling_fks(conn)
        conn.commit()
    finally:
        conn.execute('PRAGMA foreign_keys=ON')


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply idempotent column migrations for pre-existing databases."""
    cols = {r['name'] for r in conn.execute("PRAGMA table_info(extracted_txns)").fetchall()}
    if not cols:
        return
    if 'raw_description' not in cols:
        conn.execute("ALTER TABLE extracted_txns ADD COLUMN raw_description "
                     "TEXT NOT NULL DEFAULT ''")
    if 'transaction_type' not in cols:
        conn.execute("ALTER TABLE extracted_txns ADD COLUMN transaction_type "
                     "TEXT NOT NULL DEFAULT ''")
    _migrate_source_documents_kind(conn)
    if cols and 'transaction_type' in cols and 'raw_description' in cols:
        conn.execute(
            "INSERT OR REPLACE INTO _meta(key, value) VALUES (?, ?)",
            ('schema_version', str(SCHEMA_VERSION)),
        )


def ensure_tables(conn: sqlite3.Connection) -> None:
    """Create all tables if they do not already exist (idempotent)."""
    conn.executescript(_DDL)
    _migrate(conn)
    conn.execute(
        "INSERT OR REPLACE INTO _meta(key, value) VALUES (?, ?)",
        ('schema_version', str(SCHEMA_VERSION)),
    )
    conn.commit()
