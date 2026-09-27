#!/usr/bin/env python3
"""
Tasks Agent - Trello "assigned to you" tasks harvested from Gmail.

There's no direct Trello API connection yet (key/token not configured), so
this reads the notification emails Trello already sends when a card gets
assigned - "<person> added you to the card <card> on <board>" - via the
existing gmail-connector skill (workspace 'catalyze', where these land).
Manual refresh only, per the owner - no background poller, no scheduled
Gmail polling.

Once a Trello API key+token exist (.agent/skills/trello-connector/token.env)
this can be upgraded to pull the real card description/checklist/comments
too; today the card name + board + a direct link is all the notification
email carries (Trello's own notification template has no description
field).

The dashboard server imports this module and delegates all /api/tasks*
traffic here.
"""

import html
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

WIB = timezone(timedelta(hours=7))

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = BASE_DIR / 'journal' / 'state'
TASKS_PATH = STATE_DIR / 'trello_tasks.json'
GMAIL_SCRIPT = BASE_DIR / '.agent' / 'skills' / 'gmail-connector' / 'gmail_manager.py'

# Trello assignment notifications land in the Catalyze mailbox.
GMAIL_WORKSPACE = 'catalyze'
# Narrows the Gmail search cheaply; the regex below on the subject/body is
# what actually decides "is this a card assignment" (Gmail's search also
# matches digest emails that merely mention the phrase in a snippet).
GMAIL_QUERY = 'from:trello.com "added you to the card"'
GMAIL_LIMIT = 40

# "<person> added you to the card <card> (<card_url>) on <board> (<board_url>)"
# The plain-text body puts this sentence on its OWN line (blank lines before
# the "Reply via email" footer and after the "Here's what you missed"
# preamble), so matching line-by-line - rather than across the whole body -
# keeps the footer/preamble out of the captured groups. The subject line has
# the same sentence but never the URLs.
_ASSIGN_LINE_RE = re.compile(
    r'^(?P<person>.+?) added you to the card (?P<card>.+?)'
    r'(?:\s*\((?P<card_url>https://trello\.com/c/[^\)]+)\))? on '
    r'(?P<board>.+?)(?:\s*\((?P<board_url>https://trello\.com/b/[^\)]+)\))?$',
    re.IGNORECASE,
)


def _log(msg):
    try:
        sys.stderr.write(f"[tasks] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _now_iso():
    return datetime.now(WIB).isoformat(timespec='seconds')


def _sort_key(task):
    """RFC 2822 date strings ("Wed, 29 Oct 2025 ...") don't sort correctly
    as plain strings (the weekday name dominates the comparison) - parse to
    an actual timestamp; unparseable/missing dates sort last."""
    d = task.get('date')
    if not d:
        return 0.0
    try:
        return parsedate_to_datetime(d).timestamp()
    except Exception:
        return 0.0


def _run_gmail(args, timeout=60):
    """Shell out to the existing gmail-connector CLI in --json mode. Reuses
    its auth/token-refresh logic instead of re-implementing OAuth here."""
    cmd = [sys.executable, str(GMAIL_SCRIPT), '--workspace', GMAIL_WORKSPACE, '--json'] + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    out = (r.stdout or '').strip()
    if not out:
        raise RuntimeError((r.stderr or 'gmail_manager.py produced no output').strip()[:500])
    try:
        data = json.loads(out.splitlines()[-1])
    except Exception as e:
        raise RuntimeError(f'could not parse gmail_manager.py output: {e}')
    if isinstance(data, dict) and data.get('error'):
        raise RuntimeError(data['error'])
    return data


def _load():
    if not TASKS_PATH.exists():
        return {'tasks': [], 'last_refreshed': None}
    try:
        return json.loads(TASKS_PATH.read_text(encoding='utf-8'))
    except Exception:
        return {'tasks': [], 'last_refreshed': None}


def _save(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = str(TASKS_PATH) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, str(TASKS_PATH))


def _norm(s):
    return re.sub(r'[^a-z0-9]+', '', (s or '').lower())


def _match_repo(board_name, repo_names):
    """Fuzzy-match a Trello board name to a repo folder name. Exact
    normalized match first, then a token-overlap score; empty (None) if
    nothing is confident enough - the owner categorizes those manually."""
    if not board_name or not repo_names:
        return None
    exact = {_norm(r): r for r in repo_names}
    nb = _norm(board_name)
    if nb in exact:
        return exact[nb]

    board_tokens = {t for t in re.findall(r'[a-z0-9]+', board_name.lower()) if len(t) > 2}
    if not board_tokens:
        return None

    best, best_score = None, 0.0
    for r in repo_names:
        repo_tokens = {t for t in re.findall(r'[a-z0-9]+', r.lower()) if len(t) > 2}
        overlap = board_tokens & repo_tokens
        if not overlap:
            continue
        score = len(overlap) / min(len(board_tokens), len(repo_tokens))
        if score > best_score:
            best, best_score = r, score
    return best if best_score >= 0.6 else None


def _repo_names():
    try:
        import coding_agent
        return [r['name'] for r in coding_agent._scan_repos().get('repos', [])]
    except Exception as e:
        _log(f"could not read repo list: {e}")
        return []


def _looks_like_assignment(subject):
    """Cheap pre-filter on the subject alone, before spending a Gmail 'get'
    call: the Gmail search query already narrows to this phrase, but it
    matches loosely (e.g. digest emails that merely mention it), so confirm
    the exact substring is really there."""
    return ' added you to the card ' in (subject or '').lower()


def _parse_assignment_line(text):
    """Scan a body (or subject) line by line for the assignment sentence and
    return its match groups as a dict, or None. Matching one line at a time
    - instead of the whole flattened body - keeps the "Here's what you
    missed" preamble and the "Reply via email" footer out of the captured
    groups (see _ASSIGN_LINE_RE)."""
    for raw_line in (text or '').replace('\r\n', '\n').split('\n'):
        line = raw_line.strip()
        if ' added you to the card ' not in line.lower():
            continue
        m = _ASSIGN_LINE_RE.match(line)
        if m:
            return m.groupdict()
    return None


def refresh_tasks():
    """Manual pull: scan Gmail for 'added you to the card' notifications,
    merge into the cached list (existing repo picks / dismissed status are
    preserved), save, and return the merged state."""
    state = _load()
    by_id = {t['id']: t for t in state.get('tasks', [])}

    listed = _run_gmail(['list', '--query', GMAIL_QUERY, '--limit', str(GMAIL_LIMIT)])
    repo_names = _repo_names()
    new_count = 0

    for row in listed.get('messages', []):
        msg_id = row.get('id')
        if not msg_id or msg_id in by_id:
            continue  # already harvested - don't re-fetch the body

        subj = row.get('subject') or ''
        if not _looks_like_assignment(subj):
            continue  # a digest, a "moved the card", etc. - not this signal

        # The subject never carries the trello.com links - always fetch the
        # body, which repeats the same sentence WITH them inline.
        try:
            full = _run_gmail(['get', msg_id])
        except Exception as e:
            _log(f"get {msg_id} failed: {e}")
            continue
        groups = _parse_assignment_line(full.get('body')) or _parse_assignment_line(subj)
        if not groups or not groups.get('card'):
            continue

        board = html.unescape((groups.get('board') or '').strip())
        task = {
            'id': msg_id,
            'person': html.unescape((groups.get('person') or '').strip()),
            'card': html.unescape((groups.get('card') or '').strip()),
            'card_url': groups.get('card_url'),
            'board': board,
            'board_url': groups.get('board_url'),
            'date': row.get('date'),
            'repo': _match_repo(board, repo_names),
            'repo_manual': False,
            'status': 'open',
        }
        by_id[msg_id] = task
        new_count += 1

    tasks = sorted(by_id.values(), key=_sort_key, reverse=True)
    state = {'tasks': tasks, 'last_refreshed': _now_iso()}
    _save(state)
    _log(f"refreshed: {new_count} new, {len(tasks)} total")
    return state


def _mutate(task_id, fn):
    state = _load()
    found = False
    for t in state.get('tasks', []):
        if t['id'] == task_id:
            fn(t)
            found = True
            break
    if not found:
        raise ValueError('task not found')
    _save(state)
    return state


def set_repo(task_id, repo):
    def _apply(t):
        t['repo'] = repo or None
        t['repo_manual'] = True
    return _mutate(task_id, _apply)


def set_status(task_id, status):
    if status not in ('open', 'dismissed'):
        raise ValueError("status must be 'open' or 'dismissed'")

    def _apply(t):
        t['status'] = status
    return _mutate(task_id, _apply)


def get_view():
    state = _load()
    return {
        'tasks': state.get('tasks', []),
        'last_refreshed': state.get('last_refreshed'),
        'repos': _repo_names(),
    }


def _read_json(handler):
    try:
        length = int(handler.headers.get('Content-Length', 0))
        if length <= 0:
            return {}
        return json.loads(handler.rfile.read(length).decode('utf-8'))
    except Exception:
        return {}


# ── HTTP routing hook (called from server.py handlers) ─────────────────
def route_get(handler):
    path = handler.path.split('?', 1)[0]
    if path == '/api/tasks':
        handler._send_json(200, json.dumps(get_view(), ensure_ascii=False))
        return True
    return False


def route_post(handler):
    path = handler.path.split('?', 1)[0]
    if path == '/api/tasks/refresh':
        try:
            handler._send_json(200, json.dumps(refresh_tasks(), ensure_ascii=False))
        except Exception as e:
            handler._send_json(502, json.dumps({'error': str(e)}))
        return True
    if path.startswith('/api/tasks/'):
        rest = path[len('/api/tasks/'):].split('/')
        task_id = rest[0]
        sub = rest[1] if len(rest) > 1 else None
        body = _read_json(handler)
        try:
            if sub == 'repo':
                result = set_repo(task_id, (body or {}).get('repo'))
                handler._send_json(200, json.dumps(result, ensure_ascii=False))
            elif sub == 'dismiss':
                result = set_status(task_id, 'dismissed')
                handler._send_json(200, json.dumps(result, ensure_ascii=False))
            elif sub == 'reopen':
                result = set_status(task_id, 'open')
                handler._send_json(200, json.dumps(result, ensure_ascii=False))
            else:
                handler._send_json(404, json.dumps({'error': 'not found'}))
        except ValueError as e:
            handler._send_json(404, json.dumps({'error': str(e)}))
        except Exception as e:
            handler._send_json(500, json.dumps({'error': str(e)}))
        return True
    return False
