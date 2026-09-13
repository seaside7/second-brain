"""DAL (Data Access Layer) for the Transactions feature.

Thin wrappers over the SQLite connection.  Every function takes an explicit
``conn`` (from ``schema.connect``) so there is no hidden global state.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

import schema

# ── helpers ──────────────────────────────────────────────────────────────

def _now_wib() -> str:
    return datetime.now().strftime('%Y-%m-%dT%H:%M:%S')

# ── accounts ─────────────────────────────────────────────────────────────

def list_accounts(conn: sqlite3.Connection, *, active_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM accounts"
    if active_only:
        sql += " WHERE active=1"
    sql += " ORDER BY provider, alias"
    return [dict(r) for r in conn.execute(sql).fetchall()]

def get_account(conn: sqlite3.Connection, account_id: int) -> Optional[dict]:
    r = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    return dict(r) if r else None

def add_account(conn: sqlite3.Connection, *, type: str, provider: str, alias: str,
                masked: str = '', owner_name: str = '', currency: str = 'IDR') -> int:
    cur = conn.execute(
        "INSERT INTO accounts(type,provider,alias,masked,owner_name,currency) "
        "VALUES(?,?,?,?,?,?)",
        (type, provider, alias, masked, owner_name, currency))
    conn.commit()
    return cur.lastrowid

def update_account(conn: sqlite3.Connection, account_id: int, **fields) -> bool:
    if not fields:
        return False
    allowed = {'type','provider','alias','masked','owner_name','currency','active'}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return False
    sets['updated_at'] = _now_wib()
    conn.execute(
        f"UPDATE accounts SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
        (*sets.values(), account_id))
    conn.commit()
    return conn.total_changes > 0

# ── source_documents ─────────────────────────────────────────────────────

def add_source_document(conn: sqlite3.Connection, *,
                         kind: str, source_key: str, fingerprint: str,
                         provider: str = '', parser_version: str = '',
                         email_sender: str = '', email_subject: str = '',
                         email_received_at: str = '', preview: str = '',
                         upload_path: str = '') -> int:
    cur = conn.execute(
        "INSERT INTO source_documents"
        "(kind,source_key,fingerprint,provider,parser_version,"
        "email_sender,email_subject,email_received_at,preview,upload_path) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (kind, source_key, fingerprint, provider, parser_version,
         email_sender, email_subject, email_received_at, preview, upload_path))
    conn.commit()
    return cur.lastrowid

def update_source_document(conn: sqlite3.Connection, doc_id: int, **fields) -> bool:
    allowed = {'status','error','parser_version','preview'}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return False
    conn.execute(
        f"UPDATE source_documents SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
        (*sets.values(), doc_id))
    conn.commit()
    return True

def get_source_document(conn: sqlite3.Connection, doc_id: int) -> Optional[dict]:
    r = conn.execute("SELECT * FROM source_documents WHERE id=?", (doc_id,)).fetchone()
    return dict(r) if r else None

def source_doc_exists(conn: sqlite3.Connection, source_key: str = '', fingerprint: str = '') -> Optional[int]:
    """Return doc id if source_key or fingerprint already stored, else None."""
    if source_key:
        r = conn.execute("SELECT id FROM source_documents WHERE source_key=?", (source_key,)).fetchone()
        if r:
            return r[0]
    if fingerprint:
        r = conn.execute("SELECT id FROM source_documents WHERE fingerprint=?", (fingerprint,)).fetchone()
        if r:
            return r[0]
    return None

# ── import_batches ───────────────────────────────────────────────────────

def add_import_batch(conn: sqlite3.Connection, doc_id: int) -> int:
    cur = conn.execute(
        "INSERT INTO import_batches(source_document_id) VALUES(?)", (doc_id,))
    conn.commit()
    return cur.lastrowid

def update_import_batch(conn: sqlite3.Connection, batch_id: int, **fields) -> bool:
    allowed = {'state','stats_json'}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return False
    conn.execute(
        f"UPDATE import_batches SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
        (*sets.values(), batch_id))
    conn.commit()
    return True

def get_import_batch(conn: sqlite3.Connection, batch_id: int) -> Optional[dict]:
    r = conn.execute(
        "SELECT b.*, sd.fingerprint, sd.provider, sd.kind, sd.status as doc_status "
        "FROM import_batches b JOIN source_documents sd ON sd.id=b.source_document_id "
        "WHERE b.id=?", (batch_id,)).fetchone()
    return dict(r) if r else None

def list_import_batches(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT b.*, sd.provider, sd.kind, sd.status as doc_status "
        "FROM import_batches b JOIN source_documents sd ON sd.id=b.source_document_id "
        "ORDER BY b.created_at DESC LIMIT ?", (limit,)).fetchall()]

# ── extracted_txns ───────────────────────────────────────────────────────

def add_extracted_rows(conn: sqlite3.Connection, batch_id: int, doc_id: int,
                       rows: list[dict]) -> list[int]:
    """Insert extracted rows inside a transaction; returns list of ids."""
    ids = []
    for i, row in enumerate(rows):
        ext = row.get('ext_index')
        if ext is None:
            ext = i
        cur = conn.execute(
            "INSERT INTO extracted_txns"
            "(doc_id,batch_id,ext_index,provider,src_txn_id,description,"
            "raw_description,transaction_type,merchant,"
            "recipient,phone_suffix,direction,principal_amount,fee_amount,total_amount,"
            "currency,occurred_at,bank_ref,source_page,raw1,raw2,parser_version,"
            "dup_status,duplicate_of) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (doc_id, batch_id, ext,
             row.get('provider',''), row.get('src_txn_id',''),
             row.get('description',''), row.get('raw_description',''),
             row.get('transaction_type',''), row.get('merchant',''),
             row.get('recipient',''), row.get('phone_suffix',''),
             row.get('direction','out'),
             row.get('principal_amount', 0), row.get('fee_amount', 0),
             row.get('total_amount', 0), row.get('currency','IDR'),
             row.get('occurred_at',''), row.get('bank_ref',''),
             row.get('source_page', 0),
             row.get('raw1',''), row.get('raw2',''),
             row.get('parser_version',''),
             row.get('dup_status','unique'), row.get('duplicate_of')))
        ids.append(cur.lastrowid)
    conn.commit()
    return ids

def update_extracted(conn: sqlite3.Connection, ext_id: int, **fields) -> bool:
    """Update a normalised extracted row in place (reprocess path). Frozen
    identity fields (doc_id, batch_id, src_txn_id, ext_index) are not editable."""
    allowed = {'provider','description','raw_description','transaction_type',
               'merchant','recipient','phone_suffix','direction',
               'principal_amount','fee_amount','total_amount','currency',
               'occurred_at','bank_ref','source_page','raw1','raw2',
               'parser_version','dup_status'}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return False
    conn.execute(
        f"UPDATE extracted_txns SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
        (*sets.values(), ext_id))
    conn.commit()
    return True

def list_extracted_by_batch(conn: sqlite3.Connection, batch_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM extracted_txns WHERE batch_id=? ORDER BY ext_index",
        (batch_id,)).fetchall()]

def update_extracted_dup(conn: sqlite3.Connection, ext_id: int,
                          dup_status: str, duplicate_of: int | None = None) -> None:
    conn.execute(
        "UPDATE extracted_txns SET dup_status=?, duplicate_of=? WHERE id=?",
        (dup_status, duplicate_of, ext_id))

# ── ledger_txns ──────────────────────────────────────────────────────────

def add_ledger_row(conn: sqlite3.Connection, ext_id: int, *,
                    account_id: int | None = None,
                    amount: int = 0, direction: str = 'out',
                    nature: str = 'needs_review',
                    category_id: int | None = None,
                    notes: str = '', confidence: str = 'none',
                    confidence_reason: str = '',
                    evidence_json: str = '{}',
                    review_status: str = 'ok',
                    txn_status: str = 'confirmed') -> int:
    cur = conn.execute(
        "INSERT INTO ledger_txns"
        "(ext_id,account_id,amount,direction,nature,category_id,notes,"
        "confidence,confidence_reason,evidence_json,review_status,txn_status) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (ext_id, account_id, amount, direction, nature, category_id,
         notes, confidence, confidence_reason, evidence_json,
         review_status, txn_status))
    conn.commit()
    return cur.lastrowid

def add_ledger_rows_batch(conn: sqlite3.Connection, rows: list[tuple]) -> list[int]:
    """Bulk insert ledger rows. Each tuple: (ext_id, account_id, amount,
    direction, nature, category_id, notes, confidence, confidence_reason,
    evidence_json, review_status, txn_status)."""
    ids = []
    for r in rows:
        cur = conn.execute(
            "INSERT INTO ledger_txns"
            "(ext_id,account_id,amount,direction,nature,category_id,notes,"
            "confidence,confidence_reason,evidence_json,review_status,txn_status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", r)
        ids.append(cur.lastrowid)
    conn.commit()
    return ids

def get_ledger(conn: sqlite3.Connection, ledger_id: int) -> Optional[dict]:
    r = conn.execute(
        "SELECT l.*, a.alias as account_alias, a.masked as account_masked, "
        "a.provider as account_provider "
        "FROM ledger_txns l LEFT JOIN accounts a ON a.id=l.account_id "
        "WHERE l.id=?", (ledger_id,)).fetchone()
    return dict(r) if r else None

def update_ledger(conn: sqlite3.Connection, ledger_id: int, **fields) -> bool:
    allowed = {'account_id','amount','direction','nature','category_id',
               'notes','confidence','confidence_reason','review_status',
               'txn_status','evidence_json'}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return False
    sets['updated_at'] = _now_wib()
    conn.execute(
        f"UPDATE ledger_txns SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
        (*sets.values(), ledger_id))
    conn.commit()
    return True


def has_manual_correction(conn: sqlite3.Connection, ledger_id: int,
                          fields: tuple[str, ...] = ('category_id', 'nature')) -> bool:
    """True if a user has manually corrected any of the given fields on this
    ledger row. Reprocess must never overwrite those fields."""
    marks = ', '.join('?' for _ in fields)
    r = conn.execute(
        f"SELECT COUNT(*) FROM corrections WHERE ledger_id=? AND field IN ({marks})",
        (ledger_id, *fields)).fetchone()
    return r[0] > 0


def add_ledger_row_ext(conn: sqlite3.Connection, ext_id: int, *,
                       account_id: int | None = None,
                       amount: int = 0, direction: str = 'out',
                       nature: str = 'needs_review',
                       category_id: int | None = None,
                       notes: str = '', confidence: str = 'none',
                       confidence_reason: str = '',
                       evidence_json: str = '{}',
                       review_status: str = 'ok',
                       txn_status: str = 'confirmed',
                       parse_conf_src: str = '') -> int:
    """Like add_ledger_row but wraps in a caller-managed transaction so a
    reprocess batch (principal row + optional fee row) is atomic."""
    return add_ledger_row(conn, ext_id,
        account_id=account_id, amount=amount, direction=direction,
        nature=nature, category_id=category_id, notes=notes,
        confidence=confidence, confidence_reason=confidence_reason,
        evidence_json=evidence_json, review_status=review_status,
        txn_status=txn_status)

def list_ledger(conn: sqlite3.Connection, *,
                nature: str | None = None,
                review_status: str | None = None,
                txn_status: str | None = None,
                account_id: int | None = None,
                category_id: int | None = None,
                from_date: str | None = None,
                to_date: str | None = None,
                search: str | None = None,
                limit: int = 200,
                offset: int = 0) -> list[dict]:
    conds, params = [], []
    if nature:
        conds.append("l.nature=?"); params.append(nature)
    if review_status:
        conds.append("l.review_status=?"); params.append(review_status)
    if txn_status:
        conds.append("l.txn_status=?"); params.append(txn_status)
    if account_id:
        conds.append("l.account_id=?"); params.append(account_id)
    if category_id:
        conds.append("l.category_id=?"); params.append(category_id)
    if from_date:
        conds.append("e.occurred_at>=?"); params.append(from_date)
    if to_date:
        conds.append("e.occurred_at<=?"); params.append(to_date)
    if search:
        conds.append("(l.notes LIKE ? OR e.description LIKE ? OR e.merchant LIKE ?)")
        s = f'%{search}%'; params.extend([s, s, s])
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    sql = (
        "SELECT l.*, a.alias as account_alias, a.masked as account_masked, "
        "c.name as category_name, c.\"group\" as category_group, "
        "e.description, e.raw_description, e.transaction_type, e.merchant, "
        "e.recipient, e.src_txn_id, e.provider, "
        "e.phone_suffix, e.bank_ref, e.occurred_at, "
        "s.email_subject "
        "FROM ledger_txns l "
        "LEFT JOIN accounts a ON a.id=l.account_id "
        "LEFT JOIN categories c ON c.id=l.category_id "
        "LEFT JOIN extracted_txns e ON e.id=l.ext_id "
        "LEFT JOIN source_documents s ON s.id=e.doc_id "
        f"{where} ORDER BY e.occurred_at DESC, l.created_at DESC LIMIT ? OFFSET ?")
    params.extend([limit, offset])
    return [dict(r) for r in conn.execute(sql, params).fetchall()]

def count_ledger(conn: sqlite3.Connection, *,
                 txn_status: str | None = None,
                 review_status: str | None = None,
                 category_id: int | None = None,
                 from_date: str | None = None,
                 to_date: str | None = None) -> int:
    conds, params = [], []
    if txn_status:
        conds.append("l.txn_status=?"); params.append(txn_status)
    if review_status:
        conds.append("l.review_status=?"); params.append(review_status)
    if category_id:
        conds.append("l.category_id=?"); params.append(category_id)
    if from_date:
        conds.append("e.occurred_at>=?"); params.append(from_date)
    if to_date:
        conds.append("e.occurred_at<=?"); params.append(to_date)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    join = ""
    if from_date or to_date:
        join = " JOIN extracted_txns e ON e.id=l.ext_id"
    r = conn.execute(f"SELECT COUNT(*) FROM ledger_txns l{join} {where}", params).fetchone()
    return r[0] if r else 0

# ── transfers ────────────────────────────────────────────────────────────

def add_transfer(conn: sqlite3.Connection, *,
                  from_ledger_id: int, to_ledger_id: int,
                  principal_amount: int = 0,
                  fee_ledger_id: int | None = None,
                  status: str = 'unmatched',
                  matched_by: str = '', score: float = 0.0) -> int:
    cur = conn.execute(
        "INSERT INTO transfers"
        "(from_ledger_id,to_ledger_id,principal_amount,fee_ledger_id,"
        "status,matched_by,score) VALUES(?,?,?,?,?,?,?)",
        (from_ledger_id, to_ledger_id, principal_amount, fee_ledger_id,
         status, matched_by, score))
    conn.commit()
    return cur.lastrowid

def update_transfer(conn: sqlite3.Connection, transfer_id: int, **fields) -> bool:
    allowed = {'status','score','matched_by'}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return False
    conn.execute(
        f"UPDATE transfers SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
        (*sets.values(), transfer_id))
    conn.commit()
    return True

def list_transfers(conn: sqlite3.Connection, *,
                   status: str | None = None,
                   limit: int = 100) -> list[dict]:
    conds, params = [], []
    if status:
        conds.append("t.status=?"); params.append(status)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    return [dict(r) for r in conn.execute(
        f"SELECT t.*, "
        "  fl.amount as from_amount, fl.nature as from_nature, "
        "  tl.amount as to_amount, tl.nature as to_nature, "
        "  ae.description as from_desc, ae.merchant as from_merchant, "
        "  te.description as to_desc, te.merchant as to_merchant "
        "FROM transfers t "
        "LEFT JOIN ledger_txns fl ON fl.id=t.from_ledger_id "
        "LEFT JOIN ledger_txns tl ON tl.id=t.to_ledger_id "
        "LEFT JOIN extracted_txns ae ON ae.id=fl.ext_id "
        "LEFT JOIN extracted_txns te ON te.id=tl.ext_id "
        f"{where} ORDER BY t.created_at DESC LIMIT ?",
        [*params, limit]).fetchall()]

def get_transfer_for_ledger(conn: sqlite3.Connection, ledger_id: int) -> Optional[dict]:
    r = conn.execute(
        "SELECT * FROM transfers WHERE from_ledger_id=? OR to_ledger_id=?",
        (ledger_id, ledger_id)).fetchone()
    return dict(r) if r else None

def delete_transfer(conn: sqlite3.Connection, transfer_id: int) -> bool:
    conn.execute("DELETE FROM transfers WHERE id=?", (transfer_id,))
    conn.commit()
    return conn.total_changes > 0

# ── categories + rules ──────────────────────────────────────────────────

def list_categories(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM categories ORDER BY builtin DESC, name").fetchall()]

def add_category(conn: sqlite3.Connection, name: str,
                  group: str = '', builtin: int = 0) -> int:
    cur = conn.execute(
        "INSERT OR IGNORE INTO categories(name,\"group\",builtin) VALUES(?,?,?)",
        (name, group, builtin))
    conn.commit()
    return cur.lastrowid

def find_category(conn: sqlite3.Connection, name: str) -> Optional[dict]:
    r = conn.execute("SELECT * FROM categories WHERE name=?", (name,)).fetchone()
    return dict(r) if r else None

def get_category(conn: sqlite3.Connection, category_id: int) -> Optional[dict]:
    r = conn.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone()
    return dict(r) if r else None

def nature_for_category(conn: sqlite3.Connection, current_nature: str,
                        category_id: int | None) -> str:
    """Nature a row should carry after the owner assigns category_id.

    A person-transfer the owner files into real spend becomes an expense
    (Transfers-group categories and Uncategorized keep it a transfer).
    Every other nature is untouched.
    """
    if current_nature != 'transfer_to_person' or not category_id:
        return current_nature
    cat = get_category(conn, category_id)
    if not cat:
        return current_nature
    if (cat.get('group') or '') == 'Transfers' or cat.get('name') == 'Uncategorized':
        return current_nature
    return 'expense'

def get_or_create_category(conn: sqlite3.Connection, name: str,
                            group: str = '') -> int:
    c = find_category(conn, name)
    if c:
        return c['id']
    return add_category(conn, name, group=group)

def list_rules(conn: sqlite3.Connection, *, active_only: bool = True) -> list[dict]:
    sql = "SELECT r.*, c.name as category_name FROM category_rules r " \
          "LEFT JOIN categories c ON c.id=r.category_id"
    if active_only:
        sql += " WHERE r.active=1"
    sql += " ORDER BY r.times_applied DESC"
    return [dict(r) for r in conn.execute(sql).fetchall()]

def add_rule(conn: sqlite3.Connection, *,
             merchant_or_recipient: str, category_id: int,
             nature: str = 'expense', source: str = 'manual',
             confidence: float = 1.0) -> int:
    cur = conn.execute(
        "INSERT INTO category_rules"
        "(merchant_or_recipient,category_id,nature,source,confidence) "
        "VALUES(?,?,?,?,?)",
        (merchant_or_recipient, category_id, nature, source, confidence))
    conn.commit()
    return cur.lastrowid

def deactivate_rule(conn: sqlite3.Connection, rule_id: int) -> bool:
    conn.execute("UPDATE category_rules SET active=0 WHERE id=?", (rule_id,))
    conn.commit()
    return True

def increment_rule_usage(conn: sqlite3.Connection, rule_id: int) -> None:
    conn.execute(
        "UPDATE category_rules SET times_applied=times_applied+1 WHERE id=?",
        (rule_id,))
    conn.commit()

# ── corrections + audit ─────────────────────────────────────────────────

def add_correction(conn: sqlite3.Connection, *,
                    ledger_id: int, field: str,
                    before_val: str, after_val: str,
                    reason: str = '', actor: str = 'user') -> int:
    cur = conn.execute(
        "INSERT INTO corrections(ledger_id,field,before_val,after_val,reason,actor) "
        "VALUES(?,?,?,?,?,?)",
        (ledger_id, field, before_val, after_val, reason, actor))
    conn.commit()
    return cur.lastrowid

def add_audit(conn: sqlite3.Connection, *,
              action: str, entity: str = '', entity_id: int | None = None,
              before_json: str = '{}', after_json: str = '{}',
              actor: str = 'user', source: str = 'web') -> int:
    cur = conn.execute(
        "INSERT INTO audit_log(action,entity,entity_id,before_json,after_json,actor,source) "
        "VALUES(?,?,?,?,?,?,?)",
        (action, entity, entity_id, before_json, after_json, actor, source))
    conn.commit()
    return cur.lastrowid

def list_audit(conn: sqlite3.Connection, *,
               entity: str | None = None,
               limit: int = 100) -> list[dict]:
    conds, params = [], []
    if entity:
        conds.append("entity=?"); params.append(entity)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    return [dict(r) for r in conn.execute(
        f"SELECT * FROM audit_log {where} ORDER BY ts DESC LIMIT ?",
        [*params, limit]).fetchall()]

# ── sync_state ───────────────────────────────────────────────────────────

def get_sync_state(conn: sqlite3.Connection, key: str) -> str:
    r = conn.execute("SELECT value FROM sync_state WHERE key=?", (key,)).fetchone()
    return r[0] if r else ''

def set_sync_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO sync_state(key,value) VALUES(?,?)",
                 (key, value))
    conn.commit()

# ── reports ──────────────────────────────────────────────────────────────

def spending_summary(conn: sqlite3.Connection, *,
                     from_date: str | None = None,
                     to_date: str | None = None) -> dict:
    """Return {total_expense, total_fee, total_income, total_refund,
    total_cashback, total_transfer_to_person, total_internal,
    total_top_up, count, by_category:[{id,name,count,total}], 
    by_account:[{alias,total}]}."""
    conds = ["l.nature NOT IN ('void')"]
    params: list[Any] = []
    if from_date:
        conds.append("e.occurred_at>=?"); params.append(from_date)
    if to_date:
        conds.append("e.occurred_at<=?"); params.append(to_date)
    where = "WHERE " + " AND ".join(conds)

    # Totals by nature
    sql = f"SELECT l.nature, SUM(l.amount) as total, COUNT(*) as cnt " \
          "FROM ledger_txns l " \
          "LEFT JOIN extracted_txns e ON e.id=l.ext_id " \
          f"{where} GROUP BY l.nature"
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    by_nature = {r['nature']: {'total': r['total'], 'count': r['cnt']} for r in rows}

    # By category
    cat_sql = f"SELECT c.id, c.name, COUNT(*) as cnt, SUM(l.amount) as total " \
              "FROM ledger_txns l " \
              "LEFT JOIN extracted_txns e ON e.id=l.ext_id " \
              "LEFT JOIN categories c ON c.id=l.category_id " \
              f"{where} AND l.nature='expense' GROUP BY l.category_id"
    by_cat = [dict(r) for r in conn.execute(cat_sql, params).fetchall()]

    # By account
    acc_sql = f"SELECT a.alias, SUM(l.amount) as total " \
              "FROM ledger_txns l " \
              "LEFT JOIN extracted_txns e ON e.id=l.ext_id " \
              "LEFT JOIN accounts a ON a.id=l.account_id " \
              f"{where} GROUP BY l.account_id"
    by_acc = [dict(r) for r in conn.execute(acc_sql, params).fetchall()]

    return {
        'expense': by_nature.get('expense', {}).get('total', 0),
        'fee': by_nature.get('fee', {}).get('total', 0),
        'income': by_nature.get('income', {}).get('total', 0),
        'refund': by_nature.get('refund', {}).get('total', 0),
        'cashback': by_nature.get('cashback', {}).get('total', 0),
        'transfer_to_person': by_nature.get('transfer_to_person', {}).get('total', 0),
        'internal_transfer': by_nature.get('internal_transfer', {}).get('total', 0),
        'top_up': by_nature.get('top_up', {}).get('total', 0),
        'total_count': sum(r['cnt'] for r in rows),
        'by_category': by_cat,
        'by_account': by_acc,
    }

# ── idr helpers ──────────────────────────────────────────────────────────

def fmt_idr(amount: int) -> str:
    """Format amount in IDR with thousand separators."""
    return f"Rp{amount:,.0f}".replace(',', '.')
