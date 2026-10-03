"""Tests for the trips + splits modules.

Covers trip lifecycle and assignment (with correction rows so reprocess keeps
them), candidate search by date band / keyword, and split allocation semantics
(atomic replace, duplicate-key rejection, mismatch flag, unsplit).
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
import trips
import splits


def _parsed(src, year=2026, month='08', day='01', **kw):
    return {
        'description': 'seeded tx', 'raw_description': '',
        'transaction_type': 'payment', 'merchant': '', 'recipient': '',
        'direction': 'out', 'principal_amount': 0, 'fee_amount': 0,
        'total_amount': 120000, 'occurred_at': f'{year}-{month}-{day}T09:00:00',
        'src_txn_id': src, 'provider': 'bca', 'parser_version': 'test',
        **kw,
    }


class TripsSplitsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self._conn = schema.connect(str(self._tmp / 't.db'))
        schema.ensure_tables(self._conn)
        self.doc_id = store.add_source_document(
            self._conn, kind='bca_pdf', source_key='gmail:bca:MSG1',
            fingerprint='fp1', provider='bca')
        self.batch_id = store.add_import_batch(self._conn, self.doc_id)
        self._ext_idx = 0

    def tearDown(self):
        self._conn.close()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _seed_ledger(self, **parsed):
        ext_id = store.add_extracted_rows(
            self._conn, self.batch_id, self.doc_id,
            [_parsed(ext_index=self._ext_idx, **parsed)])[0]
        self._ext_idx += 1
        return store.add_ledger_row(
            self._conn, ext_id, amount=parsed.get(
                'total_amount', 120000),
            direction=parsed.get('direction', 'out'),
            nature='expense', category_id=None, confidence='low',
            confidence_reason='seed')

    def _cat(self, name):
        return store.get_or_create_category(self._conn, name=name)


class TripLifecycleTests(TripsSplitsTestCase):
    def test_create_validation_and_list(self):
        r = trips.create_trip(self._conn, name='')
        self.assertFalse(r['ok'])
        r = trips.create_trip(self._conn, name='Bali 2026',
                              start_date='2026-09-10', end_date='2026-09-05')
        self.assertFalse(r['ok'])
        self.assertIn('end_date', r.get('error', ''))
        r = trips.create_trip(self._conn, name='Bali 2026',
                              destination='Bali',
                              start_date='2026-09-10', end_date='2026-09-15')
        self.assertTrue(r['ok'])
        trips.create_trip(self._conn, name='Lombok', start_date='2026-10-01')
        lst = trips.list_trips(self._conn)
        self.assertEqual(lst['count'], 2)
        self.assertEqual(lst['trips'][0]['name'], 'Lombok')
        self.assertIn('spend_total', lst['trips'][0])

    def test_update_and_audit(self):
        t = trips.create_trip(self._conn, name='Bali', destination='Bali')
        tid = t['trip_id']
        r = trips.update_trip(self._conn, trip_id=tid, destination='Ubud',
                              notes='own trip')
        self.assertTrue(r['ok'])
        got = store.get_trip(self._conn, tid)
        self.assertEqual(got['destination'], 'Ubud')
        ev = [e for e in store.list_audit(self._conn, entity='trips')
              if e['action'] == 'trip_update']
        self.assertEqual(len(ev), 1)

    def test_assign_unassign_writes_corrections_and_audit(self):
        lid1 = self._seed_ledger(src='T1', day='01')
        lid2 = self._seed_ledger(src='T2', day='02')
        tid = trips.create_trip(self._conn, name='Bali', start_date='2026-08-01',
                                end_date='2026-08-31')['trip_id']

        r = trips.assign(self._conn, trip_id=tid, ledger_ids=[lid1, lid2, 9999])
        self.assertEqual(r['assigned'], [lid1, lid2])
        self.assertEqual(r['missing'], [9999])
        for lid in (lid1, lid2):
            self.assertEqual(store.get_ledger(self._conn, lid)['trip_id'], tid)
            self.assertTrue(store.has_manual_correction(
                self._conn, lid, ('trip_id',)))
        corr = self._conn.execute(
            "SELECT field, before_val, after_val FROM corrections "
            "WHERE ledger_id=? AND field='trip_id'", (lid1,)).fetchone()
        self.assertEqual(tuple(corr), ('trip_id', '', str(tid)))

        again = trips.assign(self._conn, trip_id=tid, ledger_ids=[lid1])
        self.assertEqual(again['already_assigned'], [lid1])
        self.assertEqual(again['assigned'], [])

        u = trips.unassign(self._conn, ledger_ids=[lid1])
        self.assertEqual(u['unassigned'], [lid1])
        self.assertIsNone(store.get_ledger(self._conn, lid1)['trip_id'])
        corr2 = self._conn.execute(
            "SELECT after_val FROM corrections WHERE ledger_id=? "
            "AND field='trip_id' ORDER BY id DESC LIMIT 1", (lid1,)).fetchone()
        self.assertEqual(corr2[0], '')

    def test_get_trip_aggregates_without_double_counting(self):
        tid = trips.create_trip(self._conn, name='Bali')['trip_id']
        lid1 = self._seed_ledger(src='A', month='08', day='01', total_amount=100000)
        lid2 = self._seed_ledger(src='B', month='08', day='02', total_amount=50000)
        store.update_ledger(self._conn, lid2, nature='internal_transfer')
        trips.assign(self._conn, trip_id=tid, ledger_ids=[lid1, lid2])
        got = trips.get_trip(self._conn, trip_id=tid)
        self.assertEqual(got['spend_total'], 100000)
        self.assertEqual(got['member_count'], 2)


class CandidateSearchTests(TripsSplitsTestCase):
    def test_date_window_and_keyword(self):
        self._seed_ledger(src='MAY', month='05', day='15')
        lid_jul = self._seed_ledger(src='JUL', month='07', day='20',
                                    description='Hotel Santika')
        lid_aug = self._seed_ledger(src='AUG', month='08', day='01',
                                    description='flight ticket')

        r = trips.find_trip_candidates(self._conn, from_date='2026-07-01',
                                       to_date='2026-08-31')
        self.assertEqual(r['count'], 2)
        got = {x['id'] for x in r['rows']}
        self.assertEqual(got, {lid_jul, lid_aug})

        k = trips.find_trip_candidates(self._conn, keyword='santika')
        self.assertEqual(k['count'], 1)
        self.assertEqual(k['rows'][0]['id'], lid_jul)

    def test_default_excludes_assigned_include_brings_them_back(self):
        lid = self._seed_ledger(src='X', month='08', day='01')
        tid1 = trips.create_trip(self._conn, name='Trip1',
                                 start_date='2026-08-01', end_date='2026-08-31')['trip_id']
        tid2 = trips.create_trip(self._conn, name='Trip2',
                                 start_date='2026-08-01', end_date='2026-08-31')['trip_id']
        trips.assign(self._conn, trip_id=tid1, ledger_ids=[lid])
        r = trips.find_trip_candidates(self._conn, trip_id=tid2)
        self.assertEqual(r['count'], 0)
        r2 = trips.find_trip_candidates(self._conn, trip_id=tid2,
                                        include_assigned=True)
        self.assertEqual(r2['count'], 1)


class SplitAllocationTests(TripsSplitsTestCase):
    def test_set_splits_atomic_replace_and_balance(self):
        cat_food = self._cat('Food')
        cat_hotel = self._cat('Hotel')
        lid = self._seed_ledger(src='S1', total_amount=120000)

        r = splits.set_splits(self._conn, ledger_id=lid, allocations=[
            {'category_id': cat_food, 'amount': 70000},
            {'category_id': cat_hotel, 'amount': 50000},
        ])
        self.assertTrue(r['ok'])
        self.assertFalse(r['mismatch'])
        self.assertEqual(len(r['splits']), 2)
        bal = splits.split_balance(self._conn, ledger_id=lid)
        self.assertEqual((bal['allocated_total'], bal['mismatch']), (120000, False))

        splits.set_splits(self._conn, ledger_id=lid, allocations=[
            {'category_id': cat_food, 'amount': 40000},
            {'category_id': cat_hotel, 'amount': 40000},
            {'category_id': cat_hotel, 'amount': 40000},  # dup key -> rejected
        ])
        self.assertEqual(len(splits.list_splits(self._conn, ledger_id=lid)['splits']), 2)

    def test_mismatch_flagged_not_rebalanced(self):
        cat = self._cat('Food')
        lid = self._seed_ledger(src='S2', total_amount=120000)
        r = splits.set_splits(self._conn, ledger_id=lid, allocations=[
            {'category_id': cat, 'amount': 50000}])
        self.assertTrue(r['ok'])
        self.assertTrue(r['mismatch'])
        parent = store.get_ledger(self._conn, lid)
        self.assertEqual(parent['amount'], 120000)
        bal = splits.split_balance(self._conn, ledger_id=lid)
        self.assertTrue(bal['mismatch'])

    def test_invalid_category_and_missing_trip_rejected(self):
        lid = self._seed_ledger(src='S3', total_amount=100000)
        r = splits.set_splits(self._conn, ledger_id=lid, allocations=[
            {'category_id': 99999, 'amount': 100000}])
        self.assertFalse(r['ok'])
        self.assertIn('category', r.get('error', ''))
        cat = self._cat('Food')
        r2 = splits.set_splits(self._conn, ledger_id=lid, allocations=[
            {'category_id': cat, 'trip_id': 99999, 'amount': 100000}])
        self.assertFalse(r2['ok'])
        self.assertIn('trip', r2.get('error', ''))

    def test_unsplit_clears_allocations(self):
        cat_food = self._cat('Food')
        cat_hotel = self._cat('Hotel')
        lid = self._seed_ledger(src='S4', total_amount=120000)
        splits.set_splits(self._conn, ledger_id=lid, allocations=[
            {'category_id': cat_food, 'amount': 60000},
            {'category_id': cat_hotel, 'amount': 60000}])
        r = splits.unsplit(self._conn, ledger_id=lid)
        self.assertTrue(r['ok'])
        self.assertEqual(len(splits.list_splits(self._conn, ledger_id=lid)['splits']), 0)

    def test_trip_pinned_split_keys(self):
        cat_travel = self._cat('Travel')
        lid = self._seed_ledger(src='S5', total_amount=90000)
        tid = trips.create_trip(self._conn, name='Bali')['trip_id']
        r = splits.set_splits(self._conn, ledger_id=lid, allocations=[
            {'category_id': cat_travel, 'trip_id': tid, 'amount': 90000}])
        self.assertTrue(r['ok'])
        self.assertEqual(r['splits'][0]['trip_id'], tid)
        self.assertEqual(r['splits'][0]['trip_name'], 'Bali')


if __name__ == '__main__':
    unittest.main()