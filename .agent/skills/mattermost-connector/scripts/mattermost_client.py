#!/usr/bin/env python3
"""
Mattermost Connector
Read channels, message history, search, and post messages in a Mattermost workspace.

Auth: credentials resolved via workspace_resolver (workspace-specific mattermost.env).
API:  Mattermost REST API v4
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

DEFAULT_TIMEOUT = 60
MAX_RETRIES = 3

# Global 180s timeout (Unix only)
def _timeout_handler(signum, frame):
    print("[ERROR] Mattermost Connector timed out after 180 seconds", file=sys.stderr)
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
    """Load MATTERMOST_URL, MATTERMOST_TOKEN, MATTERMOST_TEAM_ID from workspace."""
    global _current_workspace
    ctx = ws.get(workspace_name)
    _current_workspace = ctx.name

    # Load from workspace mattermost.env
    env = ctx.load_env('mattermost')
    creds = {
        'url': env.get('MATTERMOST_URL', '').rstrip('/'),
        'token': env.get('MATTERMOST_TOKEN', ''),
        'team_id': env.get('MATTERMOST_TEAM_ID', ''),
    }

    # Fallback: old location (backward compat)
    if not creds['url'] or not creds['token']:
        old_path = os.path.join(REPO_ROOT, '.agent', 'skills', 'mattermost-connector', 'token.env')
        if os.path.exists(old_path):
            with open(old_path, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#') or '=' not in line:
                        continue
                    k, v = line.split('=', 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    if k == 'MATTERMOST_URL' and not creds['url']:
                        creds['url'] = v.rstrip('/')
                    elif k == 'MATTERMOST_TOKEN' and not creds['token']:
                        creds['token'] = v
                    elif k == 'MATTERMOST_TEAM_ID' and not creds['team_id']:
                        creds['team_id'] = v

    return creds


def require_credentials(creds, need_team=True):
    if not creds['url']:
        print(f"Error: MATTERMOST_URL not set for workspace '{_current_workspace}'.", file=sys.stderr)
        print(f"Add it to: .agent/workspaces/{_current_workspace}/mattermost.env", file=sys.stderr)
        sys.exit(1)
    if not creds['token']:
        print(f"Error: MATTERMOST_TOKEN not set for workspace '{_current_workspace}'.", file=sys.stderr)
        print(f"Add it to: .agent/workspaces/{_current_workspace}/mattermost.env", file=sys.stderr)
        sys.exit(1)
    if need_team and not creds['team_id']:
        print(f"Error: MATTERMOST_TEAM_ID not set. Run --action list_teams to find yours.", file=sys.stderr)
        sys.exit(1)

# ---------- HTTP ----------

def api(creds, method, path, params=None, body=None, retry=0):
    url = f"{creds['url']}/api/v4{path}"
    if params:
        url += '?' + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})

    headers = {
        'Authorization': f"Bearer {creds['token']}",
        'Content-Type': 'application/json',
    }
    data = json.dumps(body).encode('utf-8') if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    print(f"[DEBUG] [{_current_workspace}] {method} {path}", file=sys.stderr)
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        if e.code == 429 and retry < MAX_RETRIES:
            wait = int(e.headers.get('X-RateLimit-Reset', 2))
            print(f"[WARN] Rate limited, waiting {wait}s...", file=sys.stderr)
            time.sleep(wait)
            return api(creds, method, path, params, body, retry + 1)
        detail = ''
        try:
            detail = e.read().decode('utf-8')[:300]
        except Exception:
            pass
        print(f"[ERROR] HTTP {e.code} on {method} {path}: {detail}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"[ERROR] Cannot reach {creds['url']}: {e.reason}", file=sys.stderr)
        sys.exit(1)


def get(creds, path, params=None):
    return api(creds, 'GET', path, params=params)


def post_api(creds, path, body):
    return api(creds, 'POST', path, body=body)

# ---------- actions ----------

def list_teams(creds):
    require_credentials(creds, need_team=False)
    teams = get(creds, '/teams')
    if isinstance(teams, list):
        print(f"[{_current_workspace}] Found {len(teams)} team(s):")
        for t in teams:
            print(f"  {t.get('display_name', t.get('name'))} - ID: {t['id']}")
    else:
        print(json.dumps(teams, indent=2))


def list_channels(creds, include_private=False):
    require_credentials(creds)
    page, per_page = 0, 100
    all_channels = []
    while True:
        ch = get(creds, f"/teams/{creds['team_id']}/channels",
                 params={'page': page, 'per_page': per_page})
        if not isinstance(ch, list) or not ch:
            break
        all_channels.extend(ch)
        if len(ch) < per_page:
            break
        page += 1

    filtered = [c for c in all_channels
                if c.get('type') == 'O' or (include_private and c.get('type') == 'P')]
    print(f"[{_current_workspace}] Found {len(filtered)} channel(s):")
    for c in filtered:
        ctype = '[private]' if c.get('type') == 'P' else ''
        print(f"  #{c.get('display_name', c.get('name'))} - ID: {c['id']} {ctype}")


def list_dms(creds):
    require_credentials(creds, need_team=False)
    me = get(creds, '/users/me')
    uid = me.get('id')
    if not uid:
        print("[ERROR] Could not resolve current user.", file=sys.stderr)
        sys.exit(1)

    page, per_page = 0, 60
    all_dms = []
    while True:
        ch = get(creds, f'/users/{uid}/channels',
                 params={'page': page, 'per_page': per_page})
        if not isinstance(ch, list) or not ch:
            break
        dms = [c for c in ch if c.get('type') in ('D', 'G')]
        all_dms.extend(dms)
        if len(ch) < per_page:
            break
        page += 1

    print(f"[{_current_workspace}] Found {len(all_dms)} DM/group channel(s):")
    for c in all_dms:
        label = c.get('display_name') or c.get('name') or c['id']
        print(f"  {label} - ID: {c['id']} ({'DM' if c.get('type') == 'D' else 'Group'})")


def get_history(creds, channel_id, limit=20):
    require_credentials(creds, need_team=False)
    result = get(creds, f'/channels/{channel_id}/posts',
                 params={'per_page': limit, 'page': 0})
    order = result.get('order', [])
    posts = result.get('posts', {})
    print(f"[{_current_workspace}] Last {len(order)} message(s) in {channel_id}:")
    for pid in order:
        p = posts.get(pid, {})
        uid = p.get('user_id', 'unknown')
        text = (p.get('message') or '').replace('\n', ' ')
        if len(text) > 120:
            text = text[:120] + '...'
        ts = p.get('create_at', 0) / 1000
        from datetime import datetime
        dt = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')
        print(f"  [{dt}] {uid}: {text}")


def search_messages(creds, query):
    require_credentials(creds)
    result = post_api(creds, f'/teams/{creds["team_id"]}/posts/search',
                      {'terms': query, 'is_or_search': False})
    order = result.get('order', [])
    posts = result.get('posts', {})
    print(f"[{_current_workspace}] Found {len(order)} result(s) for '{query}':")
    for pid in order:
        p = posts.get(pid, {})
        uid = p.get('user_id', 'unknown')
        text = (p.get('message') or '').replace('\n', ' ')
        if len(text) > 120:
            text = text[:120] + '...'
        print(f"  [{p.get('channel_id', '')}] {uid}: {text}")


def get_user_info(creds, user_ref):
    require_credentials(creds, need_team=False)
    if user_ref.startswith('@'):
        result = get(creds, f'/users/username/{user_ref[1:]}')
    else:
        result = get(creds, f'/users/{user_ref}')
    if 'id' not in result:
        print(f"[ERROR] User not found: {user_ref}", file=sys.stderr)
        sys.exit(1)
    print(f"ID:       {result['id']}")
    print(f"Username: @{result.get('username', '')}")
    print(f"Name:     {result.get('first_name', '')} {result.get('last_name', '')}".strip())
    print(f"Email:    {result.get('email', '')}")


def post_message(creds, channel_id, text, root_id=None, approved=False):
    require_send_approval('post to Mattermost', approved)
    require_credentials(creds, need_team=False)
    body = {'channel_id': channel_id, 'message': text}
    if root_id:
        body['root_id'] = root_id
    result = post_api(creds, '/posts', body)
    if 'id' not in result:
        print(f"[ERROR] Post failed: {result}", file=sys.stderr)
        sys.exit(1)
    print(f"[{_current_workspace}] Message posted. Post ID: {result['id']}")
    permalink = f"{creds['url']}/pl/{result['id']}"
    print(f"Permalink: {permalink}")

# ---------- CLI ----------

def main():
    p = argparse.ArgumentParser(description='Mattermost Connector')
    p.add_argument('--action', required=True,
                   choices=['list_teams', 'list_channels', 'list_dms',
                            'history', 'search', 'user_info', 'post'],
                   help='Action to perform')
    p.add_argument('--workspace', default=None,
                   help='Workspace name (default: active workspace)')
    p.add_argument('--channel', help='Channel ID for history/post actions')
    p.add_argument('--limit', type=int, default=20, help='Number of messages (history)')
    p.add_argument('--query', help='Search query')
    p.add_argument('--user', help='User ID or @username for user_info')
    p.add_argument('--text', help='Message text for post')
    p.add_argument('--text-file', dest='text_file',
                   help='Read post text from a file')
    p.add_argument('--root-id', dest='root_id', help='Root post ID for thread reply')
    p.add_argument('--include-private', dest='include_private', action='store_true')
    p.add_argument('--approved', action='store_true',
                   help='Owner has explicitly approved this post.')
    args = p.parse_args()

    creds = load_credentials(args.workspace)

    if args.action == 'list_teams':
        list_teams(creds)
    elif args.action == 'list_channels':
        list_channels(creds, include_private=args.include_private)
    elif args.action == 'list_dms':
        list_dms(creds)
    elif args.action == 'history':
        if not args.channel:
            p.error('--channel is required for history')
        get_history(creds, args.channel, limit=args.limit)
    elif args.action == 'search':
        if not args.query:
            p.error('--query is required for search')
        search_messages(creds, args.query)
    elif args.action == 'user_info':
        if not args.user:
            p.error('--user is required for user_info')
        get_user_info(creds, args.user)
    elif args.action == 'post':
        if not args.channel:
            p.error('--channel is required for post')
        text = ''
        if args.text_file:
            with open(args.text_file, encoding='utf-8') as f:
                text = f.read()
        elif args.text:
            text = args.text
        else:
            p.error('--text or --text-file is required for post')
        post_message(creds, args.channel, text,
                     root_id=args.root_id, approved=args.approved)


if __name__ == '__main__':
    main()
