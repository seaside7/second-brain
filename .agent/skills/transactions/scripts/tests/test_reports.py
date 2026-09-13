"""reports.analytics + period bounds + store.nature_for_category.

Reconciliation guarantee: analytics totals are computed from the same bucket
rows the charts render, so cards/charts/list agree for identical filters.
"""
from __future__ import annotations

import calendar
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

SCRIPTS = str(Path(__file__).resolve().parent.parent)
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import schema
import store
import reports


class AnalyticsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self._conn = schema.connect(str(self._tmp / 't.db'))
        schema.ensure_tables(self._conn)
        self.doc_id = store.add_source_document(
            self._conn, kind='gmail', source_key='gmail:bca:MSG1',
            fingerprint='f1', provider='bca')
        self.batch_id = store.add_import_batch(self._conn, self.doc_id)
        self.food = store.get_or_create_category(
            self._conn, 'Food & Dining', group='Food & Dining')
        self.fee_cat = store.get_or_create_category(
            self._conn, 'Bank Fee', group='Fees')
        self.internal_cat = store.get_or_create_category(
            self._conn, 'Internal Transfer', group='Transfers')
        # 2 expense rows (different days) + 1 fee + 1 internal.
        self._seed('2026-09-02T10:00:00', 50000, 'expense', self.food)
        self._seed('2026-09-05T10:00:00', 30000, 'expense', self.food)
        self._seed('2026-09-05T11:00:00', 2500, 'fee', self.fee_cat)
        self._seed('2026-09-06T10:00:00', 200000, 'internal_transfer',
                   self.internal_cat)
        self._conn.commit()

    def tearDown(self):
        self._conn.close()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _seed(self, occurred_at, amount, nature, category_id):
        self._ext_idx = getattr(self, '_ext_idx', 0) + 1
        [ext_id] = store.add_extracted_rows(self._conn, self.batch_id,
                                            self.doc_id, [{
                                                'ext_index': self._ext_idx,
                                                'src_txn_id': f'seed-{self._ext_idx}',
                                                'description': 'seed',
                                                'direction': 'out',
                                                'total_amount': amount,
                                                'occurred_at': occurred_at,
                                                'provider': 'bca'}])
        return store.add_ledger_row(
            self._conn, ext_id, amount=amount, direction='out',
            nature=nature, category_id=category_id)

    def test_totals_reconcile_with_buckets(self):
        d = reports.analytics(
            self._conn, from_date='2026-09-01T00:00:00',
            to_date='2026-09-30T23:59:59')
        self.assertEqual(d['granularity'], 'day')
        # spend = expense + fee (fees included by default)
        self.assertEqual(d['totals']['spend'], 82500)
        self.assertEqual(d['totals']['spend'],
                         sum(b['spend'] for b in d['buckets']))
        self.assertEqual(d['totals']['count'],
                         sum(b['count'] for b in d['buckets']))
        # internal transfer is a movement line, never spend
        self.assertEqual(d['totals']['internal'], 200000)

    def test_exclude_fees_toggle(self):
        d = reports.analytics(
            self._conn, from_date='2026-09-01T00:00:00',
            to_date='2026-09-30T23:59:59', include_fees=False)
        self.assertEqual(d['totals']['spend'], 80000)

    def test_category_filter(self):
        d = reports.analytics(
            self._conn, from_date='2026-09-01T00:00:00',
            to_date='2026-09-30T23:59:59', category_ids=[self.food])
        self.assertEqual(d['totals']['spend'], 80000)
        self.assertEqual(len(d['mom']), 1)
        self.assertEqual(d['mom'][0]['current'], 80000)

    def test_mom_compare(self):
        d = reports.analytics(
            self._conn, from_date='2026-09-01T00:00:00',
            to_date='2026-09-30T23:59:59',
            cmp_from='2026-08-01T00:00:00', cmp_to='2026-08-31T23:59:59')
        by_name = {m['name']: m for m in d['mom']}
        self.assertEqual(by_name['Food & Dining']['previous'], 0)
        self.assertIsNone(by_name['Food & Dining']['pct'])
        self.assertEqual(by_name['Food & Dining']['delta'], 80000)

    def test_monthly_granularity_for_long_range(self):
        d = reports.analytics(
            self._conn, from_date='2026-01-01T00:00:00',
            to_date='2026-09-30T23:59:59')
        self.assertEqual(d['granularity'], 'month')
        self.assertEqual(d['totals']['spend'],
                         sum(b['spend'] for b in d['buckets']))


class PeriodBoundsTestCase(unittest.TestCase):
    def test_current_month_ends_on_real_last_day(self):
        now = datetime.now()
        f, t = reports.period_bounds('current_month')
        last = calendar.monthrange(now.year, now.month)[1]
        self.assertTrue(f.startswith(f'{now.year}-{now.month:02d}-01'))
        self.assertTrue(t.startswith(f'{now.year}-{now.month:02d}-{last:02d}'))

    def test_last_month_is_exact_calendar_month(self):
        now = datetime.now()
        f, t = reports.period_bounds('last_month')
        first_this = now.replace(day=1)
        self.assertLess(t, first_this.strftime('%Y-%m-%d'))
        self.assertTrue(f.endswith('-01T00:00:00'))
        # from/to fall in the same (previous) month
        self.assertEqual(f[:7], t[:7])
        self.assertNotEqual(f[:7], now.strftime('%Y-%m'))


class NatureForCategoryTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self._conn = schema.connect(str(self._tmp / 't.db'))
        schema.ensure_tables(self._conn)
        self.food = store.get_or_create_category(
            self._conn, 'Food & Dining', group='Food & Dining')
        self.internal = store.get_or_create_category(
            self._conn, 'Internal Transfer', group='Transfers')
        self.uncat = store.get_or_create_category(
            self._conn, 'Uncategorized', group='Uncategorized')

    def tearDown(self):
        self._conn.close()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_person_transfer_to_spend_becomes_expense(self):
        self.assertEqual(
            store.nature_for_category(self._conn, 'transfer_to_person', self.food),
            'expense')

    def test_person_transfer_to_transfers_group_stays(self):
        self.assertEqual(
            store.nature_for_category(self._conn, 'transfer_to_person', self.internal),
            'transfer_to_person')

    def test_person_transfer_to_uncategorized_stays(self):
        self.assertEqual(
            store.nature_for_category(self._conn, 'transfer_to_person', self.uncat),
            'transfer_to_person')

    def test_other_natures_untouched(self):
        self.assertEqual(
            store.nature_for_category(self._conn, 'expense', self.food), 'expense')
        self.assertEqual(
            store.nature_for_category(self._conn, 'internal_transfer', self.food),
            'internal_transfer')
        self.assertEqual(
            store.nature_for_category(self._conn, 'transfer_to_person', None),
            'transfer_to_person')


if __name__ == '__main__':
    unittest.main()
