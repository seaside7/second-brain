"""Tests for trip accounting semantics (reports.trip_breakdown and
store.trip_spend_total).

Covers the resolved rules: assigned allocations count once, trip-pinned
splits substitute their parent's amount, internal transfers / wallet top-ups
never count even when assigned, income natures are movement not spend, and
per-category attribution follows ledger rows and split allocations.
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
import reports


def _parsed(src, year=2026, month='08', day='01', **kw):
    return {
        'description': 'seeded tx', 'raw_description': '',
        'transaction_type': 'payment', 'merchant': '', 'recipient': '',
        'direction': 'out', 'principal_amount': 0, 'fee_amount': 0,
        'total_amount': 120000, 'occurred_at': f'{year}-{month}-{day}T09:00:00',
        'src_txn_id': src, 'provider': 'bca', 'parser_version': 'test',
        **kw,
    }


class TripReportBase(unittest.TestCase):
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

    def _seed(self, src, *, amount=120000, day='01', nature='expense',
              description='seeded tx', merchant='', **kw):
        ext_id = store.add_extracted_rows(
            self._conn, self.batch_id, self.doc_id,
            [_parsed(src, day=day, description=description,
                     merchant=merchant, ext_index=self._ext_idx, **kw)])[0]
        self._ext_idx += 1
        return store.add_ledger_row(
            self._conn, ext_id, amount=amount, direction='out',
            nature=nature, category_id=None, confidence='low',
            confidence_reason='seed')

    def _cat(self, name):
        return store.get_or_create_category(self._conn, name=name)


class TripBreakdownTests(TripReportBase):
    def test_whole_members_count_once_with_exclusions(self):
        tid = trips.create_trip(self._conn, name='Bali')['trip_id']
        food = self._cat('Food')
        lid_spend = self._seed('A', amount=40000, day='01')
        lid_fee = self._seed('F', amount=2000, day='02', nature='fee')
        lid_transfer = self._seed('I', amount=50000, day='03',
                                  nature='internal_transfer')
        lid_topup = self._seed('T', amount=30000, day='04', nature='top_up')
        lid_refund = self._seed('R', amount=15000, day='05', nature='refund',
                                direction='in')
        store.update_ledger(self._conn, lid_spend, category_id=food)
        trips.assign(self._conn, trip_id=tid,
                     ledger_ids=[lid_spend, lid_fee, lid_transfer,
                                 lid_topup, lid_refund])

        br = reports.trip_breakdown(self._conn, trip_id=tid)
        self.assertTrue(br['ok'])
        # spend = spend + fee; transfer/top_up excluded; refund is movement
        self.assertEqual(br['spend_total'], 42000)
        self.assertEqual(store.trip_spend_total(self._conn, tid), 42000)
        self.assertEqual(br['movement'], {'refund': 15000})
        self.assertEqual(len(br['excluded']), 2)
        natures = {r['nature'] for r in br['members']}
        self.assertEqual(natures, {'expense', 'fee', 'refund'})
        cat = next(c for c in br['by_category'] if c['category_id'] == food)
        self.assertEqual(cat['total'], 40000)

    def test_split_members_substitute_parent_amount(self):
        tid = trips.create_trip(self._conn, name='Solo')['trip_id']
        food = self._cat('Food')
        hotel = self._cat('Hotel')
        parent = self._seed('P', amount=200000, day='01',
                            nature='transfer_to_person')
        # 60k of the 200k transfer is hotel spend for the trip
        r = splits.set_splits(self._conn, ledger_id=parent, allocations=[
            {'category_id': hotel, 'trip_id': tid, 'amount': 60000},
            {'category_id': food, 'amount': 140000},
        ])
        self.assertTrue(r['ok'])
        # parent stays out of the trip; only the pinned split counts
        br = reports.trip_breakdown(self._conn, trip_id=tid)
        self.assertEqual(br['spend_total'], 60000)
        self.assertEqual(store.trip_spend_total(self._conn, tid), 60000)
        self.assertEqual(br['member_count'], 1)
        self.assertEqual(br['members'][0]['kind'], 'split')
        self.assertEqual(br['members'][0]['split_id'], r['splits'][0]['id'])
        cat = br['by_category'][0]
        self.assertEqual(cat['category_name'], 'Hotel')
        self.assertEqual(cat['total'], 60000)

    def test_other_trip_split_not_counted(self):
        tid_a = trips.create_trip(self._conn, name='A')['trip_id']
        tid_b = trips.create_trip(self._conn, name='B')['trip_id']
        food = self._cat('Food')
        parent = self._seed('P', amount=100000, day='01')
        trips.assign(self._conn, trip_id=tid_a, ledger_ids=[parent])
        splits.set_splits(self._conn, ledger_id=parent, allocations=[
            {'category_id': food, 'trip_id': tid_b, 'amount': 100000},
        ])
        # Every rupiah counts once: the split displaces the whole amount, so
        # trip A reports nothing and trip B carries the 100k.
        br_a = reports.trip_breakdown(self._conn, trip_id=tid_a)
        br_b = reports.trip_breakdown(self._conn, trip_id=tid_b)
        self.assertEqual(br_a['spend_total'], 0)
        self.assertEqual(br_b['spend_total'], 100000)
        self.assertEqual(br_a['spend_total'] + br_b['spend_total'], 100000)

    def test_returns_error_for_missing_trip(self):
        br = reports.trip_breakdown(self._conn, trip_id=99999)
        self.assertFalse(br['ok'])


if __name__ == '__main__':
    unittest.main()