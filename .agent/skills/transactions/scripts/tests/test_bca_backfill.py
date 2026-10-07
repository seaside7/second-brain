import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bca_backfill
import categorize
import schema
import store


def _q_cat(conn, category_id):
    r = conn.execute(
        'SELECT "group", name FROM categories WHERE id=?', (category_id,)).fetchone()
    return (r[0], r[1]) if r else (None, None)

SAMPLE = """\
Date	Description	Branch	Amount	Balance
01/09/2026	TRSF E-BANKING DB
0109/FTSCY/WS95031
600000.00
DINDA FITRI NURUL	0000	600,000.00	DB	53,998.09
02/09/2026	TRSF E-BANKING DB
0209/FTFVA/WS95031
70001/GOPAY TOPUP
-
-
081998986707	0000	901,000.00	DB	132,998.09
02/09/2026	TRSF E-BANKING CR
0209/FTFVA/WS95031
21000000076 / DANA
-
-
081998986707	0000	50,000.00	CR	183,998.09
05/09/2026	TRANSAKSI DEBIT TGL: 0509 QR 008 00000.00INDAH UTAM
0000	46,000.00	DB	129,998.09
06/09/2026	BIAYA ADM
0000	20,000.00	DB	109,998.09
PEND	BI-FAST CR TRANSFER DR 009 SAID ISKANDAR
0000	500,000.00	CR	5,000,000.00
"""


SAMPLE_CSV = """\
Account No.,=,`7310655826
Name,=,SAID ISKANDAR
Currency,=,IDR

'01/09/2026,TRSF E-BANKING DB 0109/FTSCY/WS95031         600000.00DINDA FITRI NURUL,`0000,600000.00,DB,53998.09
'02/09/2026,TRSF E-BANKING DB 0209/FTFVA/WS9503170001/GOPAY TOPUP -                 -                 081998986707,`0000,901000.00,DB,132998.09
'02/09/2026,TRSF E-BANKING CR 0209/FTSCY/WS95051         1000000.00GoPay Bank TransfeID2624832252246OHBDOMPET ANAK BANGSA,`0000,1000000.00,CR,1132998.09
'05/09/2026,TRSF E-BANKING DB 0509/FTFVA/WS9503112308/SPAYLATER   -                 -                 001564565408,`0000,103000.00,DB,424498.09
'05/09/2026,TRANSAKSI DEBIT TGL: 0905         QR  008           00000.00INDAH UTAM,`0000,33000.00,DB,116998.09
'PEND,TRSF E-BANKING DB 1309/FTSCY/WS95031         100000.00DINDA FITRI NURUL,`0000,100000.00,DB,521933.09
Starting Balance,=,653998.09
"""


class BcaBackfillTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, 't.db')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _conn(self):
        from schema import connect, ensure_tables
        c = connect(self.db)
        ensure_tables(c)
        c.execute('INSERT INTO categories(name, "group") VALUES(?, ?)', ('Internal Transfer', 'Transfers'))
        c.commit()
        return c

    def test_parse_statement(self):
        rows, meta = bca_backfill.parse_statement(SAMPLE)
        self.assertEqual(meta['raw_rows'], 6)
        self.assertEqual(meta['pending'], 1)
        self.assertEqual(len(rows), 6)
        bytype = {}
        for r in rows:
            bytype.setdefault(r['transaction_type'], []).append(r)
        self.assertEqual([r['total_amount'] for r in bytype['transfer']],
                         [600000, 500000])
        trf = bytype['transfer'][0]
        self.assertEqual(trf['recipient'], 'DINDA FITRI NURUL')
        self.assertEqual(trf['bank_ref'], '0109/FTSCY/WS95031')
        self.assertEqual(trf['direction'], 'out')
        # VA top up row: transaction_type va_payment, biller GoPay, internal
        va = bytype['va_payment'][0]
        self.assertEqual(va['recipient'], 'GoPay Top Up')
        self.assertEqual(va['description'], 'VA - GoPay Top Up')
        # DANA withdrawal -> VA payment (biller DANA) -> Review in categorizer
        dana = bytype['va_payment'][1]
        self.assertEqual(dana['direction'], 'in')
        self.assertEqual(dana['recipient'], 'DANA')
        self.assertEqual(dana['description'], 'VA - DANA')
        # QRIS row
        qr = bytype['qris'][0]
        self.assertEqual(qr['merchant'], 'INDAH UTAM')
        self.assertEqual(qr['description'], 'QRIS - INDAH UTAM')
        # fee row
        fee = bytype['fee'][0]
        self.assertEqual(fee['principal_amount'], 0)
        self.assertEqual(fee['fee_amount'], 20000)
        # pending row: date = end_date, mark pending
        ped = [r for r in rows if r.get('pending')]
        self.assertEqual(len(ped), 1)
        self.assertEqual(ped[0]['occurred_at'], '2026-09-13T00:00:00')
        self.assertEqual(ped[0]['recipient'], 'Said Iskandar')
        self.assertEqual(ped[0]['description'], 'Transfer - Said Iskandar')

    def test_match_and_roundtrip(self):
        conn = self._conn()
        rows, _ = bca_backfill.parse_statement(SAMPLE)
        doc = store.add_source_document(conn, kind='manual',
                                        source_key='bca_statement:TEST',
                                        fingerprint=(fp := bca_backfill.statement_fingerprint(SAMPLE)),
                                        provider='bca', parser_version='t')
        batch = store.add_import_batch(conn, doc)
        for i, r in enumerate(rows):
            bca_backfill.insert_new(conn, batch, doc, r, i)
        # re-running match on the same rows must be confident (or pending-finalizable)
        for r in rows:
            m = bca_backfill.match_existing(conn, r)
            self.assertIn(m['status'], ('confident', 'confident_pending'),
                          msg=f'{r["description"]} -> {m["status"]}')
        res = bca_backfill.run_backfill(conn, SAMPLE + '\n')
        self.assertTrue(res['ok'], msg=res)
        res2 = bca_backfill.run_backfill(conn, SAMPLE + '\n')
        self.assertFalse(res2['ok'])
        self.assertIn('already', res2.get('error', ''))
        # preview short-circuits after an import too
        prev = bca_backfill.preview(conn, SAMPLE + '\n')
        self.assertTrue(prev['already_imported'])

    def test_parse_csv_statement(self):
        rows, meta = bca_backfill.parse_statement(SAMPLE_CSV)
        self.assertEqual(meta['raw_rows'], 6)
        self.assertEqual(meta['pending'], 1)
        self.assertEqual(meta['errors'], [])
        self.assertEqual(len(rows), 6)
        bytype = {}
        for r in rows:
            bytype.setdefault(r['transaction_type'], []).append(r)

        trf = bytype['transfer'][0]
        self.assertEqual(trf['total_amount'], 600000)
        self.assertEqual(trf['direction'], 'out')
        self.assertEqual(trf['recipient'], 'DINDA FITRI NURUL')
        self.assertEqual(trf['bank_ref'], '0109/FTSCY/WS95031')

        va = bytype['va_payment'][0]
        self.assertEqual(va['recipient'], 'GoPay Top Up')
        self.assertEqual(va['transaction_type'], 'va_payment')
        spaylater = bytype['va_payment'][1]
        self.assertEqual(spaylater['recipient'], 'SPayLater')

        wallet = bytype['transfer'][1]
        self.assertEqual(wallet['direction'], 'in')
        self.assertEqual(wallet['recipient'], 'GoPay Wallet')
        self.assertEqual(wallet['description'], 'Transfer - GoPay Wallet')

        qr = bytype['qris'][0]
        self.assertEqual(qr['merchant'], 'INDAH UTAM')
        self.assertEqual(qr['description'], 'QRIS - INDAH UTAM')

        ped = [r for r in rows if r.get('pending')]
        self.assertEqual(len(ped), 1)
        self.assertEqual(ped[0]['occurred_at'], '2026-09-13T00:00:00')
        self.assertEqual(ped[0]['recipient'], 'DINDA FITRI NURUL')

    def test_csv_rows_categorize(self):
        conn = self._conn()
        store.add_account(conn, type='bank', provider='bca',
                          alias='BCA Personal', owner_name='Said Iskandar')
        conn.commit()
        rows, _ = bca_backfill.parse_statement(SAMPLE_CSV)
        cats = categorize.categorize_batch(conn, rows)
        bytype = {}
        for r in rows:
            bytype.setdefault(r['transaction_type'], []).append(r)
        # GoPay Top Up VA (excluded from spend)
        va = bytype['va_payment'][0]
        hit = next(c for c, r in zip(cats, rows) if r is va)
        self.assertEqual(hit['nature'], 'internal_transfer')
        # SPayLater loan payment
        sl = bytype['va_payment'][1]
        hit = next(c for c, r in zip(cats, rows) if r is sl)
        self.assertEqual(hit['nature'], 'expense')
        self.assertEqual(_q_cat(conn, hit['category_id']),
                         ('Loans', 'Loan Payment'))
        # GoPay wallet withdrawal (money in) -> internal, not income
        wl = bytype['transfer'][1]
        hit = next(c for c, r in zip(cats, rows) if r is wl)
        self.assertEqual(hit['nature'], 'internal_transfer')

    def test_preview_against_empty_db(self):
        conn = self._conn()
        prev = bca_backfill.preview(conn, SAMPLE)
        self.assertFalse(prev['already_imported'])
        self.assertEqual(prev['summary']['proposed_inserts'], len(prev['proposed_inserts']))


if __name__ == '__main__':
    unittest.main()