"""Tests for the Transactions feature.

Uses in-memory SQLite for full isolation.  Run with:
    python -m pytest tests/test_transactions.py -v
    -- or --
    python -m unittest tests.test_transactions -v
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Add the transactions skill to path
_SKILL_DIR = Path(__file__).resolve().parent.parent / '.agent' / 'skills' / 'transactions' / 'scripts'
if str(_SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILL_DIR))

import schema
import store
from duplicates import check_duplicates
from categorize import _categorize_single, apply_correction
from reconcile import confirm_transfer, unlink_transfer, find_transfer_candidates
from reports import overview, _period_bounds
from import_engine import _infer_nature


class _InMemoryDB:
    """Context manager for an in-memory SQLite connection."""

    def __enter__(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys=ON')
        schema.ensure_tables(self.conn)
        return self.conn

    def __exit__(self, *args):
        self.conn.close()


class TestSchema(unittest.TestCase):
    def test_creates_tables(self):
        with _InMemoryDB() as conn:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()]
            for expected in ['accounts', 'source_documents', 'import_batches',
                             'extracted_txns', 'ledger_txns', 'transfers',
                             'categories', 'category_rules', 'corrections',
                             'audit_log', 'sync_state', '_meta']:
                self.assertIn(expected, tables)

    def test_schema_version(self):
        with _InMemoryDB() as conn:
            r = conn.execute("SELECT value FROM _meta WHERE key='schema_version'").fetchone()
            self.assertEqual(r[0], str(schema.SCHEMA_VERSION))

    def test_idempotent(self):
        with _InMemoryDB() as conn:
            schema.ensure_tables(conn)
            schema.ensure_tables(conn)  # second call should not error


class TestAccounts(unittest.TestCase):
    def test_add_and_list(self):
        with _InMemoryDB() as conn:
            acc_id = store.add_account(conn, type='bank', provider='bca',
                alias='BCA Personal', masked='****1234', owner_name='Said')
            self.assertIsInstance(acc_id, int)
            self.assertGreater(acc_id, 0)
            accounts = store.list_accounts(conn)
            self.assertEqual(len(accounts), 1)
            self.assertEqual(accounts[0]['alias'], 'BCA Personal')

    def test_get_account(self):
        with _InMemoryDB() as conn:
            acc_id = store.add_account(conn, type='ewallet', provider='gopay',
                alias='GoPay', masked='')
            acc = store.get_account(conn, acc_id)
            self.assertIsNotNone(acc)
            self.assertEqual(acc['provider'], 'gopay')

    def test_update_account(self):
        with _InMemoryDB() as conn:
            acc_id = store.add_account(conn, type='bank', provider='bca',
                alias='BCA')
            ok = store.update_account(conn, acc_id, alias='BCA Updated')
            self.assertTrue(ok)
            acc = store.get_account(conn, acc_id)
            self.assertEqual(acc['alias'], 'BCA Updated')


class TestSourceDocuments(unittest.TestCase):
    def test_add_and_exists(self):
        with _InMemoryDB() as conn:
            doc_id = store.add_source_document(conn,
                kind='gopay_pdf', source_key='upload:abc123',
                fingerprint='abc123')
            self.assertGreater(doc_id, 0)
            existing = store.source_doc_exists(conn, source_key='upload:abc123')
            self.assertEqual(existing, doc_id)

    def test_fingerprint_dupe(self):
        with _InMemoryDB() as conn:
            store.add_source_document(conn, kind='gopay_pdf',
                source_key='key1', fingerprint='fp1')
            existing = store.source_doc_exists(conn, fingerprint='fp1')
            self.assertIsNotNone(existing)


class TestDuplicateDetection(unittest.TestCase):
    def _setup_doc_batch(self, conn):
        doc_id = store.add_source_document(conn, kind='gmail',
            source_key='gmail:test:setup', fingerprint='setup_fp')
        batch_id = store.add_import_batch(conn, doc_id)
        return doc_id, batch_id

    def test_unique_new(self):
        with _InMemoryDB() as conn:
            status, dup_id = check_duplicates(conn,
                source_key='txn_new_123',
                total_amount=50000,
                occurred_at='2025-06-15T10:00:00',
                direction='out')
            self.assertEqual(status, 'unique')
            self.assertIsNone(dup_id)

    def test_exact_dup_by_src_txn_id(self):
        with _InMemoryDB() as conn:
            doc_id, batch_id = self._setup_doc_batch(conn)
            store.add_extracted_rows(conn, batch_id, doc_id, [{
                'src_txn_id': 'TXN_DUP_001',
                'total_amount': 100000,
                'occurred_at': '2025-06-15T10:00:00',
                'direction': 'out',
            }])
            store.update_import_batch(conn, batch_id, state='committed')

            status, dup_id = check_duplicates(conn,
                source_key='TXN_DUP_001',
                total_amount=100000,
                occurred_at='2025-06-15T10:00:00',
                direction='out')
            self.assertEqual(status, 'exact_dup')

    def test_possible_dup_same_amount_day(self):
        with _InMemoryDB() as conn:
            doc_id, batch_id = self._setup_doc_batch(conn)
            store.add_extracted_rows(conn, batch_id, doc_id, [{
                'src_txn_id': 'ORIG_TXN',
                'total_amount': 75000,
                'occurred_at': '2025-06-20T14:00:00',
                'direction': 'out',
                'description': 'Belanja di Toko',
            }])
            store.update_import_batch(conn, batch_id, state='committed')

            status, dup_id = check_duplicates(conn,
                source_key='DIFFERENT_TXN_ID',
                total_amount=75000,
                occurred_at='2025-06-20T16:00:00',
                direction='out')
            self.assertEqual(status, 'possible')


class TestCategorize(unittest.TestCase):
    def _make_row(self, **overrides):
        row = {
            'id': 1, 'description': '', 'merchant': '', 'recipient': '',
            'direction': 'out', 'total_amount': 50000, 'fee_amount': 0,
            'provider': 'gopay', 'src_txn_id': 'TXN001',
        }
        row.update(overrides)
        return row

    def test_fee_detection(self):
        with _InMemoryDB() as conn:
            row = self._make_row(description='Administrasi BCA', fee_amount=5000, total_amount=5000)
            result = _categorize_single(conn, row)
            self.assertEqual(result['nature'], 'fee')
            self.assertEqual(result['confidence'], 'high')

    def test_topup_detection(self):
        with _InMemoryDB() as conn:
            row = self._make_row(description='Top up GoPay')
            result = _categorize_single(conn, row)
            self.assertEqual(result['nature'], 'top_up')
            self.assertEqual(result['confidence'], 'high')

    def test_cashback_detection(self):
        with _InMemoryDB() as conn:
            row = self._make_row(description='Cashback reward', direction='in', total_amount=5000)
            result = _categorize_single(conn, row)
            self.assertEqual(result['nature'], 'cashback')

    def test_dinda_belanja_sayur(self):
        with _InMemoryDB() as conn:
            row = self._make_row(recipient='Dinda Fitri', description='Belanja Sayur')
            result = _categorize_single(conn, row)
            self.assertEqual(result['nature'], 'expense')
            self.assertIn('Dinda', result['reason'])

    def test_dinda_bayar_utang(self):
        with _InMemoryDB() as conn:
            row = self._make_row(recipient='Dinda', description='Bayar Utang')
            result = _categorize_single(conn, row)
            self.assertEqual(result['nature'], 'debt_repayment')

    def test_dinda_no_note(self):
        with _InMemoryDB() as conn:
            row = self._make_row(recipient='Dinda Fitri Nurul Aini', description='')
            result = _categorize_single(conn, row)
            self.assertEqual(result['nature'], 'transfer_to_person')

    def test_gotagihan_unknown(self):
        with _InMemoryDB() as conn:
            row = self._make_row(description='GoTagihan listrik')
            result = _categorize_single(conn, row)
            self.assertEqual(result['nature'], 'expense')
            self.assertEqual(result['confidence'], 'low')

    def test_refund_detection(self):
        with _InMemoryDB() as conn:
            row = self._make_row(description='Refund pembatalan', direction='in')
            result = _categorize_single(conn, row)
            self.assertEqual(result['nature'], 'refund')
            self.assertEqual(result['confidence'], 'high')

    def test_income_direction_in(self):
        with _InMemoryDB() as conn:
            row = self._make_row(description='Transfer masuk', direction='in', total_amount=200000)
            result = _categorize_single(conn, row)
            self.assertEqual(result['nature'], 'income')


class TestTransferReconciliation(unittest.TestCase):
    def _setup_doc_batch(self, conn):
        doc_id = store.add_source_document(conn, kind='manual',
            source_key='manual:tr_setup', fingerprint='tr_setup_fp')
        batch_id = store.add_import_batch(conn, doc_id)
        return doc_id, batch_id

    def test_confirm_and_unlink(self):
        with _InMemoryDB() as conn:
            doc_id, batch_id = self._setup_doc_batch(conn)

            acc1 = store.add_account(conn, type='bank', provider='bca', alias='BCA')
            acc2 = store.add_account(conn, type='ewallet', provider='gopay', alias='GoPay')

            ext_ids = store.add_extracted_rows(conn, batch_id, doc_id, [{
                'src_txn_id': 'BCA_OUT', 'total_amount': 100000,
                'direction': 'out', 'occurred_at': '2025-06-15T10:00:00',
            }, {
                'src_txn_id': 'GP_IN', 'total_amount': 100000,
                'direction': 'in', 'occurred_at': '2025-06-15T10:05:00',
            }])
            ext1, ext2 = ext_ids[0], ext_ids[1]

            lid1 = store.add_ledger_row(conn, ext1, account_id=acc1,
                amount=100000, direction='out', nature='internal_transfer')
            lid2 = store.add_ledger_row(conn, ext2, account_id=acc2,
                amount=100000, direction='in', nature='top_up')

            result = confirm_transfer(conn, from_ledger_id=lid1,
                to_ledger_id=lid2, principal_amount=100000, matched_by='manual')
            self.assertTrue(result['ok'])
            self.assertIn('transfer_id', result)

            t = store.get_transfer_for_ledger(conn, lid1)
            self.assertIsNotNone(t)
            self.assertEqual(t['status'], 'confirmed')

            result = unlink_transfer(conn, t['id'])
            self.assertTrue(result['ok'])

            t2 = store.get_transfer_for_ledger(conn, lid1)
            self.assertIsNone(t2)

    def test_one_to_one_enforced(self):
        with _InMemoryDB() as conn:
            doc_id, batch_id = self._setup_doc_batch(conn)

            ext_ids = store.add_extracted_rows(conn, batch_id, doc_id, [{
                'src_txn_id': 'T1', 'total_amount': 50000,
                'direction': 'out', 'occurred_at': '2025-06-15T10:00:00',
            }, {
                'src_txn_id': 'T2', 'total_amount': 50000,
                'direction': 'in', 'occurred_at': '2025-06-15T10:00:00',
            }, {
                'src_txn_id': 'T3', 'total_amount': 50000,
                'direction': 'in', 'occurred_at': '2025-06-15T10:00:00',
            }])
            ext1, ext2, ext3 = ext_ids

            lid1 = store.add_ledger_row(conn, ext1, amount=50000, direction='out', nature='internal_transfer')
            lid2 = store.add_ledger_row(conn, ext2, amount=50000, direction='in', nature='top_up')
            lid3 = store.add_ledger_row(conn, ext3, amount=50000, direction='in', nature='top_up')

            confirm_transfer(conn, from_ledger_id=lid1, to_ledger_id=lid2,
                principal_amount=50000)

            result = confirm_transfer(conn, from_ledger_id=lid3, to_ledger_id=lid2,
                principal_amount=50000)
            self.assertFalse(result['ok'])


class TestReports(unittest.TestCase):
    def test_overview_empty(self):
        with _InMemoryDB() as conn:
            result = overview(conn, period='current_month')
            self.assertEqual(result['expense'], 0)
            self.assertEqual(result['total_count'], 0)

    def test_period_bounds(self):
        from_d, to_d = _period_bounds('current_month')
        self.assertIsNotNone(from_d)
        self.assertIsNotNone(to_d)

    def test_all_time(self):
        from_d, to_d = _period_bounds('all')
        self.assertIsNone(from_d)
        self.assertIsNone(to_d)


class TestInferNature(unittest.TestCase):
    def test_topup(self):
        row = {'description': 'Top up GoPay via BCA', 'direction': 'out', 'fee_amount': 0}
        self.assertEqual(_infer_nature(row), 'top_up')

    def test_fee(self):
        row = {'description': 'Administrasi', 'direction': 'out', 'fee_amount': 5000, 'total_amount': 5000}
        self.assertEqual(_infer_nature(row), 'expense')

    def test_cashback(self):
        row = {'description': 'Cashback reward', 'direction': 'in', 'fee_amount': 0}
        self.assertEqual(_infer_nature(row), 'cashback')

    def test_refund(self):
        row = {'description': 'Refund order cancelled', 'direction': 'in', 'fee_amount': 0}
        self.assertEqual(_infer_nature(row), 'refund')

    def test_needs_review_default(self):
        row = {'description': 'Some unknown transaction', 'direction': 'out', 'fee_amount': 0}
        self.assertEqual(_infer_nature(row), 'needs_review')


class TestFmtIdr(unittest.TestCase):
    def test_format(self):
        self.assertEqual(store.fmt_idr(100000), 'Rp100.000')
        self.assertEqual(store.fmt_idr(1500000), 'Rp1.500.000')
        self.assertEqual(store.fmt_idr(0), 'Rp0')


class TestAuditTrail(unittest.TestCase):
    def test_add_and_list(self):
        with _InMemoryDB() as conn:
            store.add_audit(conn, action='test_action', entity='test_entity',
                entity_id=42, after_json='{"key":"value"}')
            rows = store.list_audit(conn, entity='test_entity')
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['action'], 'test_action')


class TestSyncState(unittest.TestCase):
    def test_set_and_get(self):
        with _InMemoryDB() as conn:
            store.set_sync_state(conn, 'gmail_history_id', '12345')
            val = store.get_sync_state(conn, 'gmail_history_id')
            self.assertEqual(val, '12345')

    def test_default_empty(self):
        with _InMemoryDB() as conn:
            val = store.get_sync_state(conn, 'nonexistent')
            self.assertEqual(val, '')


class TestCorrections(unittest.TestCase):
    def test_add_correction(self):
        with _InMemoryDB() as conn:
            doc_id = store.add_source_document(conn, kind='manual',
                source_key='manual:corr', fingerprint='corr_fp')
            batch_id = store.add_import_batch(conn, doc_id)
            ext = store.add_extracted_rows(conn, batch_id, doc_id, [{
                'src_txn_id': 'C1', 'total_amount': 10000,
                'direction': 'out', 'occurred_at': '2025-06-15T10:00:00',
            }])[0]
            lid = store.add_ledger_row(conn, ext, amount=10000, direction='out',
                nature='needs_review')
            cid = store.add_correction(conn, ledger_id=lid,
                field='nature', before_val='needs_review', after_val='expense',
                reason='Manual correction')
            self.assertGreater(cid, 0)


class TestRules(unittest.TestCase):
    def test_add_and_match(self):
        with _InMemoryDB() as conn:
            cat_id = store.get_or_create_category(conn, 'Groceries')
            rule_id = store.add_rule(conn,
                merchant_or_recipient='belanja sayur',
                category_id=cat_id, nature='expense')
            self.assertGreater(rule_id, 0)
            rules = store.list_rules(conn)
            self.assertEqual(len(rules), 1)
            self.assertEqual(rules[0]['merchant_or_recipient'], 'belanja sayur')

    def test_deactivate(self):
        with _InMemoryDB() as conn:
            cat_id = store.get_or_create_category(conn, 'Test')
            rule_id = store.add_rule(conn,
                merchant_or_recipient='test_rule',
                category_id=cat_id)
            store.deactivate_rule(conn, rule_id)
            rules = store.list_rules(conn, active_only=True)
            self.assertEqual(len(rules), 0)


class TestImportLifecycle(unittest.TestCase):
    def test_confirm_and_delete(self):
        from import_engine import confirm_import, delete_import
        with _InMemoryDB() as conn:
            # Create doc + batch with extracted rows
            doc_id = store.add_source_document(conn, kind='gopay_pdf',
                source_key='upload:lc_test', fingerprint='lc_fp')
            batch_id = store.add_import_batch(conn, doc_id)
            ext_ids = store.add_extracted_rows(conn, batch_id, doc_id, [{
                'src_txn_id': 'LC1', 'total_amount': 50000,
                'direction': 'out', 'occurred_at': '2025-06-15T10:00:00',
                'description': 'Top up GoPay',
            }])

            # Confirm
            result = confirm_import(conn, batch_id)
            self.assertTrue(result['ok'])
            self.assertEqual(result['new_rows'], 1)

            # Verify ledger created
            ledger = store.list_ledger(conn, limit=10)
            self.assertEqual(len(ledger), 1)

            # Delete
            result = delete_import(conn, batch_id)
            self.assertTrue(result['ok'])

            # Verify batch cancelled
            batch = store.get_import_batch(conn, batch_id)
            self.assertEqual(batch['state'], 'cancelled')


if __name__ == '__main__':
    unittest.main()
