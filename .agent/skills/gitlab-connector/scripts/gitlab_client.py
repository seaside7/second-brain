#!/usr/bin/env python3
"""
GitLab Connector
Read projects, issues, merge requests, pipelines, and search code via GitLab REST API v4.

Auth: credentials resolved via workspace_resolver (workspace-specific gitlab.env).
API:  https://your-instance/api/v4/...
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
from datetime import datetime, timedelta, timezone

WIB = timezone(timedelta(hours=7))

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SKILL_DIR, '..', '..', '..', '..'))

# Add workspace resolver to path
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
    print("[ERROR] GitLab Connector timed out after 180 seconds", file=sys.stderr)
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
    """Load GITLAB_URL and GITLAB_TOKEN from workspace gitlab.env."""
    global _current_workspace
    ctx = ws.get(workspace_name)
    _current_workspace = ctx.name

    # Load from workspace gitlab.env
    env = ctx.load_env('gitlab')
    creds = {
        'url': env.get('GITLAB_URL', '').rstrip('/'),
        'token': env.get('GITLAB_TOKEN', ''),
    }

    # Fallback: try old location if workspace env is empty (backward compat)
    if not creds['url'] or not creds['token']:
        old_env_path = os.path.join(REPO_ROOT, '.agent', 'skills', 'gitlab-connector', 'token.env')
        if os.path.exists(old_env_path):
            with open(old_env_path, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#') or '=' not in line:
                        continue
                    k, v = line.split('=', 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    if k == 'GITLAB_URL' and not creds['url']:
                        creds['url'] = v.rstrip('/')
                    elif k == 'GITLAB_TOKEN' and not creds['token']:
                        creds['token'] = v

    # Fallback: environment variables
    if not creds['url']:
        creds['url'] = os.environ.get('GITLAB_URL', '').rstrip('/')
    if not creds['token']:
        creds['token'] = os.environ.get('GITLAB_TOKEN', '')

    return creds


def require_creds(creds):
    if not creds['url']:
        print(f"Error: GITLAB_URL not set for workspace '{_current_workspace}'.", file=sys.stderr)
        print(f"Add it to: .agent/workspaces/{_current_workspace}/gitlab.env", file=sys.stderr)
        sys.exit(1)
    if not creds['token']:
        print(f"Error: GITLAB_TOKEN not set for workspace '{_current_workspace}'.", file=sys.stderr)
        print(f"Add it to: .agent/workspaces/{_current_workspace}/gitlab.env", file=sys.stderr)
        sys.exit(1)

# ---------- HTTP ----------

def api_get(creds, path, params=None, retry=0):
    """GET request to GitLab API v4. Returns parsed JSON (list or dict)."""
    url = f"{creds['url']}/api/v4{path}"
    if params:
        url += '?' + urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}, doseq=True)

    headers = {'PRIVATE-TOKEN': creds['token']}
    req = urllib.request.Request(url, headers=headers)

    print(f"[DEBUG] [{_current_workspace}] GET {path}", file=sys.stderr)
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        if e.code == 429 and retry < MAX_RETRIES:
            wait = int(e.headers.get('Retry-After', 5))
            print(f"[WARN] Rate limited, waiting {wait}s...", file=sys.stderr)
            time.sleep(wait)
            return api_get(creds, path, params, retry + 1)
        detail = ''
        try:
            detail = e.read().decode('utf-8')[:300]
        except Exception:
            pass
        print(f"[ERROR] HTTP {e.code} on GET {path}: {detail}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"[ERROR] Cannot reach {creds['url']}: {e.reason}", file=sys.stderr)
        sys.exit(1)


def api_post(creds, path, body, retry=0):
    """POST request to GitLab API v4. Returns parsed JSON."""
    url = f"{creds['url']}/api/v4{path}"
    headers = {
        'PRIVATE-TOKEN': creds['token'],
        'Content-Type': 'application/json',
    }
    data = json.dumps(body).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers=headers, method='POST')

    print(f"[DEBUG] [{_current_workspace}] POST {path}", file=sys.stderr)
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        if e.code == 429 and retry < MAX_RETRIES:
            wait = int(e.headers.get('Retry-After', 5))
            print(f"[WARN] Rate limited, waiting {wait}s...", file=sys.stderr)
            time.sleep(wait)
            return api_post(creds, path, body, retry + 1)
        detail = ''
        try:
            detail = e.read().decode('utf-8')[:300]
        except Exception:
            pass
        print(f"[ERROR] HTTP {e.code} on POST {path}: {detail}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"[ERROR] Cannot reach {creds['url']}: {e.reason}", file=sys.stderr)
        sys.exit(1)

# ---------- actions ----------

def list_groups(creds):
    require_creds(creds)
    groups = api_get(creds, '/groups', params={'per_page': 50, 'min_access_level': 10})
    print(f"[{_current_workspace}] Found {len(groups)} group(s):")
    for g in groups:
        print(f"  {g.get('full_path')} - ID: {g['id']}  ({g.get('visibility', '')})")


def list_projects(creds, group_id=None):
    require_creds(creds)
    if group_id:
        projects = api_get(creds, f'/groups/{group_id}/projects',
                           params={'per_page': 50, 'order_by': 'last_activity_at'})
    else:
        projects = api_get(creds, '/projects',
                           params={'membership': 'true', 'per_page': 50,
                                   'order_by': 'last_activity_at'})
    print(f"[{_current_workspace}] Found {len(projects)} project(s):")
    for p in projects:
        print(f"  {p.get('path_with_namespace')} - ID: {p['id']}  "
              f"({p.get('default_branch', '?')})")


def list_issues(creds, project_id, state=None, labels=None):
    require_creds(creds)
    params = {'per_page': 30, 'order_by': 'updated_at', 'sort': 'desc'}
    if state:
        params['state'] = state
    if labels:
        params['labels'] = labels
    issues = api_get(creds, f'/projects/{project_id}/issues', params=params)
    print(f"[{_current_workspace}] Found {len(issues)} issue(s):")
    for i in issues:
        assignee = (i.get('assignee') or {}).get('username', 'unassigned')
        labels_str = ', '.join(i.get('labels', []))
        print(f"  #{i['iid']} [{i.get('state')}] {i.get('title')}")
        print(f"       assignee: @{assignee}  labels: {labels_str or '-'}")


def get_issue(creds, project_id, iid):
    require_creds(creds)
    issue = api_get(creds, f'/projects/{project_id}/issues/{iid}')
    print(f"Issue #{issue['iid']}: {issue.get('title')}")
    print(f"State:    {issue.get('state')}")
    print(f"Author:   @{(issue.get('author') or {}).get('username', '?')}")
    print(f"Assignee: @{(issue.get('assignee') or {}).get('username', 'unassigned')}")
    print(f"Labels:   {', '.join(issue.get('labels', [])) or '-'}")
    print(f"Created:  {issue.get('created_at')}")
    print(f"Updated:  {issue.get('updated_at')}")
    print(f"URL:      {issue.get('web_url')}")
    desc = issue.get('description') or '(no description)'
    print(f"\n--- Description ---\n{desc}")


def create_issue(creds, project_id, title, description=None, labels=None,
                 assignee_id=None, approved=False):
    require_send_approval('create GitLab issue', approved)
    require_creds(creds)
    body = {'title': title}
    if description:
        body['description'] = description
    if labels:
        body['labels'] = labels
    if assignee_id:
        body['assignee_ids'] = [int(assignee_id)]
    result = api_post(creds, f'/projects/{project_id}/issues', body)
    print(f"[{_current_workspace}] Issue created: #{result.get('iid')} - {result.get('title')}")
    print(f"URL: {result.get('web_url')}")


def list_mrs(creds, project_id, state=None):
    require_creds(creds)
    params = {'per_page': 20, 'order_by': 'updated_at', 'sort': 'desc'}
    if state:
        params['state'] = state
    mrs = api_get(creds, f'/projects/{project_id}/merge_requests', params=params)
    print(f"[{_current_workspace}] Found {len(mrs)} merge request(s):")
    for mr in mrs:
        author = (mr.get('author') or {}).get('username', '?')
        print(f"  !{mr['iid']} [{mr.get('state')}] {mr.get('title')}")
        print(f"       author: @{author}  "
              f"source: {mr.get('source_branch')} -> {mr.get('target_branch')}")


def get_mr(creds, project_id, iid):
    require_creds(creds)
    mr = api_get(creds, f'/projects/{project_id}/merge_requests/{iid}')
    print(f"MR !{mr['iid']}: {mr.get('title')}")
    print(f"State:    {mr.get('state')}")
    print(f"Author:   @{(mr.get('author') or {}).get('username', '?')}")
    print(f"Branch:   {mr.get('source_branch')} -> {mr.get('target_branch')}")
    print(f"Created:  {mr.get('created_at')}")
    print(f"Updated:  {mr.get('updated_at')}")
    print(f"URL:      {mr.get('web_url')}")
    desc = mr.get('description') or '(no description)'
    print(f"\n--- Description ---\n{desc[:2000]}")


def search_code(creds, query):
    require_creds(creds)
    results = api_get(creds, '/search',
                      params={'scope': 'blobs', 'search': query, 'per_page': 20})
    print(f"[{_current_workspace}] Found {len(results)} code result(s) for '{query}':")
    for r in results:
        project = r.get('project_id', '?')
        path = r.get('path', '?')
        ref = r.get('ref', '')
        data = (r.get('data') or '').replace('\n', ' ')
        if len(data) > 120:
            data = data[:120] + '...'
        print(f"  [project:{project}] {path} ({ref}): {data}")


def list_pipelines(creds, project_id):
    require_creds(creds)
    pipes = api_get(creds, f'/projects/{project_id}/pipelines',
                    params={'per_page': 15, 'order_by': 'updated_at', 'sort': 'desc'})
    print(f"[{_current_workspace}] Found {len(pipes)} pipeline(s):")
    for p in pipes:
        print(f"  #{p['id']} [{p.get('status')}] ref={p.get('ref')}  "
              f"created: {p.get('created_at')}")


def list_commits(creds, project_id, since=None, until=None, author=None, ref_name=None):
    """List commits in a project with optional date/author filters."""
    require_creds(creds)
    params = {'per_page': 50}
    if since:
        params['since'] = since
    if until:
        params['until'] = until
    if author:
        params['author'] = author
    if ref_name:
        params['ref_name'] = ref_name
    else:
        params['all'] = 'true'
    commits = api_get(creds, f'/projects/{project_id}/repository/commits', params=params)
    return commits


def list_my_commits_today(creds, output_json=False):
    """List all commits by the current user across all accessible projects, from today (WIB)."""
    require_creds(creds)

    me = api_get(creds, '/user')
    my_email = me.get('email', '')
    my_username = me.get('username', '')
    my_name = me.get('name', '')

    now = datetime.now(WIB)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    since_iso = today_start.isoformat()
    week_ago = (now - timedelta(days=7)).isoformat()

    projects = api_get(creds, '/projects',
                       params={'membership': 'true', 'per_page': 50,
                               'order_by': 'last_activity_at', 'sort': 'desc',
                               'last_activity_after': week_ago})

    results = []
    for proj in projects:
        pid = proj['id']
        pname = proj.get('path_with_namespace', proj.get('name', str(pid)))

        commits = list_commits(creds, pid, since=since_iso)
        if not isinstance(commits, list):
            continue

        my_commits = []
        for c in commits:
            c_email = (c.get('author_email') or '').lower()
            c_name = (c.get('author_name') or '').lower()
            if (c_email == my_email.lower() or
                c_name == my_name.lower() or
                c_name == my_username.lower()):
                my_commits.append(c)

        if my_commits:
            results.append({
                'project': pname,
                'project_id': pid,
                'workspace': _current_workspace,
                'commits': my_commits,
            })

    if output_json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        total = sum(len(r['commits']) for r in results)
        print(f"[{_current_workspace}] Found {total} commit(s) today across {len(results)} project(s):")
        for r in results:
            print(f"\n  {r['project']} ({len(r['commits'])} commits):")
            for c in r['commits']:
                short_id = c.get('short_id', c.get('id', '')[:8])
                msg = (c.get('title') or c.get('message', '')).split('\n')[0]
                print(f"    {short_id} - {msg}")

    return results

# ---------- CLI ----------

def main():
    p = argparse.ArgumentParser(description='GitLab Connector')
    p.add_argument('--action', required=True,
                   choices=['list_groups', 'list_projects', 'list_issues',
                            'get_issue', 'create_issue', 'list_mrs', 'get_mr',
                            'search', 'pipelines', 'my_commits_today'],
                   help='Action to perform')
    p.add_argument('--workspace', default=None,
                   help='Workspace name (default: active workspace from workspaces.json)')
    p.add_argument('--project-id', dest='project_id', help='GitLab project ID')
    p.add_argument('--group-id', dest='group_id', help='GitLab group ID')
    p.add_argument('--iid', help='Issue or MR internal ID (within a project)')
    p.add_argument('--state', help='Filter by state: opened, closed, merged, all')
    p.add_argument('--labels', help='Comma-separated labels filter')
    p.add_argument('--title', help='Issue title (create_issue)')
    p.add_argument('--description', help='Issue description (create_issue)')
    p.add_argument('--assignee-id', dest='assignee_id', help='Assignee user ID')
    p.add_argument('--query', help='Search query')
    p.add_argument('--json', dest='output_json', action='store_true', help='Output as JSON')
    p.add_argument('--approved', action='store_true',
                   help='Owner has explicitly approved this write action.')
    args = p.parse_args()

    creds = load_credentials(args.workspace)

    if args.action == 'list_groups':
        list_groups(creds)
    elif args.action == 'list_projects':
        list_projects(creds, group_id=args.group_id)
    elif args.action == 'list_issues':
        if not args.project_id:
            p.error('--project-id is required for list_issues')
        list_issues(creds, args.project_id, state=args.state, labels=args.labels)
    elif args.action == 'get_issue':
        if not args.project_id or not args.iid:
            p.error('--project-id and --iid are required for get_issue')
        get_issue(creds, args.project_id, args.iid)
    elif args.action == 'create_issue':
        if not args.project_id or not args.title:
            p.error('--project-id and --title are required for create_issue')
        create_issue(creds, args.project_id, args.title,
                     description=args.description, labels=args.labels,
                     assignee_id=args.assignee_id, approved=args.approved)
    elif args.action == 'list_mrs':
        if not args.project_id:
            p.error('--project-id is required for list_mrs')
        list_mrs(creds, args.project_id, state=args.state)
    elif args.action == 'get_mr':
        if not args.project_id or not args.iid:
            p.error('--project-id and --iid are required for get_mr')
        get_mr(creds, args.project_id, args.iid)
    elif args.action == 'search':
        if not args.query:
            p.error('--query is required for search')
        search_code(creds, args.query)
    elif args.action == 'pipelines':
        if not args.project_id:
            p.error('--project-id is required for pipelines')
        list_pipelines(creds, args.project_id)
    elif args.action == 'my_commits_today':
        list_my_commits_today(creds, output_json=args.output_json)


if __name__ == '__main__':
    main()
