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


class TestMergeSameCategory(unittest.TestCase):
    """splits/set tolerates a receipt with several lines in one category."""

    def setUp(self):
        _dash = str(Path(__file__).resolve().parent.parent / 'dashboard')
        if _dash not in sys.path:
            sys.path.insert(0, _dash)
        import transactions_api
        self.merge = transactions_api._merge_same_category

    def test_the_payload_from_the_owner_report_merges_and_keeps_the_total(self):
        out = self.merge([
            {'category_id': 4437, 'amount': 138500, 'notes': 'udang dan saus pasta'},
            {'category_id': 266, 'amount': 32000, 'notes': 'warung gandaria'},
            {'category_id': 4437, 'amount': 282000, 'notes': 'akumulasi bln lalu'}])
        self.assertEqual(len(out), 2)
        self.assertEqual(sum(a['amount'] for a in out), 452500)
        food = next(a for a in out if a['category_id'] == 4437)
        self.assertEqual(food['amount'], 420500)
        self.assertEqual(food['notes'], 'udang dan saus pasta; akumulasi bln lalu')

    def test_distinct_categories_and_trips_are_left_alone(self):
        rows = [{'category_id': 1, 'amount': 5, 'notes': 'a'},
                {'category_id': 2, 'amount': 6, 'notes': ''},
                {'category_id': 1, 'trip_id': 9, 'amount': 7, 'notes': 'b'}]
        self.assertEqual(len(self.merge(rows)), 3)

    def test_malformed_lines_are_passed_through_for_set_splits_to_reject(self):
        bad = [{'category_id': 1, 'amount': 'abc'}]
        self.assertEqual(self.merge(bad), bad)
        self.assertEqual(self.merge(['x']), ['x'])

    def test_set_splits_itself_still_rejects_duplicates(self):
        c = sqlite3.connect(':memory:')
        c.row_factory = sqlite3.Row
        schema.ensure_tables(c)
        c.execute("INSERT INTO source_documents(kind, source_key, fingerprint) VALUES ('manual','k','f')")
        c.execute("INSERT INTO import_batches(source_document_id, state) VALUES (1,'committed')")
        cid = c.execute('INSERT INTO categories(name,"group") VALUES (?,?)', ('Zed', 'T')).lastrowid
        ext = c.execute("INSERT INTO extracted_txns(doc_id,batch_id,ext_index,provider,src_txn_id,description,"
                        "direction,principal_amount,total_amount,occurred_at) "
                        "VALUES (1,1,0,'bca','S1','x','out',10,10,'2026-11-20T10:00:00')").lastrowid
        lid = c.execute("INSERT INTO ledger_txns(ext_id,amount,direction,nature) VALUES (?,10,'out','expense')",
                        (ext,)).lastrowid
        c.commit()
        res = splits.set_splits(c, ledger_id=lid, allocations=[
            {'category_id': cid, 'amount': 4}, {'category_id': cid, 'amount': 6}])
        self.assertFalse(res['ok'])
        self.assertIn('Duplicate', res['error'])
        ok = splits.set_splits(c, ledger_id=lid, allocations=self.merge([
            {'category_id': cid, 'amount': 4}, {'category_id': cid, 'amount': 6}]))
        self.assertTrue(ok['ok'], ok)
        c.close()


if __name__ == '__main__':
    unittest.main()
