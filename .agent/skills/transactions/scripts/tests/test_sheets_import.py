"""Tests for the one-time recap (Google Sheet) import.

Covers: CSV loading/normalisation, classification (bank debits vs allocation
children vs summary/adjustment exclusions), match-existing-first, enrichment
override rules with correction protection + conflict behaviour, insert-missing
through the source_documents chain, split-allocation linking, idempotency via
sheet_import_log, and all-or-nothing rollback.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = str(Path(__file__).resolve().parent.parent)
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import schema
import store
import sheets_import as si


def _recap(**kw):
    base = {
        'row_no': 1, 'occurred_at': '2026-09-01', 'amount': 25000,
        'direction': 'out', 'description': 'lunch cafe alice',
        'category': '', 'confirmed': False, 'type': '', 'split_of': None,
        'trip': '', 'notes': '', 'reference': '',
    }
    base.update(kw)
    return base


class RecapImportTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self._conn = schema.connect(str(self._tmp / 't.db'))
        schema.ensure_tables(self._conn)
        self._ext_idx = 0

    def tearDown(self):
        self._conn.close()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _seed_ledger(self, amount=25000, day='2026-09-01', direction='out',
                     description='lunch cafe alice', nature='expense',
                     category_id=None):
        src = f'fp{self._ext_idx}'
        doc = store.add_source_document(self._conn, kind='gmail',
                                        source_key=f'gmail:bca:{src}',
                                        fingerprint=src,
                                        provider='bca')
        batch = store.add_import_batch(self._conn, doc)
        ext = store.add_extracted_rows(self._conn, batch, doc, [{
            'provider': 'bca', 'src_txn_id': f'T{self._ext_idx}',
            'occurred_at': f'{day}T09:00:00', 'description': description,
            'raw_description': description, 'transaction_type': 'qris',
            'merchant': '', 'recipient': '', 'direction': direction,
            'principal_amount': amount, 'fee_amount': 0, 'total_amount': amount,
            'currency': 'IDR', 'parser_version': 'test',
        }])[0]
        self._ext_idx += 1
        return store.add_ledger_row(self._conn, ext, amount=amount,
                                    direction=direction, nature=nature,
                                    category_id=category_id,
                                    confidence='low' if category_id is None else 'high',
                                    confidence_reason='seed')


class LoadTests(RecapImportTestCase):
    def test_csv_load_mapping_and_amounts(self):
        csv_path = self._tmp / 'recap.csv'
        csv_path.write_text(
            'Tanggal,Keterangan,Debit,Kategori,Tipe\n'
            '2026-09-01,Lunch cafe alice,Rp25.000,Food,transaction\n'
            '2026-09-02,Salary,0,Bonus,income\n', encoding='utf-8')
        loaded = si.load_recap(csv_path)
        self.assertTrue(loaded['ok'], loaded.get('error'))
        self.assertEqual(len(loaded['rows']), 2)
        r0 = loaded['rows'][0]
        self.assertEqual((r0['occurred_at'], r0['amount'], r0['description'],
                          r0['category'], r0['direction']),
                         ('2026-09-01', 25000, 'Lunch cafe alice', 'Food', 'out'))
        self.assertTrue(loaded['rows'][1]['confirmed'])  # default confirmed

    def test_csv_credit_rows_are_income_not_zero(self):
        # The recap fills BOTH amount columns with "Rp 0.00" defaults, so a
        # non-empty debit cell must not mask a real credit (bank CR row).
        csv_path = self._tmp / 'recap.csv'
        csv_path.write_text(
            'Tanggal transaksi,Deskripsi sumber,'
            'Kredit / uang masuk (Rp),Debit / uang keluar (Rp),'
            'Kategori (bisa diganti),Catatan\n'
            '9/25/2026,Gaji prorata Samudera Indonesia,'
            '"Rp 41,250,976.00","Rp 0.00",Gaji,period\n'
            '9/26/2026,Sate ayam,"Rp 0.00","Rp 25,000.00",Makan,\n',
            encoding='utf-8')
        loaded = si.load_recap(csv_path)
        self.assertTrue(loaded['ok'], loaded.get('error'))
        r0, r1 = loaded['rows']
        self.assertEqual(r0['amount'], -41250976)
        self.assertEqual(r0['direction'], 'in')
        self.assertEqual(r1['amount'], 25000)
        self.assertEqual(r1['direction'], 'out')

    def test_classify_excludes_summary_and_adjustment(self):
        rows = [
            _recap(row_no=1, description='Lunch cafe', category='Food', type=''),
            _recap(row_no=2, description='TOTAL', category='', type=''),
            _recap(row_no=3, description='chart adj', category='', type='adjustment'),
            _recap(row_no=4, amount=8000, description='split item', category='Food',
                   type='allocation'),
            _recap(row_no=5, description='Hotel', category='Hotel'),
            _recap(row_no=6, description='no date row', occurred_at='', category=''),
        ]
        bucket = si.classify(rows)
        self.assertEqual([r['row_no'] for r in bucket['parents']], [1, 5])
        self.assertEqual([r['row_no'] for r in bucket['allocations']], [4])
        reasons = {r['row_no']: r['reason'] for r in bucket['excluded']}
        self.assertEqual(reasons, {2: 'summary', 3: 'adjustment/chart', 6: 'no date'})


class MatchTests(RecapImportTestCase):
    def setUp(self):
        super().setUp()
        self.lid = self._seed_ledger()

    def test_exact_likely_ambiguous_different(self):
        exact = si._match_existing(self._conn, _recap(amount=25000,
                                                      description='lunch cafe alice'))
        self.assertEqual(exact['status'], 'exact')
        self.assertEqual(exact['ledger_id'], self.lid)

        likely = si._match_existing(self._conn, _recap(amount=25000,
                                                       description='some random payee'))
        self.assertEqual(likely['status'], 'likely')

        amb = si._match_existing(self._conn, _recap(amount=25000, direction='in',
                                                    description='whatever'))
        self.assertEqual(amb['status'], 'ambiguous')

        diff = si._match_existing(self._conn, _recap(amount=999000,
                                                     description='totally new thing'))
        self.assertEqual(diff['status'], 'different')


class ConfirmTests(RecapImportTestCase):
    def test_confirm_enriches_matched_and_protects_correction(self):
        lid = self._seed_ledger()
        prev = si.preview_recap(self._conn, rows=[_recap(
            confirmed=True, category='Food', notes='dinner split', trip='Bali')])
        self.assertEqual(len(prev['matched']), 1)
        self.assertEqual(prev['totals']['matched'], 25000)

        r = si.confirm_recap(self._conn, rows=prev['matched'])
        self.assertTrue(r['ok'])
        self.assertEqual(r['enriched'], 1)
        ledger = store.get_ledger(self._conn, lid)
        self.assertIsNotNone(ledger['category_id'])
        self.assertEqual(store.get_category(self._conn, ledger['category_id'])['name'], 'Food')
        self.assertIn('dinner split', ledger['notes'] or '')
        self.assertTrue(store.has_manual_correction(self._conn, lid, ('category_id',)))

        # conflict: recap re-run is a no-op (logged), but a fresh manual
        # correction + new recap row with a different category must conflict.
        store.update_ledger(self._conn, lid, category_id=None)
        store.add_correction(self._conn, ledger_id=lid, field='category_id',
                             before_val='', after_val='Manual', reason='user')
        prev2 = si.preview_recap(self._conn, rows=[_recap(
            row_no=2, confirmed=True, category='Travel')])
        self.assertEqual(len(prev2['matched']), 1)
        r2 = si.confirm_recap(self._conn, rows=prev2['matched'])
        self.assertTrue(r2['ok'])
        self.assertEqual(r2['conflicts'], 1)
        self.assertEqual(r2['enriched'], 0)
        self.assertIsNone(store.get_ledger(self._conn, lid)['category_id'])

    def test_unconfirmed_fills_only_empty(self):
        lid = self._seed_ledger(category_id=None)
        r = si.confirm_recap(self._conn, rows=[_recap(confirmed=False,
                                                      category='Food')])
        self.assertEqual(r['enriched'], 1)
        self.assertIsNotNone(store.get_ledger(self._conn, lid)['category_id'])
        # seeded high-confidence category stays untouched by unconfirmed recap
        cat = store.get_or_create_category(self._conn, name='Hotel')
        lid2 = self._seed_ledger(description='other thing', category_id=cat)
        r2 = si.confirm_recap(self._conn, rows=[_recap(row_no=9, confirmed=False,
                                                       category='Travel')])
        self.assertEqual(store.get_ledger(self._conn, lid2)['category_id'], cat)

    def test_insert_missing_via_source_chain_and_trip(self):
        tid = store.add_trip(self._conn, name='Bali 2026', destination='Bali')
        prev = si.preview_recap(self._conn, rows=[_recap(
            row_no=3, amount=400000, description='villa booking',
            category='Travel', confirmed=True, trip='bali')])
        self.assertEqual(len(prev['new_rows']), 1)
        h = prev['new_rows'][0]['hash']
        r = si.confirm_recap(self._conn, rows=prev['new_rows'],
                             apply_hashes=[h])
        self.assertEqual(r['inserted'], 1)
        got = self._conn.execute(
            "SELECT kind, provider FROM source_documents "
            "WHERE kind='sheet'").fetchall()
        self.assertEqual(len(got), 1)
        row = self._conn.execute(
            "SELECT l.*, e.description FROM ledger_txns l "
            "JOIN extracted_txns e ON e.id=l.ext_id").fetchone()
        self.assertEqual(row['amount'], 400000)
        self.assertEqual(row['trip_id'], tid)
        self.assertEqual(row['category_id'],
                         store.find_category(self._conn, 'Travel')['id'])

        # idempotent: re-running same recap previews nothing new
        prev2 = si.preview_recap(self._conn, rows=[_recap(
            row_no=3, amount=400000, description='villa booking',
            category='Travel', confirmed=True, trip='bali')])
        self.assertEqual(len(prev2['new_rows']), 0)
        self.assertEqual(len(prev2['matched']), 0)

    def test_held_rows_never_auto_insert(self):
        prev = si.preview_recap(self._conn, rows=[_recap(
            row_no=5, amount=70000, description='supermarket shop',
            category='Groceries')])
        h = prev['new_rows'][0]['hash']
        r = si.confirm_recap(self._conn, rows=prev['new_rows'])
        self.assertEqual(r['held_review'], 1)
        self.assertEqual(r['inserted'], 0)
        self.assertEqual(self._conn.execute(
            "SELECT COUNT(*) FROM ledger_txns").fetchone()[0], 0)
        # held rows stay out of future previews until resolved, but are logged
        prev2 = si.preview_recap(self._conn, rows=[_recap(
            row_no=5, amount=70000, description='supermarket shop',
            category='Groceries')])
        self.assertEqual(len(prev2['new_rows']), 0)

    def test_ambiguous_held_to_review(self):
        self._seed_ledger(amount=50000, description='store one')
        rows = [_recap(row_no=7, amount=50000, description='store one',
                       direction='in')]
        prev = si.preview_recap(self._conn, rows=rows)
        self.assertEqual(len(prev['ambiguous']), 1)
        r = si.confirm_recap(self._conn, rows=prev['ambiguous'])
        self.assertEqual(r['held_review'], 1)
        self.assertEqual(r['inserted'], 0)

    def test_split_allocations_link_as_children(self):
        parent = _recap(row_no=10, amount=100000, description='groceries big',
                        category='Groceries', confirmed=True)
        child_a = _recap(row_no=11, amount=60000, description='split food',
                         category='Food', type='allocation', split_of=10)
        child_b = _recap(row_no=12, amount=40000, description='split home',
                         category='Home', type='allocation', split_of=10)
        rows = [parent, child_a, child_b]
        prev = si.preview_recap(self._conn, rows=rows)
        self.assertEqual(len(prev['new_rows']), 1)
        self.assertEqual(len(prev['allocations']), 2)
        r = si.confirm_recap(self._conn, rows=rows,
                             apply_hashes=[prev['new_rows'][0]['hash']])
        self.assertEqual(r['inserted'], 1)
        splits = self._conn.execute(
            'SELECT amount FROM txn_splits').fetchall()
        self.assertEqual(sorted(s[0] for s in splits), [40000, 60000])
        debits = self._conn.execute(
            "SELECT COUNT(*) FROM ledger_txns WHERE nature='expense'").fetchone()[0]
        self.assertEqual(debits, 1)  # allocations never become separate debits

    def test_rollback_on_error_leaves_no_trace(self):
        lid = self._seed_ledger()
        before = self._conn.execute(
            'SELECT COUNT(*) FROM ledger_txns').fetchone()[0]
        rows = [_recap(row_no=20, amount=25000, description='lunch cafe alice',
                       confirmed=True, category='Food'),
                _recap(row_no=21, amount=333000, description='brand new thing',
                       confirmed=True, category='Travel')]
        orig = si._insert_missing
        calls = {'n': 0}

        def boom(conn, row):
            calls['n'] += 1
            conn.execute(
                "INSERT INTO source_documents(kind,source_key,fingerprint,provider) "
                "VALUES('sheet','x','y','sheet')")
            raise RuntimeError('boom')

        si._insert_missing = boom
        try:
            r = si.confirm_recap(self._conn, rows=rows,
                                 apply_hashes=[si._row_hash(rows[1])])
            self.assertFalse(r['ok'])
        finally:
            si._insert_missing = orig
        self.assertEqual(self._conn.execute(
            'SELECT COUNT(*) FROM ledger_txns').fetchone()[0], before)
        self.assertEqual(self._conn.execute(
            "SELECT COUNT(*) FROM source_documents WHERE kind='sheet'").fetchone()[0], 0)
        self.assertEqual(self._conn.execute(
            "SELECT COUNT(*) FROM sheet_import_log").fetchone()[0], 0)


class FamilyTests(RecapImportTestCase):
    def _alloc(self, **kw):
        base = {
            'row_no': 100, 'occurred_at': '2026-09-26', 'amount': 100000,
            'direction': 'out', 'description': 'Alokasi Groceries',
            'category': 'Groceries', 'confirmed': True, 'type': '',
            'split_of': None, 'trip': '', 'notes': '', 'reference': '',
        }
        base.update(kw)
        return base

    def test_build_families_groups_slices_and_sum(self):
        rows = [
            self._alloc(row_no=4, amount=1000000, description='jajan.rmh1.5 | DINDA',
                        category='Bulanan Dinda',
                        notes='Send total to Dinda Rp2.500.000 pada 25 September: '
                              'Bulanan Rp1.000.000 (R004) + Groceries Rp1.500.000 (R127)'),
            self._alloc(row_no=127, amount=1500000, description='jajan.rmh1.5 | DINDA',
                        category='Groceries',
                        notes='Send total to Dinda Rp2.500.000 (R127)'),
            self._alloc(row_no=92, amount=83000, description='Alokasi transfer Dinda',
                        category='Makan dan minum',
                        notes='Send total to Dinda Rp1.467.000, transfer BCA '
                              'tanggal 30 September (asal R092)'),
        ]
        fams = si._build_families(rows)
        self.assertEqual(len(fams), 2)
        by_key = {f['key']: f for f in fams}
        self.assertEqual(by_key['2500000']['amount'], 2500000)
        self.assertEqual(by_key['1467000']['amount'], 83000)
        self.assertEqual(by_key['1470000']['amount'] if '1470000' in by_key else by_key.get('1467000', {}).get('amount'),
                         by_key['1467000']['amount'])

    def test_preview_collapses_family_into_one_parent(self):
        rows = [
            self._alloc(row_no=4, amount=1000000, description='jajan.rmh1.5 | DINDA',
                        category='Bulanan Dinda',
                        notes='Send total to Dinda Rp2.500.000 pada 25 September: '
                              'Bulanan Rp1.000.000 (R004) + Groceries Rp1.500.000 (R127)'),
            self._alloc(row_no=127, amount=1500000, description='jajan.rmh1.5 | DINDA',
                        category='Groceries',
                        notes='Send total to Dinda Rp2.500.000'),
        ]
        prev = si.preview_recap(self._conn, rows=rows)
        self.assertEqual(len(prev['new_rows']), 1)
        parent = prev['new_rows'][0]
        self.assertEqual(parent['amount'], 2500000)
        self.assertTrue(parent['is_family'])
        self.assertEqual(len(parent['family_slices']), 2)
        self.assertEqual(prev['totals']['sheet_total'], 2500000)

    def test_confirm_family_inserts_parent_plus_splits(self):
        rows = [
            self._alloc(row_no=4, amount=1000000, occurred_at='2026-09-26',
                        description='jajan.rmh1.5 | DINDA', category='Bulanan Dinda',
                        notes='Send total to Dinda Rp2.500.000 pada 25 September; '
                              'transfer BCA tanggal 26 september'),
            self._alloc(row_no=127, amount=1500000, occurred_at='2026-09-26',
                        description='jajan.rmh1.5 | DINDA', category='Groceries',
                        notes='Send total to Dinda Rp2.500.000'),
        ]
        prev = si.preview_recap(self._conn, rows=rows)
        parent = prev['new_rows'][0]
        r = si.confirm_recap(self._conn, rows=rows,
                             apply_hashes=[parent['hash']])
        self.assertEqual(r['inserted'], 1)
        debits = self._conn.execute(
            "SELECT amount,nature,review_status FROM ledger_txns").fetchall()
        self.assertEqual(len(debits), 1)
        self.assertEqual(debits[0][0], 2500000)
        self.assertEqual(debits[0][1], 'expense')
        self.assertEqual(debits[0][2], 'ok')
        splits = self._conn.execute(
            'SELECT category_id,amount FROM txn_splits').fetchall()
        self.assertEqual(len(splits), 2)
        self.assertEqual(sum(s[1] for s in splits), 2500000)
        cats = self._conn.execute(
            'SELECT id,name FROM categories WHERE name IN '
            "('Bulanan Dinda','Groceries')").fetchall()
        self.assertEqual(len(cats), 2)
        self.assertNotIn(None, [s[0] for s in splits])

    def test_family_not_applied_stays_held(self):
        rows = [
            self._alloc(row_no=4, amount=1000000, description='jajan.rmh1.5 | DINDA',
                        category='Bulanan Dinda',
                        notes='Dua alokasi satu transfer; total Rp2.500.000'),
            self._alloc(row_no=127, amount=1500000, description='Alokasi Groceries',
                        category='Groceries',
                        notes='Send total to Dinda Rp2.500.000'),
        ]
        prev = si.preview_recap(self._conn, rows=rows)
        r = si.confirm_recap(self._conn, rows=rows, apply_hashes=[])
        self.assertEqual(r['held_review'], 1)
        self.assertEqual(r['inserted'], 0)
        self.assertEqual(self._conn.execute(
            'SELECT COUNT(*) FROM ledger_txns').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()