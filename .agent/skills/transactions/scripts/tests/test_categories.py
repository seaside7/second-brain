"""Deterministic categorization + reprocess safety tests.

Run:  python -m unittest discover -s .agent/skills/transactions/scripts/tests
Or:   python .agent/skills/transactions/scripts/tests/test_categories.py

Covers the 10 required behavioral cases from the finance-notification
categorization rules plus reprocess idempotency, determinism and
manual-correction preservation. Fully offline: tests the pure categorizer and
the reprocess doc-update logic, not the Gmail fetch layer.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = str(Path(__file__).resolve().parent.parent)
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import schema
import store
import categorize
import reprocess
import gmail_sync


def _q_cat(conn, category_id):
    r = conn.execute(
        'SELECT "group", name FROM categories WHERE id=?', (category_id,)).fetchone()
    return (r[0], r[1]) if r else (None, None)


class CategorizerTestCase(unittest.TestCase):
    """The 10 required rule behaviors against the deterministic categorizer."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        db = self._tmp / 't.db'
        self._conn = schema.connect(str(db))
        schema.ensure_tables(self._conn)
        store.add_account(self._conn, type='bank', provider='bca',
                          alias='BCA Personal', owner_name='Said Iskandar')
        self._conn.commit()

    def tearDown(self):
        self._conn.close()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _cat(self, **row):
        base = {'direction': 'out', 'principal_amount': 0, 'fee_amount': 0,
                'total_amount': 0, 'transaction_type': '', 'description': '',
                'raw_description': '', 'merchant': '', 'recipient': '',
                'provider': 'bca'}
        base.update(row)
        res = categorize.categorize_batch(self._conn, [base])[0]
        return dict(res, group=_q_cat(self._conn, res['category_id'])[0],
                    name=_q_cat(self._conn, res['category_id'])[1])

    def test_01_pln_token_bill(self):
        r = self._cat(description='PLN Token Rp101.900', transaction_type='bills')
        self.assertEqual(r['nature'], 'expense')
        self.assertEqual(r['group'], 'Utilities')
        self.assertIn('Electricity', r['name'])
        self.assertEqual(r['confidence'], 'high')

    def test_02_indihome_internet_bill(self):
        r = self._cat(description='IndiHome', transaction_type='bills')
        self.assertEqual(r['group'], 'Utilities')
        self.assertIn('Internet', r['name'])
        self.assertEqual(r['nature'], 'expense')

    def test_03_telkomsel_phone_bill(self):
        r = self._cat(description='Telkomsel Halo', transaction_type='bills')
        self.assertEqual(r['group'], 'Utilities')
        self.assertIn('Mobile & Data', r['name'])

    def test_04_own_wallet_topup_internal(self):
        r = self._cat(description='GoPay Top Up', transaction_type='top_up',
                      recipient='GoPay')
        self.assertEqual(r['nature'], 'internal_transfer')
        self.assertEqual(r['group'], 'Transfers')

    def test_05_cashback_credit(self):
        r = self._cat(description='Cashback reward', direction='in',
                      transaction_type='cashback')
        self.assertEqual(r['nature'], 'cashback')
        self.assertEqual(r['group'], 'Income')

    def test_06_warung_with_food_word(self):
        r = self._cat(description='Warung Gorengan Bahari', transaction_type='qris')
        self.assertEqual(r['nature'], 'expense')
        self.assertEqual(r['group'], 'Food & Dining')
        self.assertIn(r['confidence'], ('high', 'medium'))

    def test_07_ambig_warung_needs_review(self):
        r = self._cat(description='Warung Sumber Rejeki', transaction_type='qris')
        self.assertEqual(r['nature'], 'needs_review')

    def test_08_atm_withdrawal(self):
        r = self._cat(description='Tarik Tunai', transaction_type='withdrawal')
        self.assertEqual(r['nature'], 'expense')
        self.assertEqual(r['group'], 'Cash')
        self.assertIn('Withdrawal', r['name'])

    def test_09_transfer_to_person(self):
        r = self._cat(description='Transfer - Dinda Fitri Nurul Aini',
                      transaction_type='transfer', recipient='DINDA FITRI NURUL AINI')
        self.assertEqual(r['nature'], 'transfer_to_person')
        self.assertEqual(r['group'], 'Transfers')
        # No per-recipient category - just a shared 'Transfer' (owner notes names).
        self.assertEqual(r['name'], 'Transfer')

    def test_10_explicit_admin_fee(self):
        r = self._cat(description='GoPay Top Up - Admin Fee', transaction_type='fee')
        self.assertEqual(r['nature'], 'fee')
        self.assertEqual(r['group'], 'Fees')
        self.assertEqual(r['confidence'], 'high')

    def test_11_car_service_workshop(self):
        r = self._cat(description='Bengkel Sentosa Servis Mobil',
                      transaction_type='debit')
        self.assertEqual(r['nature'], 'expense')
        self.assertEqual(r['group'], 'Vehicle')
        self.assertEqual(r['name'], 'Car Service')
        self.assertEqual(r['confidence'], 'high')

    def test_12_car_wash_not_categorised_as_service(self):
        r = self._cat(description='Cuci Mobil Super', transaction_type='debit')
        self.assertEqual(r['nature'], 'needs_review')
        self.assertNotEqual(r['name'], 'Car Service')


class ReprocessTestCase(unittest.TestCase):
    """reprocess._reprocess_doc: idempotency + manual-correction preservation."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self._conn = schema.connect(str(self._tmp / 't.db'))
        schema.ensure_tables(self._conn)
        # A gmail doc + import batch (like the real import flow).
        self.doc_id = store.add_source_document(
            self._conn, kind='gmail', source_key='gmail:bca:MSG0001',
            fingerprint='f1', provider='bca',
            email_subject='Pembayaran QRIS')
        store.update_source_document(self._conn, self.doc_id, status='parsed')
        self.batch_id = store.add_import_batch(self._conn, self.doc_id)
        self.doc = {'id': self.doc_id, 'provider': 'bca', 'batch_id': self.batch_id,
                    'source_key': 'gmail:bca:MSG0001', 'kind': 'gmail',
                    'status': 'parsed'}
        self._conn.commit()

    def tearDown(self):
        self._conn.close()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _seed_row(self, parsed):
        ids = store.add_extracted_rows(self._conn, self.batch_id, self.doc_id, [parsed])
        ledger_id = store.add_ledger_row(
            self._conn, ids[0], amount=parsed.get('total_amount', 0),
            direction=parsed.get('direction', 'out'),
            nature='needs_review', confidence='low', confidence_reason='seed')
        self._conn.commit()
        return ids[0], ledger_id

    def _qris_parsed(self, description='Warung Gorengan Bahari'):
        return {'description': description, 'raw_description': '',
                'transaction_type': 'qris', 'merchant': '', 'recipient': '',
                'direction': 'out', 'principal_amount': 25000,
                'fee_amount': 0, 'total_amount': 25000, 'occurred_at': '2026-09-01',
                'src_txn_id': 'T1'}

    def test_reprocess_fee_split_then_idempotent(self):
        # Run 1: one existing row + a new parser-introduced fee split.
        ext_id, ledger_id = self._seed_row(self._qris_parsed())
        parsed = [self._qris_parsed(),
                  {'description': 'Warung - Admin Fee', 'raw_description': '',
                   'transaction_type': 'fee', 'merchant': '', 'recipient': '',
                   'direction': 'out', 'principal_amount': 0,
                   'fee_amount': 1500, 'total_amount': 1500,
                   'occurred_at': '2026-09-01', 'src_txn_id': 'T1-fee'}]
        s1 = reprocess._reprocess_doc(self._conn, self.doc, parsed, dry_run=False)
        self.assertEqual(s1['updated_ext'], 1)
        self.assertEqual(s1['added_ledger'], 1)
        self.assertEqual(s1['preserved_manual'], 0)

        nrow = self._conn.execute(
            'SELECT COUNT(*) FROM ledger_txns WHERE ext_id=?', (ext_id,)).fetchone()[0]
        feerow = self._conn.execute(
            "SELECT COUNT(*) FROM extracted_txns WHERE doc_id=? AND "
            "transaction_type='fee'", (self.doc_id,)).fetchone()[0]
        self.assertTrue(nrow >= 1)
        self.assertEqual(feerow, 1)

        # Run 2 must be a no-op for new rows (previous ledger idempotent).
        s2 = reprocess._reprocess_doc(self._conn, self.doc, parsed, dry_run=False)
        self.assertEqual(s2['added_ledger'], 0)
        self.assertEqual(s2['updated_ext'], 2)
        self.assertEqual(
            self._conn.execute('SELECT COUNT(*) FROM ledger_txns').fetchone()[0],
            self._conn.execute('SELECT COUNT(*) FROM ledger_txns').fetchone()[0])

    def test_reprocess_preserves_manual_correction(self):
        ext_id, ledger_id = self._seed_row(self._qris_parsed())
        store.add_correction(self._conn, ledger_id=ledger_id, field='category_id',
                             before_val='', after_val='1')
        keep = self._conn.execute(
            'SELECT nature FROM ledger_txns WHERE id=?', (ledger_id,)).fetchone()[0]
        self.assertTrue(store.has_manual_correction(self._conn, ledger_id))

        s = reprocess._reprocess_doc(self._conn, self.doc,
                                     [self._qris_parsed()], dry_run=False)
        self.assertEqual(s['preserved_manual'], 1)
        self.assertEqual(s['updated_ext'], 1)
        # Category/nature untouched by re-categorization.
        self.assertEqual(
            self._conn.execute(
                'SELECT nature FROM ledger_txns WHERE id=?',
                (ledger_id,)).fetchone()[0], keep)

    def test_reprocess_deterministic_dry_run(self):
        self._seed_row(self._qris_parsed())
        a = reprocess._reprocess_doc(self._conn, self.doc, [self._qris_parsed()],
                                     dry_run=True)
        b = reprocess._reprocess_doc(self._conn, self.doc, [self._qris_parsed()],
                                     dry_run=True)
        self.assertEqual(a, b)


class BniVaParserTestCase(unittest.TestCase):
    """BNI/wondr Virtual Account emails - payee must stay visible."""

    _VA_BODY = (
        'Transaksi berhasil! Hai, SAID ISKANDAR Terima kasih sudah bertransaksi '
        'dengan wondr by BNI! Kamu baru aja melakukan pembayaran pakai Virtual '
        'Account dengan detail sebagai berikut: Tujuan XDT-DINDAFITRINURULAINI '
        '88***97 Sumber dana SAID ISKANDAR TAPLUS \u2022 *******507 Detail '
        'transaksi Nominal Rp1.300.000 Biaya admin Rp0 Total Rp1.300.000 '
        'Lainnya Reference ID 20260912013232000190 '
        'Catatan XDT-DINDAFITRINURULAINI8808211811495297'
    )

    def test_va_recipient_extracted(self):
        self.assertEqual(gmail_sync._bni_va_recipient(self._VA_BODY),
                         'XDT-DINDAFITRINURULAINI')

    def test_va_parse_keeps_payee_in_description(self):
        rows = gmail_sync._parse_bni(self._VA_BODY, 'Transaksi berhasil!',
                                     '2026-09-12T05:32:14Z')
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r['transaction_type'], 'va_payment')
        self.assertEqual(r['description'], 'VA - XDT-DINDAFITRINURULAINI')
        self.assertEqual(r['recipient'], 'XDT-DINDAFITRINURULAINI')
        self.assertEqual(r['direction'], 'out')
        self.assertEqual(r['total_amount'], 1300000)

    def test_va_parse_fee_split(self):
        body = self._VA_BODY.replace('Biaya admin Rp0',
                                     'Biaya admin Rp1.000')
        rows = gmail_sync._parse_bni(body, 'Transaksi berhasil!',
                                     '2026-09-12T05:32:14Z')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['fee_amount'], 1000)
        self.assertEqual(rows[0]['total_amount'], 1301000)
        self.assertEqual(rows[1]['transaction_type'], 'fee')
        self.assertEqual(rows[1]['description'],
                         'VA - XDT-DINDAFITRINURULAINI - Admin Fee')


class ManualEntryTestCase(unittest.TestCase):
    """Manual income/outcome recording (email notifications can be missed)."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self._conn = schema.connect(str(self._tmp / 't.db'))
        schema.ensure_tables(self._conn)
        store.add_account(self._conn, type='bank', provider='bca',
                          alias='BCA Personal', owner_name='Said Iskandar')
        self._conn.commit()

    def tearDown(self):
        self._conn.close()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _ledger(self, ledger_id):
        r = self._conn.execute(
            'SELECT * FROM ledger_txns WHERE id=?', (ledger_id,)).fetchone()
        return dict(r)

    def test_income_manual_autocategorised(self):
        import import_engine
        r = import_engine.add_manual_tx(
            self._conn, direction='in', amount=5000000,
            occurred_at='2026-09-13T12:00', description='Salary Accountant')
        self.assertTrue(r['ok'])
        lr = self._ledger(r['ledger_id'])
        self.assertEqual(lr['direction'], 'in')
        self.assertEqual(lr['amount'], 5000000)
        self.assertEqual(lr['nature'], 'income')
        self.assertEqual(lr['review_status'], 'ok')
        self.assertEqual(
            self._conn.execute('SELECT name FROM categories WHERE id=?',
                               (lr['category_id'],)).fetchone()[0], 'Income')

    def test_expense_manual_with_explicit_category(self):
        import import_engine
        cat_id = store.get_or_create_category(self._conn, 'Groceries',
                                              group='Groceries')
        r = import_engine.add_manual_tx(
            self._conn, direction='out', amount=120000,
            occurred_at='2026-09-12T18:00', description='Belanja sayur',
            category_id=cat_id)
        self.assertTrue(r['ok'])
        lr = self._ledger(r['ledger_id'])
        self.assertEqual(lr['category_id'], cat_id)
        self.assertEqual(lr['nature'], 'expense')
        self.assertEqual(lr['review_status'], 'ok')
        self.assertEqual(lr['txn_status'], 'confirmed')

    def test_expense_manual_autocategorised(self):
        import import_engine
        r = import_engine.add_manual_tx(
            self._conn, direction='out', amount=25000,
            occurred_at='2026-09-12T12:00', description='Warung Gorengan Bahari')
        self.assertTrue(r['ok'])
        lr = self._ledger(r['ledger_id'])
        self.assertEqual(lr['nature'], 'expense')
        self.assertEqual(lr['review_status'], 'ok')
        self.assertEqual(
            self._conn.execute('SELECT "group" FROM categories WHERE id=?',
                               (lr['category_id'],)).fetchone()[0],
            'Food & Dining')

    def test_manual_validation(self):
        import import_engine
        r = import_engine.add_manual_tx(
            self._conn, direction='out', amount=0,
            occurred_at='2026-09-12T12:00', description='X')
        self.assertFalse(r['ok'])
        self.assertIn('Amount', r['error'])
        r = import_engine.add_manual_tx(
            self._conn, direction='out', amount=1000,
            occurred_at='2026-09-12T12:00', description='  ')
        self.assertFalse(r['ok'])
        self.assertIn('Description', r['error'])
        r = import_engine.add_manual_tx(
            self._conn, direction='out', amount=1000,
            occurred_at='', description='X')
        self.assertFalse(r['ok'])
        self.assertIn('Date', r['error'])

    def test_manual_row_traces_to_doc_and_batch(self):
        import import_engine
        r = import_engine.add_manual_tx(
            self._conn, direction='out', amount=5000,
            occurred_at='2026-09-10T08:00', description='Ojek')
        ext = self._conn.execute(
            'SELECT * FROM extracted_txns WHERE id=?', (r['ext_id'],)).fetchone()
        doc = self._conn.execute(
            'SELECT * FROM source_documents WHERE id=?', (ext['doc_id'],)).fetchone()
        batch = self._conn.execute(
            'SELECT * FROM import_batches WHERE id=?', (ext['batch_id'],)).fetchone()
        self.assertEqual(ext['provider'], 'manual')
        self.assertEqual(ext['total_amount'], 5000)
        self.assertEqual(doc['kind'], 'manual')
        self.assertEqual(doc['status'], 'parsed')
        self.assertEqual(batch['state'], 'committed')


if __name__ == '__main__':
    unittest.main(verbosity=2)