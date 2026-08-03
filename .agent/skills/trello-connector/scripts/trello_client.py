#!/usr/bin/env python3
"""
Trello Connector
List boards, lists, cards, and manage cards via Trello REST API.

Auth: credentials resolved via workspace_resolver (workspace-specific trello.env).
API:  https://api.trello.com/1/...
"""
import argparse
import json
import os
import signal
import sys
import time
import urllib.request
import urllib.parse
import urllib.error

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SKILL_DIR, '..', '..', '..', '..'))

sys.path.insert(0, os.path.join(REPO_ROOT, '.agent', 'workspaces'))
sys.path.insert(0, os.path.join(REPO_ROOT, '.agent', 'scripts'))

import workspace_resolver as ws
from file_utils import require_send_approval

# UTF-8 output on Windows
try:
    if sys.stdout.encoding != 'utf-8':
        sys.stdout.reconfigure(encoding='utf-8')
    if sys.stderr.encoding != 'utf-8':
        sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

DEFAULT_TIMEOUT = 30
BASE_URL = 'https://api.trello.com/1'

# Global 180s timeout (Unix only)
def _timeout_handler(signum, frame):
    print("[ERROR] Trello Connector timed out after 180 seconds", file=sys.stderr)
    sys.exit(1)

if os.name != 'nt':
    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(180)

# ---------- workspace-aware credentials ----------

_current_workspace = None


def get_current_workspace():
    """Return the resolved workspace name for this invocation."""
    return _current_workspace


def get_workspace_config():
    """Return the full WorkspaceContext for the current invocation."""
    return ws.get(_current_workspace)


def load_credentials(workspace_name=None):
    """Load TRELLO_API_KEY and TRELLO_TOKEN from workspace trello.env."""
    global _current_workspace
    ctx = ws.get(workspace_name)
    _current_workspace = ctx.name

    env = ctx.load_env('trello')
    creds = {
        'key': env.get('TRELLO_API_KEY', ''),
        'token': env.get('TRELLO_TOKEN', ''),
    }

    # Fallback: old location
    if not creds['key'] or not creds['token']:
        old_path = os.path.join(REPO_ROOT, '.agent', 'skills', 'trello-connector', 'token.env')
        if os.path.exists(old_path):
            with open(old_path, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#') or '=' not in line:
                        continue
                    k, v = line.split('=', 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    if k == 'TRELLO_API_KEY' and not creds['key']:
                        creds['key'] = v
                    elif k == 'TRELLO_TOKEN' and not creds['token']:
                        creds['token'] = v

    return creds


def require_creds(creds):
    if not creds['key']:
        print(f"Error: TRELLO_API_KEY not set for workspace '{_current_workspace}'.", file=sys.stderr)
        print(f"Add it to: .agent/workspaces/{_current_workspace}/trello.env", file=sys.stderr)
        sys.exit(1)
    if not creds['token']:
        print(f"Error: TRELLO_TOKEN not set for workspace '{_current_workspace}'.", file=sys.stderr)
        print(f"Add it to: .agent/workspaces/{_current_workspace}/trello.env", file=sys.stderr)
        sys.exit(1)

# ---------- HTTP ----------

def api_get(creds, path, params=None):
    auth_params = {'key': creds['key'], 'token': creds['token']}
    if params:
        auth_params.update(params)
    url = f"{BASE_URL}{path}?" + urllib.parse.urlencode(auth_params)

    req = urllib.request.Request(url)
    print(f"[DEBUG] [{_current_workspace}] GET {path}", file=sys.stderr)
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            time.sleep(5)
            return api_get(creds, path, params)
        detail = ''
        try:
            detail = e.read().decode('utf-8')[:300]
        except Exception:
            pass
        print(f"[ERROR] HTTP {e.code} on GET {path}: {detail}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"[ERROR] Cannot reach Trello: {e.reason}", file=sys.stderr)
        sys.exit(1)


def api_post(creds, path, params=None):
    auth_params = {'key': creds['key'], 'token': creds['token']}
    if params:
        auth_params.update(params)
    url = f"{BASE_URL}{path}"
    data = urllib.parse.urlencode(auth_params).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='POST')

    print(f"[DEBUG] [{_current_workspace}] POST {path}", file=sys.stderr)
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode('utf-8')[:300]
        except Exception:
            pass
        print(f"[ERROR] HTTP {e.code} on POST {path}: {detail}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"[ERROR] Cannot reach Trello: {e.reason}", file=sys.stderr)
        sys.exit(1)


def api_put(creds, path, params=None):
    auth_params = {'key': creds['key'], 'token': creds['token']}
    if params:
        auth_params.update(params)
    url = f"{BASE_URL}{path}"
    data = urllib.parse.urlencode(auth_params).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='PUT')

    print(f"[DEBUG] [{_current_workspace}] PUT {path}", file=sys.stderr)
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode('utf-8')[:300]
        except Exception:
            pass
        print(f"[ERROR] HTTP {e.code} on PUT {path}: {detail}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"[ERROR] Cannot reach Trello: {e.reason}", file=sys.stderr)
        sys.exit(1)

# ---------- actions ----------

def list_boards(creds):
    require_creds(creds)
    boards = api_get(creds, '/members/me/boards', params={'filter': 'open'})
    print(f"[{_current_workspace}] Found {len(boards)} board(s):")
    for b in boards:
        print(f"  {b.get('name')} - ID: {b['id']}")


def list_lists(creds, board_id):
    require_creds(creds)
    lists = api_get(creds, f'/boards/{board_id}/lists', params={'filter': 'open'})
    print(f"[{_current_workspace}] Found {len(lists)} list(s):")
    for l in lists:
        print(f"  {l.get('name')} - ID: {l['id']}")


def list_cards(creds, board_id=None, list_id=None):
    require_creds(creds)
    if list_id:
        cards = api_get(creds, f'/lists/{list_id}/cards')
    elif board_id:
        cards = api_get(creds, f'/boards/{board_id}/cards', params={'filter': 'open'})
    else:
        print("Error: need --board-id or --list-id", file=sys.stderr)
        sys.exit(1)

    print(f"[{_current_workspace}] Found {len(cards)} card(s):")
    for c in cards:
        labels = ', '.join(l.get('name', l.get('color', '')) for l in c.get('labels', []))
        due = c.get('due', '')
        due_str = f"  due: {due[:10]}" if due else ''
        print(f"  {c.get('name')}")
        print(f"       ID: {c['id']}  labels: {labels or '-'}{due_str}")


def get_card(creds, card_id):
    require_creds(creds)
    card = api_get(creds, f'/cards/{card_id}',
                   params={'members': 'true', 'member_fields': 'fullName,username'})
    print(f"Card: {card.get('name')}")
    print(f"  ID:      {card['id']}")
    print(f"  Board:   {card.get('idBoard')}")
    print(f"  List:    {card.get('idList')}")
    print(f"  URL:     {card.get('url')}")
    labels = ', '.join(l.get('name', l.get('color', '')) for l in card.get('labels', []))
    print(f"  Labels:  {labels or '-'}")
    due = card.get('due')
    print(f"  Due:     {due[:10] if due else '-'}")
    members = card.get('members', [])
    member_names = ', '.join(f"@{m.get('username', m.get('fullName', '?'))}" for m in members)
    print(f"  Members: {member_names or '-'}")
    desc = card.get('desc', '')
    if desc:
        print(f"\n--- Description ---\n{desc[:2000]}")


def my_cards(creds):
    require_creds(creds)
    cards = api_get(creds, '/members/me/cards', params={'filter': 'open'})
    print(f"[{_current_workspace}] Found {len(cards)} card(s) assigned to you:")
    for c in cards:
        due = c.get('due')
        due_str = f"  due: {due[:10]}" if due else ''
        print(f"  {c.get('name')}")
        print(f"       ID: {c['id']}{due_str}")


def move_card(creds, card_id, list_id, approved=False):
    require_send_approval('move Trello card', approved)
    require_creds(creds)
    result = api_put(creds, f'/cards/{card_id}', params={'idList': list_id})
    print(f"[{_current_workspace}] Card moved: {result.get('name')} -> list {list_id}")


def add_comment(creds, card_id, text, approved=False):
    require_send_approval('comment on Trello card', approved)
    require_creds(creds)
    result = api_post(creds, f'/cards/{card_id}/actions/comments', params={'text': text})
    print(f"[{_current_workspace}] Comment added to card {card_id}")

# ---------- CLI ----------

def main():
    p = argparse.ArgumentParser(description='Trello Connector')
    p.add_argument('--action', required=True,
                   choices=['list_boards', 'list_lists', 'list_cards',
                            'get_card', 'my_cards', 'move_card', 'comment'],
                   help='Action to perform')
    p.add_argument('--workspace', default=None,
                   help='Workspace name (default: active workspace)')
    p.add_argument('--board-id', dest='board_id', help='Trello board ID')
    p.add_argument('--list-id', dest='list_id', help='Trello list ID')
    p.add_argument('--card-id', dest='card_id', help='Trello card ID')
    p.add_argument('--text', help='Comment text')
    p.add_argument('--approved', action='store_true')
    args = p.parse_args()

    creds = load_credentials(args.workspace)

    if args.action == 'list_boards':
        list_boards(creds)
    elif args.action == 'list_lists':
        if not args.board_id:
            p.error('--board-id is required')
        list_lists(creds, args.board_id)
    elif args.action == 'list_cards':
        if not args.board_id and not args.list_id:
            p.error('--board-id or --list-id is required')
        list_cards(creds, board_id=args.board_id, list_id=args.list_id)
    elif args.action == 'get_card':
        if not args.card_id:
            p.error('--card-id is required')
        get_card(creds, args.card_id)
    elif args.action == 'my_cards':
        my_cards(creds)
    elif args.action == 'move_card':
        if not args.card_id or not args.list_id:
            p.error('--card-id and --list-id are required')
        move_card(creds, args.card_id, args.list_id, approved=args.approved)
    elif args.action == 'comment':
        if not args.card_id or not args.text:
            p.error('--card-id and --text are required')
        add_comment(creds, args.card_id, args.text, approved=args.approved)


if __name__ == '__main__':
    main()
