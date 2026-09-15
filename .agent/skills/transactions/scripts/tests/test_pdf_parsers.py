"""Tests for the multi-bank statement PDF parsers + upload engine.

Run:  python -m unittest discover -s .agent/skills/transactions/scripts/tests
Or:   python .agent/skills/transactions/scripts/tests/test_pdf_parsers.py

Pure line-parser tests (no real PDFs) plus engine tests that stub the
parser so the whole upload->filter->ensure-account->auto-confirm pipeline
is exercised offline against a temp SQLite DB.
"""
from __future__ import annotations

import base64
import hashlib
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
import import_engine as ie
from parsers import bca_pdf
from parsers import bni_pdf
from parsers import gopay_pdf


def _row(**kw):
    base = {
        'provider': 'bca', 'src_txn_id': '', 'description': 'tx',
        'raw_description': '', 'transaction_type': 'payment',
        'merchant': '', 'recipient': '', 'direction': 'out',
        'principal_amount': 0, 'fee_amount': 0, 'total_amount': 0,
        'currency': 'IDR', 'occurred_at': '', 'bank_ref': '',
        'source_page': 0, 'raw1': '', 'raw2': '', 'parser_version': 'test',
    }
    base.update(kw)
    return base


class BCAParserTestCase(unittest.TestCase):
    def test_direction_and_amount_cases(self):
        cases = [
            # QRIS debit + saldo
            ('TRANSAKSI DEBIT TGL: 01/08 2,000.00 DB 24,397.09', 'out', 2000),
            # admin fee with branch code + saldo (0998 is NOT the amount)
            ('BIAYA ADM 0998 20,000.00 DB 4,397.09', 'out', 20000),
            # bank transfer out, amount only (no saldo)
            ('TRSF E-BANKING DB 0208/FTFVA/WS95031 18,000.00 DB', 'out', 18000),
            # transfer out with saldo sharing the line
            ('TRSF E-BANKING DB 0708/FTFVA/WS95031 1,001,000.00 DB 54,897.09',
             'out', 1001000),
            # credit with saldo on the same line -> mutasi is second-to-last
            ('TRSF E-BANKING CR 0708/FTSCY/WS95051 100,000.00 154,897.09',
             'in', 100000),
            ('TRSF E-BANKING CR 1408/FTSCY/WS95051 10,000.00 335,897.09',
             'in', 10000),
            # credit, mutasi only (no runnig balance shown)
            ('TRSF E-BANKING CR 0208/FTSCY/WS95031 20,000.00', 'in', 20000),
            # Bi-FAST credit with embedded DR marker + saldo
            ('BI-FAST CR BIF TRANSFER DR 1,200,000.00 1,280,897.09',
             'in', 1200000),
            ('BI-FAST CR BIF TRANSFER DR 400,000.00', 'in', 400000),
            # e-banking payment whose WSID ref must NOT eat into the amount
            ('BYR VIA E-BANKING 06/08 WSID9503100 53,000.00 DB', 'out', 53000),
            ('BYR VIA E-BANKING 07/08 WSID9503100 53,000.00 DB 51,897.09',
             'out', 53000),
        ]
        for rest, exp_dir, exp_amt in cases:
            with self.subTest(rest=rest):
                self.assertEqual(
                    bca_pdf._direction_and_amount(rest), (exp_dir, exp_amt))

    def test_clean_cont_strips_noise(self):
        cases = [
            (['QR 838', '00000.00Geprek Gob'], 'Geprek Gob'),
            (['008', 'RADIFELA KARYA UTA'], 'RADIFELA KARYA UTA'),
            (['TANGGAL :20/08/2026', 'DINDA FITRI NURUL', '20000.00'],
             'DINDA FITRI NURUL'),
            (['70001/GOPAY TOPUP', '081998986707'], '70001/GOPAY TOPUP'),
            (['QRC014', 'JS DUA BELAS DEV'], 'JS DUA BELAS DEV'),
        ]
        for lines, expected in cases:
            with self.subTest(lines=lines):
                self.assertEqual(bca_pdf._clean_cont(lines), expected)

    def test_parse_page_synthetic(self):
        page = [
            'TANGGAL KETERANGAN CBG MUTASI SALDO',
            '01/08 TRANSAKSI DEBIT TGL: 01/08 2,000.00 DB 24,397.09',
            'QR 838',
            '00000.00Geprek Gob',
            '02/08 TRSF E-BANKING CR 0208/FTSCY/WS95031 20,000.00',
            'DINDA FITRI NURUL',
            '03/08 BIAYA ADM 0998 20,000.00 DB 4,397.09',
            'MUTASI CR 1',
            'MUTASI DB 2',
        ]
        rows, idx = bca_pdf._parse_page(page, 2026)
        self.assertEqual(len(rows), 3)
        self.assertEqual(idx, 3)
        self.assertEqual([r['total_amount'] for r in rows], [2000, 20000, 20000])
        self.assertEqual([r['direction'] for r in rows], ['out', 'in', 'out'])
        self.assertEqual(rows[0]['merchant'], 'Geprek Gob')
        self.assertEqual(rows[1]['recipient'], 'DINDA FITRI NURUL')
        self.assertEqual(rows[2]['transaction_type'], 'fee')
        self.assertEqual(rows[2]['fee_amount'], 20000)
        # identical-looking rows keep unique src ids when start_idx advances
        rows2, _ = bca_pdf._parse_page([
            'TANGGAL KETERANGAN CBG MUTASI SALDO',
            '07/08 TRANSAKSI DEBIT TGL: 07/08 14,000.00 DB',
            '07/08 TRANSAKSI DEBIT TGL: 07/08 14,000.00 DB',
        ], 2026, start_idx=100)
        self.assertNotEqual(rows2[0]['src_txn_id'], rows2[1]['src_txn_id'])


class BNIParserTestCase(unittest.TestCase):
    def test_parse_lines_synthetic(self):
        lines = [
            'Saldo Awal 221,613',
            '01 Aug 2026 Pembayaran Qris',
            '-29,990 191,623',
            '08:30:38 WIB SUPERINDO CILANDAK - JAKARTA SELATAN',
            '11 Aug 2026 Transfer',
            '-206,000 17,715,805',
            '15:55:11 WIB - SAID ISKANDAR',
            '01 Aug 2026 Biaya',
            '-2,500 191,623',
            '18:29:12 WIB Transfer BI-FAST',
            '05 Aug 2026 Ewallet',
            '-50,000 145,000',
            '09:12:00 WIB TOP UP GOPAY 6281998986707',
            '31 Aug 2026 Lainnya',
            '+19,393,125 47,305',
            '06:08:08 WIB TRANSFER DARI',
            'Saldo Akhir 47,305',
        ]
        rows = bni_pdf._parse_lines(lines, 0)
        self.assertEqual(len(rows), 5)
        by_type = {r['transaction_type']: r for r in rows}

        qris = by_type['payment']
        self.assertEqual((qris['direction'], qris['total_amount']), ('out', 29990))
        self.assertEqual(qris['occurred_at'], '2026-08-01T08:30:38')
        self.assertEqual(qris['merchant'],
                         'SUPERINDO CILANDAK - JAKARTA SELATAN')

        transfer = by_type['transfer']
        self.assertEqual(transfer['direction'], 'out')
        self.assertEqual(transfer['recipient'], 'SAID ISKANDAR')

        fee = by_type['fee']
        self.assertEqual(fee['total_amount'], 2500)
        self.assertEqual(fee['principal_amount'], 0)
        self.assertEqual(fee['fee_amount'], 2500)

        ew = by_type['ewallet_topup']
        self.assertEqual(ew['direction'], 'out')
        self.assertEqual(ew['total_amount'], 50000)

        lain = by_type['other']
        self.assertEqual((lain['direction'], lain['total_amount']), ('in', 19393125))

        # pseudo-records skipped
        self.assertFalse(any('saldo awal' in r['description'].lower()
                             for r in rows))


class GoPayParserTestCase(unittest.TestCase):
    def test_new_layout_two_line(self):
        lines = [
            'Metode Pembayaran',
            '01/08/2026 Transfer ke bank ID2621936157519JKI GoPay Saldo Rp50.000',
            '08:30 JKI',
            '01/08/2026 Beli pulsa ID1234567890123 GoPay Saldo -Rp10.000',
            '09:12 123',
        ]
        rows = gopay_pdf._parse_new_layout(lines)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['direction'], 'in')
        self.assertEqual(rows[0]['total_amount'], 50000)
        self.assertEqual(rows[0]['occurred_at'], '2026-08-01T08:30:00')
        self.assertEqual(rows[1]['direction'], 'out')
        self.assertEqual(rows[1]['total_amount'], 10000)

    def test_coins_rows_marked(self):
        lines = [
            '02/08/2026 Cashback reward ID9999999999999 GoPay Coins Rp5.000',
            '10:00 999',
        ]
        rows = gopay_pdf._parse_new_layout(lines, fallback_wallet='saldo')
        self.assertEqual(rows[0]['wallet_type'], 'coins')

    def test_parse_amount(self):
        self.assertEqual(gopay_pdf._parse_amount('Rp 50.000'), 50000)
        self.assertEqual(gopay_pdf._parse_amount('-Rp10.000'), -10000)
        self.assertEqual(gopay_pdf._parse_amount('10.000'), 10000)
        self.assertEqual(gopay_pdf._parse_amount('14/06/2025'), 0)  # date


def _fake_pdf_bytes():
    return b'%PDF-1.4 fake statement payload 0123456789'


class UploadEngineTestCase(unittest.TestCase):
    """Engine pipeline with a stubbed parser (no real PDFs)."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self._conn = schema.connect(str(self._tmp / 't.db'))
        schema.ensure_tables(self._conn)
        self._orig = dict(ie.UPLOAD_PROVIDERS)
        self._b64 = base64.b64encode(_fake_pdf_bytes()).decode()

    def tearDown(self):
        ie.UPLOAD_PROVIDERS.clear()
        ie.UPLOAD_PROVIDERS.update(self._orig)
        self._conn.close()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _stub(self, provider, rows, seen=None):
        def parser(path, password='', **kwargs):
            if seen is not None:
                seen.append((str(path), password, kwargs))
            return {
                'rows': [dict(r) for r in rows],
                'account_name': 'STUB',
                'statement_period': 'Aug 2026',
                'parser_version': 'test',
                'warnings': [],
            }
        ie.UPLOAD_PROVIDERS[provider] = {
            'parser': parser, 'version': 'test',
            'kwargs': {'include_coins': False} if provider == 'gopay' else {},
        }

    def test_upload_filters_month_confirms_and_stamps_account(self):
        rows = [
            _row(src_txn_id='a1', description='QRIS alfamart', occurred_at='2026-08-01T08:00:00',
                 total_amount=25000, direction='out', provider='bca'),
            _row(src_txn_id='a2', description='GOPAY TOPUP', occurred_at='2026-08-15T10:00:00',
                 total_amount=50000, direction='out', provider='bca'),
            _row(src_txn_id='a3', description='Old row', occurred_at='2026-07-31T23:00:00',
                 total_amount=99999, direction='out', provider='bca'),
        ]
        self._stub('bca', rows)
        res = ie.upload_pdf(self._conn, filename='stmt_bca.pdf',
                            b64data=self._b64, provider='bca', month='2026-08')
        self.assertTrue(res.get('ok'))
        self.assertEqual(res['row_count'], 2)
        self.assertEqual(res['new_rows'], 2)
        self.assertEqual(res['skipped_rows'], 1)
        self.assertTrue(res['account_id'])

        # batch is auto-committed
        batch = store.get_import_batch(self._conn, res['batch_id'])
        self.assertEqual(batch['state'], 'committed')
        # ledger rows stamped with the BCA account
        acct = store.get_account(self._conn, res['account_id'])
        self.assertEqual(acct['provider'], 'bca')
        ledger = self._conn.execute(
            'SELECT amount, account_id FROM ledger_txns').fetchall()
        self.assertEqual(len(ledger), 2)
        self.assertTrue(all(a == res['account_id'] for _, a in ledger))

    def test_parsed_doc_without_batch_is_retriable(self):
        """A doc whose parse succeeded but whose batch never committed (crash
        mid-upload) must not permanently block re-import."""
        rows = [_row(src_txn_id='x1', occurred_at='2026-08-01T08:00:00',
                     total_amount=25000, direction='out', provider='bca')]
        self._stub('bca', rows)
        # simulate the crashed attempt: source_document present, no batch
        fingerprint = hashlib.sha256(_fake_pdf_bytes()).hexdigest()[:32]
        store.add_source_document(self._conn, kind='bca_pdf',
                                 source_key=f'upload:{fingerprint}',
                                 fingerprint=fingerprint, provider='bca')
        res = ie.upload_pdf(self._conn, filename='a.pdf', b64data=self._b64,
                            provider='bca', month='2026-08')
        self.assertTrue(res.get('ok'), res)
        self.assertEqual(res['row_count'], 1)

    def test_duplicate_document_blocked(self):
        rows = [_row(src_txn_id='x1', occurred_at='2026-08-01T08:00:00',
                     total_amount=25000, direction='out', provider='bca')]
        self._stub('bca', rows)
        first = ie.upload_pdf(self._conn, filename='a.pdf', b64data=self._b64,
                              provider='bca', month='2026-08')
        self.assertTrue(first.get('ok'))
        second = ie.upload_pdf(self._conn, filename='a.pdf', b64data=self._b64,
                               provider='bca', month='2026-08')
        self.assertFalse(second.get('ok'))
        self.assertIn('already been imported', second.get('error', ''))

    def test_ensure_account_is_idempotent_across_uploads(self):
        rows1 = [_row(src_txn_id='p1', occurred_at='2026-08-01T08:00:00',
                      total_amount=1000, direction='out', provider='bni')]
        rows2 = [_row(src_txn_id='p2', occurred_at='2026-08-02T08:00:00',
                      total_amount=2000, direction='out', provider='bni')]
        self._stub('bni', rows1)
        b1 = base64.b64encode(b'%PDF-bni-one-xxxxxxxx').decode()
        r1 = ie.upload_pdf(self._conn, filename='1.pdf', b64data=b1,
                           provider='bni', password='01041988', month='2026-08')
        self._stub('bni', rows2)
        b2 = base64.b64encode(b'%PDF-bni-two-yyyyyyyy').decode()
        r2 = ie.upload_pdf(self._conn, filename='2.pdf', b64data=b2,
                           provider='bni', password='01041988', month='2026-08')
        self.assertEqual(r1['account_id'], r2['account_id'])
        acct = store.get_account(self._conn, r1['account_id'])
        self.assertEqual((acct['provider'], acct['alias']), ('bni', 'BNI'))

    def test_password_and_coins_kwargs_forwarded(self):
        seen = []
        rows = [_row(src_txn_id='g1', occurred_at='2026-08-01T08:00:00',
                     total_amount=1000, direction='out', provider='gopay')]
        self._stub('gopay', rows, seen)
        res = ie.upload_pdf(self._conn, filename='gopay.pdf', b64data=self._b64,
                            provider='gopay', password='secret', month='2026-08')
        self.assertTrue(res.get('ok'))
        path, pw, kwargs = seen[0]
        self.assertEqual(pw, 'secret')
        self.assertEqual(kwargs.get('include_coins'), False)

    def test_upload_rejects_bad_provider_and_bad_month(self):
        self._stub('bca', [])
        res = ie.upload_pdf(self._conn, filename='x.pdf', b64data=self._b64,
                            provider='firstbank', month='2026-08')
        self.assertFalse(res.get('ok'))
        self.assertIn('Unsupported provider', res.get('error', ''))
        res = ie.upload_pdf(self._conn, filename='x.pdf', b64data=self._b64,
                            provider='bca', month='13-2026')
        self.assertFalse(res.get('ok'))


class SchemaMigrationTestCase(unittest.TestCase):
    def test_kind_allows_new_pdf_parsers(self):
        with tempfile.TemporaryDirectory() as d:
            conn = schema.connect(Path(d) / 't.db')
            schema.ensure_tables(conn)
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='source_documents'").fetchone()
            self.assertIn('bca_pdf', row[0])
            self.assertIn('bni_pdf', row[0])
            # inserting a bca_pdf kind must be allowed by the CHECK
            conn.execute(
                "INSERT INTO source_documents(kind, source_key, fingerprint) "
                "VALUES('bca_pdf','k','f')")
            conn.execute(
                "INSERT INTO source_documents(kind, source_key, fingerprint) "
                "VALUES('bni_pdf','k2','f2')")
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0], 2)
            conn.close()


if __name__ == '__main__':
    unittest.main()