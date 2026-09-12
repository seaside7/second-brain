"""Category taxonomy migration for the transactions skill.

Creates the canonical two-level taxonomy (group > name) from the categorizer's
_TAXONOMY, backs up the database, and (optionally) prunes orphaned legacy
flat categories that no longer have any referencing rows.

Usage:
    python migrate_taxonomy.py                 # create taxonomy + backup
    python migrate_taxonomy.py --prune-orphans # also drop unused legacy cats

Safe to run repeatedly: get_or_create is idempotent and backup is skipped when
the same-day backup file already exists.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import schema
import store
from categorize import _CAT


def backup(db_path: Path, state_dir: Path) -> Path:
    """Copy the DB to a timestamped backup (skip if today's already exists)."""
    stamp = datetime.now().strftime('%Y-%m-%d')
    b = state_dir / f'transactions.db.taxonomy-backup-{stamp}.bak'
    if b.exists():
        return b
    shutil.copy2(db_path, b)
    return b


def ensure_taxonomy(conn: sqlite3.Connection) -> list[dict]:
    """Create the canonical taxonomy rows; return the rows created."""
    before = {r['id'] for r in store.list_categories(conn)}
    created = []
    for key, (group, name) in sorted(_CAT.items()):
        cat_id = store.get_or_create_category(conn, name, group=group)
        if cat_id not in before:
            created.append({'key': key, 'id': cat_id, 'group': group, 'name': name})
    return created


def prune_orphans(conn: sqlite3.Connection, dry_run: bool = True) -> list[int]:
    """Delete legacy flat categories (group='') referenced by nothing."""
    rows = conn.execute(
        "SELECT id, name FROM categories WHERE \"group\"='' ORDER BY id").fetchall()
    orphans = []
    for cat_id, name in rows:
        used = conn.execute(
            "SELECT (SELECT COUNT(*) FROM ledger_txns WHERE category_id=?) + "
            "(SELECT COUNT(*) FROM category_rules WHERE category_id=?)",
            (cat_id, cat_id)).fetchone()[0]
        if used == 0:
            if not dry_run:
                conn.execute("DELETE FROM categories WHERE id=?", (cat_id,))
            orphans.append(cat_id)
    if not dry_run:
        conn.commit()
    return orphans


def main() -> None:
    prune = '--prune-orphans' in sys.argv
    db_path = Path(schema.DB_PATH)
    state_dir = db_path.parent

    backup_path = backup(db_path, state_dir)
    print(f'backup: {backup_path}')

    conn = schema.connect(str(db_path))
    schema.ensure_tables(conn)
    created = ensure_taxonomy(conn)
    print(f'taxonomy rows created: {len(created)}')
    for c in created:
        print(f"  id={c['id']:<3} {c['group']} > {c['name']}")

    if prune:
        orphans = prune_orphans(conn, dry_run=False)
        print(f'pruned orphaned legacy categories: {len(orphans)}')
        print('  ids:', orphans)
    conn.close()

    print('done')


if __name__ == '__main__':
    main()