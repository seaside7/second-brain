"""Tests for dashboard authentication (dashboard/auth.py).

Every test points AUTH_DB at its own temp file via the PSB_AUTH_DB env var, so
the suite never touches a real credentials file and can run concurrently with
a live server. Module-level state that is NOT the DB -- the in-process
brute-force limiter -- is reset explicitly in setUp, because that dict is
module global and would otherwise leak attempts between tests.

Run with:
    python -m unittest tests.test_auth -v
"""
from __future__ import annotations

import inspect
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_DASHBOARD = Path(__file__).resolve().parent.parent / 'dashboard'
if str(_DASHBOARD) not in sys.path:
    sys.path.insert(0, str(_DASHBOARD))

_PW = 'Bootstrap#2026'
_NEW_PW = 'Replaced#2026x'


class AuthTestCase(unittest.TestCase):
    """Points the module at a throwaway DB before it is used.

    AUTH_DB is resolved once at import time, so setting PSB_AUTH_DB in the
    environment is only enough for the *first* importer. Each test therefore
    also rebinds auth.AUTH_DB directly -- otherwise a second test class would
    silently read and write the first class's database. Overwriting the
    attribute (rather than reloading the module) keeps every other reference
    to the module object valid.
    """

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


# ── password storage ─────────────────────────────────────────────────────
class TestPasswordStorage(AuthTestCase):

    def test_plaintext_password_is_never_stored(self):
        """The single most important property: a leaked DB must not yield the
        password. Checked against the raw file bytes, not just the API."""
        self.auth.create_user('said', _PW)
        blob = self.db.read_bytes()
        self.assertNotIn(_PW.encode(), blob)
        self.assertNotIn(b'Bootstrap', blob)

    def test_hash_is_salted_so_identical_passwords_differ(self):
        self.auth.create_user('a', _PW)
        self.auth.create_user('b', _PW)
        ra, rb = self.auth.get_user('a'), self.auth.get_user('b')
        self.assertNotEqual(bytes(ra['pw_salt']), bytes(rb['pw_salt']))
        self.assertNotEqual(bytes(ra['pw_hash']), bytes(rb['pw_hash']))

    def test_verify_accepts_correct_and_rejects_wrong(self):
        h, s = self.auth.hash_password(_PW)
        self.assertTrue(self.auth.verify_password(_PW, h, s))
        self.assertFalse(self.auth.verify_password(_PW + 'x', h, s))
        self.assertFalse(self.auth.verify_password('', h, s))

    def test_db_file_is_not_world_readable(self):
        self.auth.create_user('said', _PW)
        mode = self.db.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600, f'expected 0600, got {oct(mode)}')

    def test_password_policy_accepts_reasonable_passwords(self):
        for pw in ('bootstrap2026', 'lower+upper', 'Passw0rd', _PW, _NEW_PW):
            with self.subTest(pw=pw):
                self.assertIsNone(self.auth.password_problem(pw))

    def test_password_policy_rejects_weak_passwords(self):
        # (password, why) pairs -- a dict would be unpacked key-first, which
        # silently tests the label instead of the password.
        cases = [
            ('a1!',            'under the 8-char minimum'),
            ('alllowercase',   'one character class only'),
            (' Boot2026! ',    'leading/trailing space'),
        ]
        for pw, why in cases:
            with self.subTest(pw=repr(pw), why=why):
                self.assertIsNotNone(self.auth.password_problem(pw), why)

    def test_create_user_rejects_weak_password(self):
        with self.assertRaises(ValueError):
            self.auth.create_user('said', 'abc')

    def test_user_exists(self):
        self.assertFalse(self.auth.user_exists('said'))
        self.auth.create_user('said', _PW)
        self.assertTrue(self.auth.user_exists('said'))


# ── login ────────────────────────────────────────────────────────────────
class TestLogin(AuthTestCase):

    def setUp(self):
        super().setUp()
        self.auth.create_user('said', _PW)

    def test_valid_login_returns_token(self):
        token, err = self.auth.login('said', _PW, ip='10.0.0.1')
        self.assertIsNotNone(token)
        self.assertEqual(err, '')

    def test_wrong_password_and_unknown_user_are_indistinguishable(self):
        """Prevents username enumeration: the two failure modes must be
        byte-identical, not merely similar."""
        _, e1 = self.auth.login('said', 'wrong', ip='10.0.0.1')
        _, e2 = self.auth.login('nobody', 'wrong', ip='10.0.0.1')
        self.assertEqual(e1, e2)

    def test_tokens_are_unique_per_login(self):
        a, _ = self.auth.login('said', _PW)
        b, _ = self.auth.login('said', _PW)
        self.assertNotEqual(a, b)

    def test_token_is_not_stored_in_the_clear(self):
        token, _ = self.auth.login('said', _PW)
        self.assertNotIn(token.encode(), self.db.read_bytes())

    def test_empty_username_rejected_without_traceback(self):
        token, err = self.auth.login('', _PW)
        self.assertIsNone(token)
        self.assertTrue(err)

    def test_rate_limit_locks_out_the_ip(self):
        for _ in range(self.auth.MAX_ATTEMPTS):
            self.auth.login('said', 'wrong', ip='9.9.9.9')
        token, err = self.auth.login('said', _PW, ip='9.9.9.9')
        self.assertIsNone(token)
        self.assertIn('too many attempts', err)

    def test_rate_limit_is_per_username(self):
        """Brute-forcing one account must not lock the owner out of another."""
        for _ in range(self.auth.MAX_ATTEMPTS + 1):
            self.auth.login('victim', 'wrong', ip='8.8.8.8')
        self.assertIsNone(self.auth.login('said', _PW, ip='8.8.8.8')[0])

    def test_successful_login_clears_the_failure_count(self):
        for _ in range(self.auth.MAX_ATTEMPTS - 1):
            self.auth.login('said', 'wrong', ip='7.7.7.7')
        self.auth.login('said', _PW, ip='7.7.7.7')
        self.assertNotIn('ip:7.7.7.7', self.auth._FAILURES)
        # A fresh allowance is available again.
        self.assertIsNotNone(self.auth.login('said', _PW, ip='7.7.7.7')[0])


# ── sessions ─────────────────────────────────────────────────────────────
class TestSessions(AuthTestCase):

    def setUp(self):
        super().setUp()
        self.auth.create_user('said', _PW)

    def test_valid_token_resolves_to_user(self):
        token, _ = self.auth.login('said', _PW)
        sess = self.auth.validate_session(token)
        self.assertIsNotNone(sess)
        self.assertEqual(sess['username'], 'said')

    def test_missing_and_forged_tokens_rejected(self):
        self.assertIsNone(self.auth.validate_session(None))
        self.assertIsNone(self.auth.validate_session(''))
        self.assertIsNone(self.auth.validate_session('not-a-real-token'))

    def test_expired_session_rejected(self):
        token, _ = self.auth.login('said', _PW)
        with self.auth._conn() as c:
            c.execute('UPDATE sessions SET expires_at = ? WHERE token_hash = ?',
                      (int(time.time()) - 1,
                       self.auth._hash_token(token)))
        self.assertIsNone(self.auth.validate_session(token))

    def test_destroy_session_logout(self):
        token, _ = self.auth.login('said', _PW)
        self.auth.destroy_session(token)
        self.assertIsNone(self.auth.validate_session(token))

    def test_destroy_session_is_safe_with_no_token(self):
        self.auth.destroy_session(None)  # must not raise

    def test_bootstrap_flag_is_set_then_cleared(self):
        """This is what forces the password change, so the bootstrap value
        cannot outlive the chat it was pasted into."""
        token, _ = self.auth.login('said', _PW)
        self.assertTrue(self.auth.validate_session(token)['must_change'])
        self.auth.change_password('said', _PW, _NEW_PW)
        token2, _ = self.auth.login('said', _NEW_PW)
        self.assertFalse(self.auth.validate_session(token2)['must_change'])

    def test_purge_expired_removes_only_dead_sessions(self):
        live, _ = self.auth.login('said', _PW)
        dead, _ = self.auth.login('said', _PW)
        with self.auth._conn() as c:
            c.execute('UPDATE sessions SET expires_at = ? WHERE token_hash = ?',
                      (int(time.time()) - 1, self.auth._hash_token(dead)))
        self.assertEqual(self.auth.purge_expired(), 1)
        self.assertIsNotNone(self.auth.validate_session(live))

    def test_list_sessions_returns_current_users(self):
        self.auth.login('said', _PW, user_agent='Mozilla/5.0')
        rows = self.auth.list_sessions('said')
        self.assertTrue(rows)
        self.assertIn('Mozilla/5.0', rows[0]['user_agent'])


# ── password change ──────────────────────────────────────────────────────
class TestChangePassword(AuthTestCase):

    def setUp(self):
        super().setUp()
        self.auth.create_user('said', _PW)

    def test_change_revokes_existing_sessions(self):
        """A stolen cookie must not survive the owner changing their
        password."""
        stolen, _ = self.auth.login('said', _PW)
        ok, msg = self.auth.change_password('said', _PW, _NEW_PW)
        self.assertTrue(ok, msg)
        self.assertIsNone(self.auth.validate_session(stolen))

    def test_wrong_current_password_rejected(self):
        ok, msg = self.auth.change_password('said', 'wrong', _NEW_PW)
        self.assertFalse(ok)
        self.assertIn('current password', msg)

    def test_weak_new_password_rejected(self):
        ok, msg = self.auth.change_password('said', _PW, 'abc')
        self.assertFalse(ok)
        self.assertIn('8 characters', msg)

    def test_cannot_reuse_the_same_password(self):
        ok, msg = self.auth.change_password('said', _PW, _PW)
        self.assertFalse(ok)
        self.assertIn('differ', msg)

    def test_old_password_stops_working_after_change(self):
        self.auth.change_password('said', _PW, _NEW_PW)
        self.assertIsNone(self.auth.login('said', _PW)[0])
        self.assertIsNotNone(self.auth.login('said', _NEW_PW)[0])

    def test_unknown_user_cannot_change_password(self):
        ok, _ = self.auth.change_password('ghost', _PW, _NEW_PW)
        self.assertFalse(ok)


# ── audit log ────────────────────────────────────────────────────────────
class TestAudit(AuthTestCase):

    def test_events_are_recorded_without_secrets(self):
        self.auth.create_user('said', _PW)
        self.auth.login('said', 'wrong', ip='1.2.3.4')
        self.auth.login('said', _PW, ip='1.2.3.4')
        self.auth.change_password('said', _PW, _NEW_PW)
        self.auth.destroy_session(self.auth.login(_NEW_PW and 'said', _NEW_PW)[0])

        with sqlite3.connect(str(self.db)) as c:
            events = [r[0] for r in c.execute('SELECT event FROM audit')]
        for expected in ('user_created', 'login_failed', 'login_ok',
                         'password_changed', 'logout'):
            self.assertIn(expected, events)

    def test_audit_never_contains_a_password(self):
        self.auth.create_user('said', _PW)
        self.auth.login('said', 'wrong', ip='1.2.3.4')
        self.assertNotIn(_PW.encode(), self.db.read_bytes())


# ── server wiring ────────────────────────────────────────────────────────
class TestServerGate(AuthTestCase):
    """The gate lives in server.py's DashboardHandler; these assert the
    routing contract without needing a socket."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with patch.dict(os.environ, {'AUTH_DISABLED': '', 'DASHBOARD_ALLOWED_IPS': '127.0.0.1'}):
            sys.path.insert(0, str(_DASHBOARD))
            import server
        cls.server = server
        cls.H = server.DashboardHandler

    def test_auth_handlers_are_defined(self):
        for name in ('_handle_login', '_handle_logout', '_handle_session',
                     '_handle_change_password', '_require_auth', '_cookie',
                     '_is_secure', '_cors_origin'):
            self.assertTrue(hasattr(self.H, name), f'missing {name}')

    def test_verbs_call_the_auth_gate(self):
        """Every mutating and reading verb must pass through _require_auth, or
        one of them is a way around the login."""
        for verb in ('do_GET', 'do_POST', 'do_DELETE', 'do_HEAD'):
            src = inspect.getsource(getattr(self.H, verb))
            self.assertIn('_require_auth', src, f'{verb} does not gate on auth')
            self.assertIn('_check_client_ip', src, f'{verb} does not check the IP')

    def test_cors_is_same_origin_only(self):
        """A wildcard would let any site read authenticated JSON."""
        src = inspect.getsource(self.H._send_json)
        self.assertNotIn("'*'", src)
        self.assertIn('_cors_origin', src)

    def test_bootstrap_creates_user_when_none_exists(self):
        with patch.dict(os.environ, {'PSB_AUTH_PASSWORD': 'Fresh#2026x'}):
            self.server._bootstrap_auth()
        self.assertIsNotNone(self.auth.login('said', 'Fresh#2026x')[0])
        # First-boot accounts are flagged so the owner is forced to replace the
        # password that travelled through chat.
        self.assertTrue(self.auth.get_user('said')['must_change'])

    def test_bootstrap_never_overwrites_an_existing_password(self):
        """A redeploy sets PSB_AUTH_PASSWORD again in the environment. If boot
        re-applied it, the owner's chosen password would silently revert to a
        value they may have already forgotten -- and a stale session elsewhere
        would start working again."""
        self.auth.create_user('said', _PW, must_change=0)
        with patch.dict(os.environ, {'PSB_AUTH_PASSWORD': 'Hijack#2026x'}):
            self.server._bootstrap_auth()
        self.assertIsNone(self.auth.login('said', 'Hijack#2026x')[0])
        self.assertIsNotNone(self.auth.login('said', _PW)[0])


if __name__ == '__main__':
    unittest.main()
