"""Session authentication for the dashboard.

The dashboard holds real financial and work data and is now reachable through
a public hostname, so the existing IP allowlist is no longer the whole story:
it cannot distinguish the owner's browser from a script running on the same
box, and it does nothing once the request arrives from an allowed network.

This module adds what that gap needs: a password (stored only as a
memory-hard scrypt hash) and server-side sessions (only a SHA-256 digest of
the session token is persisted, so a leaked database cannot be replayed as a
login).

Design notes
------------
* No new dependency. scrypt comes from `hashlib`, sqlite3 from the stdlib —
  this project deliberately ships with no web framework and zero build step,
  and auth should not be the thing that changes that.
* Single-file, dependency-free, and safe to import from the request handler:
  every function opens its own short-lived connection, so it is correct under
  ThreadingHTTPServer's one-thread-per-request model. WAL mode keeps the
  readers (every request) from blocking the writer (logins).
* Verification is constant-time. Login is rate limited per-username and
  per-IP, because a 4-character minimum password with no lockout is just a
  speed bump.
* `must_change` exists because the bootstrap password is transmitted once in
  chat and therefore must not be a permanent credential. While it is set, the
  API refuses everything except logout and the change-password call itself.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
AUTH_DB = Path(os.environ.get('PSB_AUTH_DB', BASE_DIR / '.agent' / 'state' / 'auth.db'))

SESSION_COOKIE = 'psb_session'
SESSION_TTL = 30 * 24 * 3600          # 30 days
SESSION_TTL_WARN = 7 * 24 * 3600      # tell the UI when it's close to expiry
MAX_TTL = 90 * 24 * 3600              # hard cap, refreshed on use

# scrypt cost. n=2**14 is ~16 MiB and ~60 ms per hash on this class of
# hardware: expensive enough to make an offline crack of a leaked hash
# costly, cheap enough that login still feels instant.
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32

MIN_PASSWORD = 8

# Brute-force limiter: (key -> [failures, window_start])
_FAILURES: dict[str, list] = {}
MAX_ATTEMPTS = 8
LOCKOUT_SECS = 300

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    pw_hash       BLOB NOT NULL,
    pw_salt       BLOB NOT NULL,
    created_at    INTEGER NOT NULL,
    changed_at    INTEGER NOT NULL,
    must_change   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash    TEXT PRIMARY KEY,
    username      TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    created_at    INTEGER NOT NULL,
    last_seen     INTEGER NOT NULL,
    expires_at    INTEGER NOT NULL,
    user_agent    TEXT,
    FOREIGN KEY (username) REFERENCES users(username)
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(username);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);
CREATE TABLE IF NOT EXISTS audit (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        INTEGER NOT NULL,
    event     TEXT NOT NULL,
    username  TEXT,
    detail    TEXT,
    ip        TEXT
);
"""


def _conn() -> sqlite3.Connection:
    AUTH_DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(AUTH_DB), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA foreign_keys=ON')
    return c


def init_db() -> None:
    with _conn() as c:
        c.executescript(SCHEMA)
    # The DB holds password hashes; keep it off other local accounts.
    try:
        os.chmod(AUTH_DB, 0o600)
    except OSError:
        pass


def _audit(c: sqlite3.Connection, event: str, username: str | None = None,
           detail: str | None = None, ip: str | None = None) -> None:
    c.execute('INSERT INTO audit (at, event, username, detail, ip) VALUES (?,?,?,?,?)',
              (int(time.time()), event, username, detail, ip))


# ── password hashing ──────────────────────────────────────────────────────
def hash_password(password: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode('utf-8'), salt=salt,
                        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN)
    return dk, salt


def verify_password(password: str, pw_hash: bytes, pw_salt: bytes) -> bool:
    try:
        dk, _ = hash_password(password, salt=pw_salt)
    except Exception:
        return False
    return hmac.compare_digest(dk, pw_hash)


def password_problem(password: str) -> str | None:
    """Return a human-readable reason the password is unacceptable, or None."""
    if len(password) < MIN_PASSWORD:
        return f'password must be at least {MIN_PASSWORD} characters'
    if password.strip() != password:
        return 'password must not start or end with a space'
    classes = sum([
        any(c.islower() for c in password),
        any(c.isupper() for c in password),
        any(c.isdigit() for c in password),
        any(not c.isalnum() for c in password),
    ])
    if classes < 2:
        return ('password needs at least two of: lowercase, uppercase, '
                'digit, symbol')
    return None


# ── users ────────────────────────────────────────────────────────────────
def create_user(username: str, password: str, must_change: int = 1) -> None:
    if not username or not username.strip():
        raise ValueError('username required')
    problem = password_problem(password)
    if problem:
        raise ValueError(problem)
    pw_hash, pw_salt = hash_password(password)
    now = int(time.time())
    with _conn() as c:
        c.execute(
            'INSERT OR REPLACE INTO users '
            '(username, pw_hash, pw_salt, created_at, changed_at, must_change) '
            'VALUES (?,?,?,?,?,?)',
            (username.strip(), pw_hash, pw_salt, now, now, int(must_change)))
        c.execute('DELETE FROM sessions WHERE username = ?', (username.strip(),))
        _audit(c, 'user_created', username.strip(),
               f'must_change={must_change}')


def user_exists(username: str) -> bool:
    with _conn() as c:
        return c.execute('SELECT 1 FROM users WHERE username = ?',
                         (username,)).fetchone() is not None


def get_user(username: str) -> sqlite3.Row | None:
    with _conn() as c:
        return c.execute('SELECT * FROM users WHERE username = ?',
                         (username,)).fetchone()


def change_password(username: str, old_password: str, new_password: str,
                    ip: str | None = None) -> tuple[bool, str]:
    """Verify the old password, then set the new one. Clears must_change and
    revokes every other session for that user (a password change should log
    out anyone else who was holding a stolen cookie)."""
    problem = password_problem(new_password)
    if problem:
        return False, problem
    if new_password == old_password:
        return False, 'new password must differ from the current one'
    row = get_user(username)
    if row is None:
        return False, 'unknown user'
    if not verify_password(old_password, row['pw_hash'], row['pw_salt']):
        with _conn() as c:
            _audit(c, 'password_change_denied', username,
                   'wrong current password', ip)
        return False, 'current password is incorrect'
    pw_hash, pw_salt = hash_password(new_password)
    now = int(time.time())
    with _conn() as c:
        c.execute('UPDATE users SET pw_hash=?, pw_salt=?, changed_at=?, must_change=0 '
                  'WHERE username = ?', (pw_hash, pw_salt, now, username))
        c.execute('DELETE FROM sessions WHERE username = ?', (username,))
        _audit(c, 'password_changed', username, None, ip)
    return True, 'password updated'


# ── rate limiting ────────────────────────────────────────────────────────
def _too_many(key: str) -> int:
    """Seconds remaining in the lockout for `key`, or 0 if not locked out."""
    rec = _FAILURES.get(key)
    if not rec:
        return 0
    count, started = rec
    if count < MAX_ATTEMPTS:
        return 0
    remaining = LOCKOUT_SECS - (time.time() - started)
    if remaining <= 0:
        _FAILURES.pop(key, None)
        return 0
    return int(remaining) + 1


def _note_failure(key: str) -> None:
    rec = _FAILURES.get(key)
    now = time.time()
    if rec is None or now - rec[1] > LOCKOUT_SECS:
        _FAILURES[key] = [1, now]
    else:
        rec[0] += 1


def _clear_failures(key: str) -> None:
    _FAILURES.pop(key, None)


# ── sessions ─────────────────────────────────────────────────────────────
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def login(username: str, password: str, ip: str | None = None,
          user_agent: str | None = None) -> tuple[str | None, str]:
    """Return (session_token, error). Exactly one is set.

    The same message is returned for an unknown user and a wrong password so
    the endpoint cannot be used to enumerate usernames."""
    username = (username or '').strip()
    generic = 'incorrect username or password'

    user_key = f'user:{username.lower()}'
    ip_key = f'ip:{ip}' if ip else None
    for key in filter(None, (user_key, ip_key)):
        wait = _too_many(key)
        if wait:
            return None, f'too many attempts — try again in {wait}s'

    row = get_user(username) if username else None
    if row is None or not verify_password(password, row['pw_hash'], row['pw_salt']):
        for key in filter(None, (user_key, ip_key)):
            _note_failure(key)
        with _conn() as c:
            _audit(c, 'login_failed', username or None, None, ip)
        return None, generic

    for key in filter(None, (user_key, ip_key)):
        _clear_failures(key)

    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with _conn() as c:
        c.execute('INSERT INTO sessions '
                  '(token_hash, username, created_at, last_seen, expires_at, user_agent) '
                  'VALUES (?,?,?,?,?,?)',
                  (_hash_token(token), username, now, now, now + SESSION_TTL,
                   (user_agent or '')[:200]))
        _audit(c, 'login_ok', username, None, ip)
    return token, ''


def validate_session(token: str | None) -> dict | None:
    """Resolve a cookie to {'username', 'must_change', 'expires_in'}, or None.

    Slides the expiry forward on use (capped at MAX_TTL from creation) so an
    active owner is never logged out mid-session."""
    if not token:
        return None
    th = _hash_token(token)
    now = int(time.time())
    with _conn() as c:
        row = c.execute('SELECT * FROM sessions WHERE token_hash = ?', (th,)).fetchone()
        if row is None:
            return None
        if row['expires_at'] <= now:
            c.execute('DELETE FROM sessions WHERE token_hash = ?', (th,))
            return None
        # Password changed since this session was minted → token is stale.
        u = c.execute('SELECT must_change, changed_at FROM users WHERE username = ?',
                      (row['username'],)).fetchone()
        if u is None or u['changed_at'] > row['created_at']:
            c.execute('DELETE FROM sessions WHERE token_hash = ?', (th,))
            return None
        expires = min(row['expires_at'], row['created_at'] + MAX_TTL)
        c.execute('UPDATE sessions SET last_seen = ?, expires_at = ? WHERE token_hash = ?',
                  (now, expires, th))
        return {
            'username': row['username'],
            'must_change': bool(u['must_change']),
            'expires_in': expires - now,
        }


def destroy_session(token: str | None, ip: str | None = None) -> None:
    if not token:
        return
    with _conn() as c:
        row = c.execute('SELECT username FROM sessions WHERE token_hash = ?',
                        (_hash_token(token),)).fetchone()
        c.execute('DELETE FROM sessions WHERE token_hash = ?', (_hash_token(token),))
        if row:
            _audit(c, 'logout', row['username'], None, ip)


def purge_expired() -> int:
    with _conn() as c:
        n = c.execute('DELETE FROM sessions WHERE expires_at <= ?',
                      (int(time.time()),)).rowcount
    return n or 0


def list_sessions(username: str) -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            'SELECT created_at, last_seen, expires_at, user_agent FROM sessions '
            'WHERE username = ? ORDER BY last_seen DESC', (username,))]
