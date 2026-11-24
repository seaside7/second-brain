"""Editing a split row in All Txns must change that allocation.

list_ledger shows every allocation of a split parent as its own row (with the
allocation's category and note) and hides the parent. The row's id is the
parent's ledger id, so an edit sent to the ledger row changes something the
list never displays: the value reverted on refresh. update_split edits the
allocation itself.

Run with:  python -m unittest tests.test_split_edit -v
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

_SKILL = Path(__file__).resolve().parent.parent / '.agent' / 'skills' / 'transactions' / 'scripts'
if str(_SKILL) not in sys.path:
    sys.path.insert(0, str(_SKILL))

import schema  # noqa: E402
import splits  # noqa: E402
import store   # noqa: E402


class TestUpdateSplit(unittest.TestCase):

    def setUp(self):
        c = self.conn = sqlite3.connect(':memory:')
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        schema.ensure_tables(c)
        c.execute("INSERT INTO source_documents(kind, source_key, fingerprint) VALUES ('manual','k','f')")
        c.execute("INSERT INTO import_batches(source_document_id, state) VALUES (1,'committed')")
        # Some of these names are seeded by ensure_tables, so look up before inserting.
        def cat_id(name):
            r = c.execute('SELECT id FROM categories WHERE name=?', (name,)).fetchone()
            return r[0] if r else c.execute(
                'INSERT INTO categories(name,"group") VALUES (?,?)', (name, 'T')).lastrowid
        self.cat = {n: cat_id(n) for n in ('Groceries', 'Credit Card', 'Adeeva')}
        ext = c.execute(
            "INSERT INTO extracted_txns(doc_id,batch_id,ext_index,provider,src_txn_id,description,"
            "recipient,direction,principal_amount,total_amount,occurred_at) "
            "VALUES (1,1,0,'bca','S1','Transfer - Dinda',?, 'out',500,500,'2026-11-20T14:19:00')",
            ('DINDA',)).lastrowid
        self.parent = c.execute(
            "INSERT INTO ledger_txns(ext_id,amount,direction,nature,review_status) "
            "VALUES (?,500,'out','expense','ok')", (ext,)).lastrowid
        self.s350 = store.add_split(c, parent_ledger_id=self.parent,
                                    category_id=self.cat['Groceries'], amount=350)
        self.s150 = store.add_split(c, parent_ledger_id=self.parent,
                                    category_id=self.cat['Credit Card'], amount=150)
        c.commit()

    def tearDown(self):
        self.conn.close()

    def rows(self):
        return {r['split_id']: r for r in store.list_ledger(self.conn, limit=50)
                if r['row_kind'] == 'split'}

    def test_the_bug_editing_the_parent_does_not_change_the_split_rows(self):
        store.update_ledger(self.conn, self.parent, category_id=self.cat['Adeeva'], notes='Bu Yayah')
        row = self.rows()[self.s350]
        self.assertEqual(row['category_name'], 'Groceries')       # what the owner saw revert
        self.assertEqual(row['notes'] or '', '')

    def test_update_split_changes_the_row_the_list_shows(self):
        res = splits.update_split(self.conn, split_id=self.s350,
                                  category_id=self.cat['Adeeva'], notes='Bu Yayah')
        self.assertTrue(res['ok'], res)
        row = self.rows()[self.s350]
        self.assertEqual(row['category_name'], 'Adeeva')
        self.assertEqual(row['notes'], 'Bu Yayah')
        self.assertEqual(self.rows()[self.s150]['category_name'], 'Credit Card')   # sibling untouched

    def test_notes_only_and_category_only(self):
        splits.update_split(self.conn, split_id=self.s150, notes='mega')
        self.assertEqual(self.rows()[self.s150]['notes'], 'mega')
        self.assertEqual(self.rows()[self.s150]['category_name'], 'Credit Card')
        splits.update_split(self.conn, split_id=self.s150, category_id=self.cat['Adeeva'])
        self.assertEqual(self.rows()[self.s150]['category_name'], 'Adeeva')
        self.assertEqual(self.rows()[self.s150]['notes'], 'mega')

    def test_amounts_and_the_parent_are_unchanged(self):
        splits.update_split(self.conn, split_id=self.s350, category_id=self.cat['Adeeva'])
        self.assertEqual(sorted(r['amount'] for r in self.rows().values()), [150, 350])
        self.assertEqual(store.get_ledger(self.conn, self.parent)['amount'], 500)

    def test_moving_onto_a_sibling_category_is_refused(self):
        res = splits.update_split(self.conn, split_id=self.s350, category_id=self.cat['Credit Card'])
        self.assertFalse(res['ok'])
        self.assertIn('already uses', res['error'])
        self.assertEqual(self.rows()[self.s350]['category_name'], 'Groceries')

    def test_invalid_input_is_rejected(self):
        self.assertFalse(splits.update_split(self.conn, split_id=9999, notes='x')['ok'])
        self.assertFalse(splits.update_split(self.conn, split_id=self.s350, category_id=None)['ok'])
        self.assertFalse(splits.update_split(self.conn, split_id=self.s350, category_id=424242)['ok'])

    def test_the_edit_is_audited(self):
        splits.update_split(self.conn, split_id=self.s350, notes='hi')
        n = self.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='split_edited'").fetchone()[0]
        self.assertEqual(n, 1)


if __name__ == '__main__':
    unittest.main()
