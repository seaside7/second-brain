"""Tests for restricted dashboard roles (dashboard/access.py + auth roles).

The 'dinda' role is a read-only family viewer: one menu (Transactions), a list
limited to rows that mention her, and an overview that hides five categories.
These tests pin the three layers that make that true:

  * auth        : roles are stored, returned with the session, validated
  * access      : the route allowlist (deny by default)
  * scoping     : TEMP VIEWs shadow the real tables, so the unmodified report
                  queries only see permitted rows

Run with:
    python -m unittest tests.test_roles -v
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT / 'dashboard', _ROOT / '.agent' / 'skills' / 'transactions' / 'scripts'):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import access   # noqa: E402
import reports  # noqa: E402
import schema   # noqa: E402
import store    # noqa: E402

_PW = 'Bootstrap#2026'
HIDDEN = ('Project Payment', 'Project Cost', 'Friend Repayment', 'Arif Loan', 'Friend Loan')


# ── auth: roles ──────────────────────────────────────────────────────────
class TestAuthRoles(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls._tmp.name) / 'auth.db'
        os.environ['PSB_AUTH_DB'] = str(cls.db)
        import auth
        cls.auth = auth

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()
        os.environ.pop('PSB_AUTH_DB', None)

    def setUp(self):
        self.auth.AUTH_DB = self.db
        for suffix in ('', '-wal', '-shm'):
            p = Path(str(self.db) + suffix)
            if p.exists():
                p.unlink()
        self.auth.init_db()
        self.auth._FAILURES.clear()

    def test_default_role_is_owner(self):
        self.auth.create_user('said', _PW)
        token, _ = self.auth.login('said', _PW)
        self.assertEqual(self.auth.validate_session(token)['role'], 'owner')

    def test_role_is_stored_and_returned_with_the_session(self):
        self.auth.create_user('dinda', _PW, role='dinda')
        token, _ = self.auth.login('dinda', _PW)
        self.assertEqual(self.auth.validate_session(token)['role'], 'dinda')

    def test_unknown_role_is_rejected(self):
        with self.assertRaises(ValueError):
            self.auth.create_user('x', _PW, role='admin')

    def test_database_without_the_role_column_is_migrated(self):
        legacy = Path(self._tmp.name) / 'legacy.db'
        c = sqlite3.connect(legacy)
        c.executescript(
            "CREATE TABLE users (username TEXT PRIMARY KEY, pw_hash BLOB NOT NULL, "
            "pw_salt BLOB NOT NULL, created_at INTEGER NOT NULL, changed_at INTEGER NOT NULL, "
            "must_change INTEGER NOT NULL DEFAULT 0);"
            "INSERT INTO users VALUES ('said', x'00', x'00', 1, 1, 0);")
        c.commit()
        c.close()
        self.auth.AUTH_DB = legacy
        self.auth.init_db()
        row = self.auth.get_user('said')
        self.assertEqual(row['role'], 'owner')          # existing accounts stay owners


# ── access: route allowlist ──────────────────────────────────────────────
class TestAccessPolicy(unittest.TestCase):

    def test_owner_may_do_anything(self):
        for method, route in (('GET', '/api/finance'), ('POST', '/api/transactions/manual'),
                              ('GET', '/samudera')):
            self.assertTrue(access.allows('owner', method, route))

    def test_dinda_gets_only_the_two_transaction_views(self):
        for route in ('/api/transactions/list', '/api/transactions/analytics',
                      '/api/transactions/categories', '/api/transactions/cycle'):
            self.assertTrue(access.allows('dinda', 'GET', route), route)

    def test_dinda_is_denied_everything_else(self):
        for route in ('/api/finance', '/api/dashboard', '/api/trading/status',
                      '/api/transactions/accounts', '/api/transactions/review',
                      '/api/transactions/transfers', '/api/transactions/trips',
                      '/api/transactions/rules', '/api/transactions/audit',
                      '/api/transactions/imports', '/api/transactions/12',
                      '/api/chat', '/api/memory-notes', '/api/invoices', '/api/file/x'):
            self.assertFalse(access.allows('dinda', 'GET', route), route)

    def test_dinda_cannot_write(self):
        for route in ('/api/transactions/manual', '/api/transactions/12/edit',
                      '/api/transactions/upload', '/api/transactions/cycle',
                      '/api/transactions/splits/set', '/api/chat'):
            self.assertFalse(access.allows('dinda', 'POST', route), route)
        self.assertFalse(access.allows('dinda', 'DELETE', '/api/transactions/list'))

    def test_dinda_can_sign_out_and_change_her_password(self):
        self.assertTrue(access.allows('dinda', 'POST', '/api/logout'))
        self.assertTrue(access.allows('dinda', 'POST', '/api/change-password'))
        self.assertTrue(access.allows('dinda', 'GET', '/api/session'))

    def test_static_shell_is_reachable_but_other_pages_are_not(self):
        for route in ('/', '/index.html', '/app.js', '/tab-transactions.js', '/style.css',
                      '/fonts/inter.woff2', '/login', '/change-password'):
            self.assertTrue(access.allows('dinda', 'GET', route), route)
        for route in ('/samudera', '/exampleprogram_promo_proposal.html',
                      '/indonesia-demography-infographic.html'):
            self.assertFalse(access.allows('dinda', 'GET', route), route)

    def test_unknown_role_gets_nothing(self):
        self.assertFalse(access.allows('mystery', 'GET', '/api/transactions/list'))

    def test_ui_profile(self):
        self.assertFalse(access.ui_profile('owner')['restricted'])
        p = access.ui_profile('dinda')
        self.assertTrue(p['restricted'])
        self.assertEqual(p['tabs'], ['transactions'])
        self.assertEqual(p['tx_views'], ['overview', 'all'])


# ── row-level scoping ────────────────────────────────────────────────────
class TestScopedData(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / 'tx.db'
        self.conn = schema.connect(self.path)
        schema.ensure_tables(self.conn)
        c = self.conn
        c.execute("INSERT INTO source_documents(kind, source_key, fingerprint) "
                  "VALUES ('manual', 'k', 'f')")
        c.execute("INSERT INTO import_batches(source_document_id, state) VALUES (1, 'committed')")
        self.cat = {}
        for name in HIDDEN + ('Groceries', 'Mortgage', 'Dinda Allowance'):
            self.cat[name] = c.execute(
                'INSERT INTO categories(name, "group") VALUES (?, ?)', (name, 'T')).lastrowid
        self.n = 0
        c.commit()

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def add(self, desc, amount, cat=None, *, recipient='', notes='', review='ok',
            nature='expense', when='2026-11-10T10:00:00', extra=None):
        self.n += 1
        c = self.conn
        ext = c.execute(
            "INSERT INTO extracted_txns(doc_id, batch_id, ext_index, provider, src_txn_id, "
            "description, recipient, direction, principal_amount, total_amount, occurred_at, "
            "bank_ref, phone_suffix) VALUES (1, 1, ?, 'bca', ?, ?, ?, 'out', ?, ?, ?, 'REF', '1234')",
            (self.n, f'S{self.n}', desc, recipient, amount, amount, when)).lastrowid
        lid = c.execute(
            "INSERT INTO ledger_txns(ext_id, amount, direction, nature, category_id, notes, "
            "review_status) VALUES (?, ?, 'out', ?, ?, ?, ?)",
            (ext, amount, nature, self.cat.get(cat) if cat else None, notes, review)).lastrowid
        c.commit()
        return lid

    def scoped(self, endpoint):
        conn = schema.connect(self.path)
        access.scoped_connection(conn, 'dinda', endpoint)
        self.addCleanup(conn.close)
        return conn

    # --- list: rows that mention Dinda -----------------------------------
    def test_list_shows_every_row_that_mentions_dinda_whatever_its_status(self):
        a = self.add('Transfer ke DINDA', 500, 'Groceries')
        b = self.add('BI Fast', 300, recipient='Dinda Pratiwi', review='review')
        d = self.add('Bulanan', 200, notes='untuk dinda', review='uncategorized')
        e = self.add('Transfer Dinda', 100, 'Friend Loan')        # hidden category, still hers
        self.add('Cicilan KPR', 9000, 'Mortgage')
        self.add('Warung', 50, 'Groceries')
        conn = self.scoped('/api/transactions/list')
        rows = store.list_ledger(conn, limit=100)
        self.assertEqual({r['id'] for r in rows}, {a, b, d, e})
        self.assertEqual(store.count_ledger(conn), 4)

    def test_list_does_not_leak_other_rows_through_search_or_filters(self):
        self.add('Cicilan KPR', 9000, 'Mortgage')
        conn = self.scoped('/api/transactions/list')
        self.assertEqual(store.list_ledger(conn, search='KPR'), [])
        self.assertEqual(store.list_ledger(conn, category_ids=[self.cat['Mortgage']]), [])

    def test_list_detail_lookup_of_a_foreign_row_finds_nothing(self):
        other = self.add('Cicilan KPR', 9000, 'Mortgage')
        conn = self.scoped('/api/transactions/list')
        self.assertIsNone(store.get_ledger(conn, other))

    # --- overview: everything except the hidden categories ---------------
    def test_overview_excludes_hidden_categories_from_totals_and_lists(self):
        self.add('Belanja', 100, 'Groceries')
        self.add('KPR', 1000, 'Mortgage')
        self.add('Client invoice', 7000, 'Project Payment', nature='income')
        self.add('Hosting', 400, 'Project Cost')
        self.add('Pinjam teman', 300, 'Friend Loan')
        self.add('Bayar teman', 200, 'Friend Repayment')
        self.add('Arif', 250, 'Arif Loan')
        conn = self.scoped('/api/transactions/analytics')
        data = reports.analytics(conn, from_date='2026-11-01', to_date='2026-11-30')
        self.assertEqual(data['totals']['spend'], 1100)            # Groceries + Mortgage only
        self.assertEqual(data['totals']['income'], 0)              # Project Payment hidden
        names = {c['name'] for c in data['categories'].values()}
        self.assertEqual(names, {'Groceries', 'Mortgage'})
        self.assertFalse(names & set(HIDDEN))

    def test_category_list_hides_the_hidden_categories(self):
        conn = self.scoped('/api/transactions/categories')
        names = {c['name'] for c in store.list_categories(conn)}
        self.assertFalse(names & set(HIDDEN))
        self.assertIn('Groceries', names)

    def test_a_parent_split_into_a_hidden_category_never_reappears_whole(self):
        parent = self.add('Transfer besar', 1000)
        self.conn.execute(
            "INSERT INTO txn_splits(parent_ledger_id, category_id, amount) VALUES (?,?,?)",
            (parent, self.cat['Groceries'], 600))
        self.conn.execute(
            "INSERT INTO txn_splits(parent_ledger_id, category_id, amount) VALUES (?,?,?)",
            (parent, self.cat['Project Cost'], 400))
        self.conn.commit()
        conn = self.scoped('/api/transactions/analytics')
        data = reports.analytics(conn, from_date='2026-11-01', to_date='2026-11-30')
        self.assertEqual(data['totals']['spend'], 0)               # not 1000, not 600

    # --- safety -----------------------------------------------------------
    def test_scoped_connection_is_read_only(self):
        self.add('Transfer ke Dinda', 500)
        conn = self.scoped('/api/transactions/list')
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("UPDATE ledger_txns SET notes = 'x'")
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("DELETE FROM ledger_txns")

    def test_endpoints_outside_the_policy_cannot_be_scoped(self):
        conn = schema.connect(self.path)
        self.addCleanup(conn.close)
        with self.assertRaises(PermissionError):
            access.scoped_connection(conn, 'dinda', '/api/transactions/accounts')
        with self.assertRaises(PermissionError):
            access.scoped_connection(conn, 'nobody', '/api/transactions/list')

    def test_the_real_tables_are_untouched(self):
        self.add('Cicilan KPR', 9000, 'Mortgage')
        self.scoped('/api/transactions/list')
        total = self.conn.execute('SELECT COUNT(*) FROM ledger_txns').fetchone()[0]
        self.assertEqual(total, 1)                                  # owner connection still sees it

    def test_strip_row_blanks_account_identifiers(self):
        row = {'description': 'Transfer Dinda', 'account_masked': '****1234', 'bank_ref': 'R1',
               'phone_suffix': '99', 'src_txn_id': 'S1', 'email_subject': 'Mutasi',
               'trip_name': 'Solo', 'amount': 5}
        out = access.strip_row('dinda', dict(row))
        for k in ('account_masked', 'bank_ref', 'phone_suffix', 'src_txn_id',
                  'email_subject', 'trip_name'):
            self.assertEqual(out[k], '')
        self.assertEqual(out['description'], 'Transfer Dinda')
        self.assertEqual(out['amount'], 5)


if __name__ == '__main__':
    unittest.main()
