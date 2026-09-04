#!/usr/bin/env python3
"""
Coding Agent - OpenCode-backed coding jobs for the dashboard.

Runs a dedicated 'opencode serve' instance PER JOB (rooted in the job's own
directory) so every session is guaranteed to operate inside exactly one
repository/worktree. Creates one worktree per job and stages changes on the
repo's `staging` branch by default (feature branches only when explicitly
requested). Building, committing and pushing are all explicit, human-gated
steps - the agent never auto-commits, auto-pushes or auto-deploys.

The dashboard server imports this module and delegates all /api/coding/* and
/api/coding/preview/* traffic here.
"""

import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ── basic layout / identity ────────────────────────────────────────────
WIB = timezone(timedelta(hours=7))

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = BASE_DIR / 'journal' / 'state'
JOBS_PATH = STATE_DIR / 'coding_jobs.json'
LOG_DIR = STATE_DIR / 'coding-logs'

GIT_NAME = os.environ.get('CODING_GIT_NAME', 'Second Brain Coding Agent')
# NOTE: do NOT fall back to OWNER_EMAIL - .env carries a placeholder value
# (you@example.com) that would silently leak into commit identities.
GIT_EMAIL = os.environ.get('CODING_GIT_EMAIL', 'said@catalyze.id')

NEVER_BRANCHES = {'main', 'master', 'develop'}
GATES = ('build', 'commit', 'push')

# repo types that have no browser preview (backend/api/cms/php)
BACKEND_TYPES = {'be', 'api', 'backend', 'cms', 'php', 'worker'}

MAX_FILES = 10
MAX_FILE_BYTES = 2 * 1024 * 1024

START = threading.Event()  # set once, signals `_started_at` handshake to loaders


# ── per-process state ──────────────────────────────────────────────────
_jobs = {}
_jobs_lock = threading.Lock()
_pids = {}            # job_id -> process object (opencode serve)
_sse_threads = {}     # job_id -> SSE consumer thread
_running_threads = {} # job_id -> worker thread

# persistent per-repo opencode sessions (the "terminal" model)
REPO_SESSION_STATE = STATE_DIR / 'coding_sessions.json'
_sessions = {}        # repo_name -> session record
_sessions_lock = threading.Lock()
_rep_pids = {}        # repo_name -> process object
_rep_sse = {}         # repo_name -> SSE thread
_rep_threads = {}     # repo_name -> worker thread (in-flight prompt)


# ── small infra ────────────────────────────────────────────────────────
def _log(msg):
    try:
        sys.stderr.write(f"[coding] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _now_iso():
    return datetime.now(WIB).isoformat(timespec='seconds')


def _config(**extra):
    cfg = dict(extra or {})
    root = os.environ.get('CODING_PROJECTS_ROOT', '').strip()
    cfg['projects_root'] = root
    cfg['worktrees_root'] = os.environ.get(
        'CODING_WORKTREES_ROOT', '').strip() or (root.rstrip('/') + '/.worktrees' if root else '')
    cfg['opencode_bin'] = os.environ.get('CODING_OPENCODE_BIN', 'opencode').strip()
    cfg['opencode_username'] = os.environ.get('OPENCODE_SERVER_USERNAME', 'opencode').strip()
    cfg['single_url'] = os.environ.get('OPENCODE_BASE_URL', '').strip() or 'http://127.0.0.1:4096'
    cfg['single_password'] = os.environ.get('OPENCODE_SERVER_PASSWORD', '').strip()
    cfg['mode'] = os.environ.get('CODING_OPENCODE_MODE', 'per-job').strip()
    try:
        lo, hi = (int(x) for x in os.environ.get('CODING_PORT_RANGE', '4100-4199').split('-', 1))
    except Exception:
        lo, hi = 4100, 4199
    cfg['port_range'] = (lo, max(lo, hi))
    try:
        cfg['job_ttl_hours'] = float(os.environ.get('CODING_JOB_TTL_HOURS', '72'))
    except Exception:
        cfg['job_ttl_hours'] = 72.0
    try:
        cfg['preview_ttl_secs'] = float(os.environ.get('CODING_PREVIEW_TTL_SECS', '1800'))
    except Exception:
        cfg['preview_ttl_secs'] = 1800.0
    return cfg


def _loads_jobs():
    global _jobs
    try:
        _jobs = json.loads(JOBS_PATH.read_text(encoding='utf-8'))
        # ever careless hand-edits: keep shape sane
        for k in list(_jobs):
            if not isinstance(_jobs.get(k), dict):
                _jobs.pop(k, None)
    except FileNotFoundError:
        _jobs = {}
    except Exception as e:
        _log(f"failed to load coding jobs: {e}")
        _jobs = {}


def _save_jobs():
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        JOBS_PATH.write_text(json.dumps(_jobs, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception as e:
        _log(f"failed to save coding jobs: {e}")


def _job_log(job_id):
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return LOG_DIR / f"{job_id}.log"


def _append_job_line(job, text):
    line = f"[{_now_iso()}] {text}"
    try:
        with open(_job_log(job['id']), 'a', encoding='utf-8') as fh:
            fh.write(line + "\n")
    except Exception:
        pass
    job.setdefault('log_tail', [])
    job['log_tail'].append(line)
    del job['log_tail'][:-200]


def _load_repo_config(repo_path):
    """Effective commands for a repo: manual .coding.json overrides + package.json scripts."""
    cfg = {}
    coding_json = Path(repo_path) / '.coding.json'
    if coding_json.exists():
        try:
            cfg = json.loads(coding_json.read_text(encoding='utf-8')) or {}
        except Exception:
            cfg = {}
    scripts, deps = {}, {}
    pkg = Path(repo_path) / 'package.json'
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding='utf-8')) or {}
            scripts = data.get('scripts') or {}
            deps = {**(data.get('dependencies') or {}), **(data.get('devDependencies') or {})}
        except Exception:
            scripts, deps = {}, {}

    def _pick(key, *names):
        val = cfg.get(key)
        if val:
            return str(val)
        for n in names:
            if scripts.get(n):
                return str(scripts[n])
        return None

    install = _pick('installCommand', 'install') or ('npm install' if pkg.exists() else None)
    out = {
        'type': cfg.get('type') or _detect_type(repo_path, pkg.exists(), scripts, deps),
        'installCommand': install,
        'lintCommand': _pick('lintCommand', 'lint'),
        'testCommand': _pick('testCommand', 'test', 'jest'),
        'buildCommand': _pick('buildCommand', 'build'),
        'devCommand': _pick('devCommand', 'dev', 'develop', 'serve', 'start'),
        'startCommand': _pick('startCommand', 'start'),
        'healthPath': str(cfg.get('healthPath') or '/'),
        'port': int(cfg['port']) if cfg.get('port') else None,
        'previewEnabled': bool(cfg.get('previewEnabled', True)),
    }
    if out['type'].lower() in BACKEND_TYPES:
        out['previewEnabled'] = False
    if not out['devCommand'] and out['startCommand']:
        out['devCommand'] = out['startCommand']
    return out


def _set_repo_type(name, rtype):
    """Persist the repo's fe/be type into <repo>/.coding.json and gitignore that
    file so it never marks the repo dirty or gets committed. rtype is one of:
    fe, be, cms, api, php, other, auto (auto = clear override, re-detect)."""
    info = _find_repo(name)
    if not info:
        raise ValueError('repo not found')
    repo_path = Path(info['path'])
    coding_json = repo_path / '.coding.json'
    cfg = {}
    if coding_json.exists():
        try:
            cfg = json.loads(coding_json.read_text(encoding='utf-8')) or {}
        except Exception:
            cfg = {}
    if rtype == 'auto' or rtype in ('fe', 'be', 'cms', 'api', 'php', 'other'):
        if rtype == 'auto':
            cfg.pop('type', None)
        else:
            cfg['type'] = rtype
    else:
        raise ValueError("type must be one of: fe, be, cms, api, php, other, auto")
    coding_json.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    _ignore_coding_json(repo_path)
    return _repo_info(info['path'])


def _ignore_coding_json(repo_path):
    """Ensure .coding.json is gitignored in the repo so our agent metadata never
    shows up as dirty changes or gets committed."""
    gitignore = repo_path / '.gitignore'
    lines = []
    if gitignore.exists():
        try:
            lines = gitignore.read_text(encoding='utf-8').splitlines()
        except Exception:
            lines = []
    if '.coding.json' not in [l.strip() for l in lines]:
        lines.append('.coding.json')
        try:
            gitignore.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        except Exception:
            pass


def _detect_type(repo_path, has_pkg, scripts, deps):
    if any('strapi' in str(k) for k in deps):
        return 'cms'
    if any(k in deps for k in ('next', 'nuxt', 'vue', 'react', '@sveltejs/kit')):
        return 'frontend'
    if any(k in deps for k in ('express', 'fastify', '@nestjs/core', 'koa', 'hono')):
        return 'api'
    if (Path(repo_path) / 'composer.json').exists():
        return 'php'
    return 'other'


def _git(repo, *args, timeout=120, env=None):
    base_env = os.environ.copy()
    if env:
        base_env.update(env)
    return subprocess.run(
        ['git', '-C', str(repo), *args],
        capture_output=True, text=True, timeout=timeout, env=base_env)


def _git_out(repo, *args, timeout=120):
    try:
        r = _git(repo, *args, timeout=timeout)
        return (r.stdout or '').strip() if r.returncode == 0 else ''
    except Exception:
        return ''


def _default_branch(repo):
    out = _git_out(repo, 'symbolic-ref', '--quiet', '--short', 'refs/remotes/origin/HEAD')
    if out:
        return out.split('/')[-1]
    # order of preference over ls-remote / local remotes
    try:
        r = _git(repo, 'ls-remote', '--symref', 'origin', 'HEAD', timeout=30)
        for line in (r.stdout or '').splitlines():
            m = re.match(r'^ref:\s+refs/heads/(\S+)\s+HEAD$', line)
            if m:
                return m.group(1)
    except Exception:
        pass
    for name in ('main', 'master', 'develop', 'staging'):
        if _git(repo, 'show-ref', '--verify', '--quiet', f'refs/remotes/origin/{name}').returncode == 0:
            return name
    return _git_out(repo, 'rev-parse', '--abbrev-ref', 'HEAD') or 'main'


def _is_git_repo(repo):
    return (Path(repo) / '.git').exists()


def _repo_info(path):
    path = Path(os.path.realpath(path))
    try:
        current = _git_out(path, 'rev-parse', '--abbrev-ref', 'HEAD')
        if current == 'HEAD':
            current = '(detached)'
    except Exception:
        current = '?'
    try:
        dirty = bool(_git_out(path, 'status', '--porcelain'))
    except Exception:
        dirty = False
    origin = _git_out(path, 'remote', 'get-url', 'origin')
    try:
        default = _default_branch(path)
    except Exception:
        default = 'main'
    config = _load_repo_config(path)
    return {
        'name': path.name,
        'path': str(path),
        'git': True,
        'default_branch': default,
        'current_branch': current,
        'dirty': dirty,
        'origin_url': origin,
        'config': config,
    }


def _scan_repos():
    cfg = _config()
    root = cfg.get('projects_root')
    if not root:
        return {'configured': False, 'root': None, 'repos': []}
    root = Path(os.path.realpath(root))
    repos = []
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        real = Path(os.path.realpath(child))
        if str(real) != str(child) and not str(real).startswith(str(root) + os.sep):
            continue  # symlink escaping PROJECTS_ROOT - reject
        if not (child / '.git').exists():
            continue
        repos.append(_repo_info(real))
    return {'configured': True, 'root': str(root), 'repos': repos}


def _find_repo(name):
    for r in _scan_repos()['repos']:
        if r['name'] == name:
            return r
    return None


# ── worktree + branch management ───────────────────────────────────────
def _branch_exists(repo, name):
    return _git(repo, 'show-ref', '--verify', '--quiet', f'refs/heads/{name}').returncode == 0


def _remote_branch_exists(repo, name):
    return _git(repo, 'show-ref', '--verify', '--quiet', f'refs/remotes/origin/{name}').returncode == 0


def _worktree_path(job, cfg):
    return Path(cfg['worktrees_root']) / job['repo'] / job['id']


_ACTIVE_STATUSES = {
    'building', 'testing', 'awaiting_commit_approval',
    'awaiting_push_approval', 'pushing', 'running',
}


def _worktree_with_branch(repo, branch, exclude_path=None):
    """Return the path of the worktree currently checking out `branch`
    (excluding `exclude_path`), or None."""
    exclude = str(Path(exclude_path).resolve()).lower() if exclude_path else None
    r = _git(repo, 'worktree', 'list', '--porcelain', timeout=30)
    path, head_branch = None, None
    for line in (r.stdout or '').splitlines():
        if line.startswith('worktree '):
            if path and head_branch == f'refs/heads/{branch}' and (exclude is None or str(Path(path).resolve()).lower() != exclude):
                return path
            path, head_branch = line[9:].strip(), None
        elif line.startswith('branch '):
            head_branch = line[7:].strip()
    if path and head_branch == f'refs/heads/{branch}' and (exclude is None or str(Path(path).resolve()).lower() != exclude):
        return path
    return None


def _reclaim_or_raise(repo, branch, cfg, current_wt, job):
    """If `branch` is checked out in other worktrees under our worktrees root,
    reclaim each stale one; refuse to start if any owner is still active."""
    wt_root = str(Path(cfg['worktrees_root']).resolve()).lower()
    while True:
        other = _worktree_with_branch(repo, branch, exclude_path=current_wt)
        if not other:
            return
        other_resolved = str(Path(other).resolve()).lower()
        if not other_resolved.startswith(wt_root):
            raise RuntimeError(f'worktree {other} already checks out {branch} (foreign worktree) - refusing to start')
        owner = _jobs.get(Path(other).name)
        if owner and owner.get('status') in _ACTIVE_STATUSES:
            raise RuntimeError(
                f"{branch} is already checked out in active job {Path(other).name} "
                f"(status {owner.get('status')}); finish or stop that job first")
        _git(repo, 'worktree', 'remove', '--force', other, timeout=60)
        _git(repo, 'worktree', 'prune', timeout=30)
        if job:
            _append_job_line(job, f'removed stale {branch} worktree {other} (owned by finished job {Path(other).name})')


def _ensure_worktree(job):
    """Create the per-job git worktree + branch. Default branch is `staging`;
    a feature branch is used only when requested. Returns (worktree_path, diff_base)."""
    cfg = _config()
    repo = Path(job['repo_path'])
    wt = _worktree_path(job, cfg)
    requested = job.get('branch') or 'staging'
    if requested in NEVER_BRANCHES:
        raise ValueError(f"{requested} cannot be used as a coding target branch")
    feature = not (requested == 'staging')

    default = _default_branch(repo)
    if _remote_branch_exists(repo, 'staging'):
        base_ref = 'origin/staging'
    else:
        base_ref = f"origin/{default}" if _remote_branch_exists(repo, default) else 'HEAD'

    if feature:
        branch = requested if requested.startswith('coding/') else f"coding/{job['id']}"
        job.setdefault('requested_branch', requested)
        job['branch'] = branch
        if _branch_exists(repo, branch):
            _reclaim_or_raise(repo, branch, cfg, str(wt), job)
            r = _git(repo, 'worktree', 'add', '--force', str(wt), branch)
        else:
            r = _git(repo, 'worktree', 'add', '-b', branch, str(wt), base_ref)
    elif _branch_exists(repo, 'staging'):
        _reclaim_or_raise(repo, 'staging', cfg, str(wt), job)
        r = _git(repo, 'worktree', 'add', '--force', str(wt), 'staging')
    else:
        r = _git(repo, 'worktree', 'add', '-b', 'staging', str(wt), base_ref)
    if r.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {(r.stderr or r.stdout).strip()[:500]}")
    job['directory'] = str(Path(os.path.realpath(wt)))
    job['diff_base'] = base_ref
    _append_job_line(job, f"worktree ready: {job['directory']} (branch {job['branch']}, base {job['diff_base']})")
    return job['directory'], job['diff_base']


def _cleanup_worktree(job):
    dir_ = job.get('directory')
    if not dir_:
        return
    try:
        _git(Path(job['repo_path']), 'worktree', 'remove', '--force', dir_)
    except Exception:
        pass
    job['directory'] = None
    job.setdefault('cleaned', True)


# ── port allocation (stable per job id) ────────────────────────────────
def _alloc_port(job_id, cfg):
    lo, hi = cfg['port_range']
    span = hi - lo + 1
    idx = int(hashlib.md5(job_id.encode()).hexdigest(), 16) % span
    return lo + idx


# ── process hygiene ────────────────────────────────────────────────────
def _kill_proc(pid):
    if not pid:
        return
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                           capture_output=True, timeout=30)
        else:
            os.killpg(pid, 15)  # start_new_session=True -> child's pgid == pid
    except Exception:
        try:
            if os.name != 'nt':
                os.kill(pid, 9)
        except Exception:
            pass


def _running(job):
    return job.get('status') not in ('failed', 'cancelled')


def _server_proc(job):
    return _pids.get(job['id'])


def _open_oc_base(job):
    o = job.get('opencode') or {}
    return f"http://127.0.0.1:{o.get('port')}"


def _oc_auth(job):
    o = job.get('opencode') or {}
    return (o.get('username') or 'opencode', o.get('password') or 'opencode')


def _start_server(job, cwd=None):
    """Spawn a dedicated opencode serve rooted in job dir; wait for health; create session."""
    cfg = _config()
    job_dir = cwd or job.get('directory')
    if not job_dir or not Path(job_dir).exists():
        raise RuntimeError('no directory to root the opencode server in')
    if job.get('opencode', {}).get('port') and _server_proc(job):
        return job['opencode']
    port = _alloc_port(job['id'], cfg)
    password = secrets.token_urlsafe(18)
    env = os.environ.copy()
    env['OPENCODE_SERVER_PASSWORD'] = password
    env['OPENCODE_SERVER_USERNAME'] = cfg['opencode_username']
    log_fh = open(_job_log(job['id']), 'ab')
    try:
        proc = subprocess.Popen(
            [cfg['opencode_bin'], 'serve', '--hostname', '127.0.0.1', '--port', str(port)],
            cwd=str(job_dir), env=env, stdout=log_fh, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True)
    finally:
        log_fh.close()
    _pids[job['id']] = proc
    job['opencode'] = {'port': port, 'password': password,
                       'username': cfg['opencode_username'], 'pid': proc.pid}
    _append_job_line(job, f"opencode serve started on 127.0.0.1:{port} (pid {proc.pid})")

    base = _open_oc_base(job)
    auth = _oc_auth(job)
    deadline = time.time() + 90
    healthy = False
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            r = requests.get(base + '/global/health', auth=auth, timeout=3)
            if r.ok and r.json().get('healthy'):
                healthy = True
                break
        except Exception:
            pass
        time.sleep(1)
    if not healthy:
        _kill_proc(proc.pid)
        _pids.pop(job['id'], None)
        tail = ''
        try:
            tail = '\n'.join(_job_log(job['id']).read_text(encoding='utf-8', errors='ignore').splitlines()[-20:])
        except Exception:
            pass
        raise RuntimeError(f"opencode serve did not become healthy (log: {tail[:800]})")

    try:
        r = requests.post(base + '/session', auth=auth, json={'title': f"{job['id']} {job.get('task','')[:40]}"}, timeout=30)
        r.raise_for_status()
        session_id = r.json().get('id')
    except Exception as e:
        _kill_proc(proc.pid)
        _pids.pop(job['id'], None)
        raise RuntimeError(f"failed to create opencode session: {e}")
    job['opencode']['session_id'] = session_id
    _append_job_line(job, f"opencode session {session_id} created")
    _start_sse(job)
    return job['opencode']


def _stop_server(job, kill=True):
    o = job.get('opencode', {})
    if o:
        try:
            requests.post(_open_oc_base(job) + f"/session/{o['session_id']}/abort",
                          auth=_oc_auth(job), json={}, timeout=5)
        except Exception:
            pass
    proc = _server_proc(job)
    if proc:
        _kill_proc(proc.pid)
        _pids.pop(job['id'], None)
    job['opencode'] = {}
    if kill:
        _append_job_line(job, 'opencode server stopped')


# ── OpenCode REST helpers ──────────────────────────────────────────────
def _oc(job, method, path, json_body=None, timeout=(15, 3600), stream=False):
    base = _open_oc_base(job)
    auth = _oc_auth(job)
    return requests.request(method, base + path, auth=auth, json=json_body,
                            timeout=timeout, stream=stream)


def _extract_text(parts):
    out = []
    for p in parts or []:
        if isinstance(p, dict) and p.get('type') == 'text' and p.get('text'):
            out.append(p['text'])
    return '\n\n'.join(out)


def _refresh_messages(job):
    try:
        r = _oc(job, 'GET', f"/session/{job['opencode']['session_id']}/message?limit=100", timeout=30)
        if not r.ok:
            return
        data = r.json() or []
    except Exception:
        return
    msgs = []
    for m in data:
        info = m.get('info') or {}
        role = info.get('role')
        text = _extract_text(m.get('parts'))
        if role and text:
            msgs.append({'role': role, 'text': text})
    job['messages'] = msgs[-40:]


def _prompt(job, text, system=None, agent=None):
    body = {'system': system} if system else {}
    if agent:
        body['agent'] = agent
    body['parts'] = [{'type': 'text', 'text': text}]
    r = _oc(job, 'POST', f"/session/{job['opencode']['session_id']}/message", json_body=body)
    if r.status_code >= 400:
        raise RuntimeError(f"opencode prompt failed ({r.status_code}): {r.text[:400]}")
    _refresh_messages(job)
    return ''


PLAN_SYSTEM = """You are a senior software engineer writing a READ-ONLY implementation plan for the repository at this project root.
Constraints:
- Do NOT create, edit or delete any files, and do not run any git command that changes repository state. Plan only.
- Read the repository freely (AGENTS.md, documentation, source, tests, package configs) to ground your plan in the real codebase.
- If the task references attachment files, they live OUTSIDE this repo (their absolute paths are given). You may read them, never modify them.
- Return a concise, actionable Markdown implementation plan with these sections:
  1. Summary of what will change
  2. Files to create / modify / delete (exact repository-relative paths)
  3. Step-by-step implementation order
  4. Tests / verification steps for this project
  5. Risks and open questions
End the plan with a single line containing exactly: PLAN_END"""


BUILD_SYSTEM = """You are a senior software engineer implementing changes inside THIS git worktree (the worktree is the project root).
Constraints:
- Work ONLY inside this worktree. Never read, write or run commands that touch files outside it, and never run destructive commands (e.g. rm -rf, git reset --hard, git clean -fdx) or commands that affect other repositories.
- Do not commit or push - the owner's tooling handles version control after review.
- Follow the repository's own conventions and any AGENTS.md you find.
- If an approved implementation plan is provided, implement exactly that plan and nothing extra.
- After implementing, verify with the project's own commands where feasible and summarize the result."""


def _task_payload_text(job):
    parts = [job.get('task', '')]
    att = job.get('attachments') or []
    if att:
        parts.append("Attachment files (read-only, OUTSIDE the repo - do not modify them):")
        for a in att:
            parts.append(f"- {a['name']}  ({a['path']})")
    return '\n\n'.join(parts)


# ── worker threads ─────────────────────────────────────────────────────
def _run_job(job):
    try:
        if job['mode'] == 'plan':
            job['status'] = 'planning'
            _refresh_jobs_state(job)
            # plan mode is READ-ONLY and runs in the repo checkout (no worktree),
            # so AGENTS.md/docs resolve correctly against the real tree.
            _start_server(job, cwd=job['repo_path'])
            _prompt(job, _task_payload_text(job), system=PLAN_SYSTEM, agent='plan')
            plan = '\n\n'.join(m['text'] for m in job.get('messages', []) if m.get('role') == 'assistant')
            job['plan'] = plan
            job['status'] = 'awaiting_build_approval'
            _append_job_line(job, 'plan complete - awaiting build approval')
            # the plan server was rooted in the CHECKOUT. Never let a later build
            # reuse it (or it would edit the checkout, not the worktree) - stop it.
            _stop_server(job)
        else:
            job['status'] = 'building'
            _refresh_jobs_state(job)
            _build_phase(job)
    except Exception as e:
        job['status'] = 'failed'
        job['last_error'] = str(e)
        _append_job_line(job, f"job failed: {e}")
        _stop_server(job)
    finally:
        _refresh_jobs_state(job)
        _save_jobs()


def _build_phase(job):
    _ensure_worktree(job)
    # guarantee the build server is rooted in the WORKTREE: any server left over
    # from the plan phase (rooted in the repo checkout) must be stopped first.
    if job.get('opencode', {}).get('port') and _server_proc(job):
        _stop_server(job)
    _start_server(job, cwd=job['directory'])
    text = _task_payload_text(job)
    if job.get('plan'):
        text = f"Approved implementation plan (implement exactly this and nothing extra):\n\n{job['plan']}\n\nOriginal task:\n\n{text}"
    _prompt(job, text, system=BUILD_SYSTEM, agent='build')
    job['status'] = 'testing'
    _refresh_jobs_state(job)
    # A read-only task (e.g. "pull latest", "check X") produces no diff. Treat it
    # as done: no install/lint/build (they'd fail on an empty worktree), no
    # commit/push (nothing to commit). The agent's answer in the conversation
    # already delivered the result.
    if not _has_changes(job.get('directory')):
        job['status'] = 'done'
        job['completed_at'] = _now_iso()
        _append_job_line(job, 'no code changes from this task - marking done (nothing to commit/push)')
        _stop_server(job)
        return
    _run_verification(job)


def _has_changes(dir_):
    """True if the git worktree at dir_ has any staged/unstaged/untracked change
    relative to its checkout (i.e. the agent actually modified files)."""
    if not dir_ or not Path(dir_).exists():
        return False
    r = _git(dir_, 'status', '--porcelain')
    return bool((r.stdout or '').strip())


def _approve_build(job):
    if job['status'] != 'awaiting_build_approval':
        raise ValueError(f"cannot approve build from status {job['status']}")
    job['approvals']['build'] = True
    job['status'] = 'building'
    _append_job_line(job, 'build approved by owner')
    _refresh_jobs_state(job)


def _run_verification(job):
    """Run install/lint/test/build from the worktree; results land in job['test_results']."""
    cfg = _load_repo_config(Path(job['repo_path']))  # read .coding.json from repo (not worktree)
    dir_ = job.get('directory')
    results = []
    steps = [('install', cfg.get('installCommand')), ('lint', cfg.get('lintCommand')),
             ('test', cfg.get('testCommand')), ('build', cfg.get('buildCommand'))]
    for name, cmd in steps:
        if not cmd:
            continue
        try:
            r = subprocess.run(cmd, shell=True, cwd=dir_, capture_output=True,
                               text=True, timeout=900, env=os.environ.copy())
            tail = '\n'.join((r.stdout or '').splitlines()[-30:])
            if (r.stderr or '').strip():
                tail += '\n' + '\n'.join(r.stderr.splitlines()[-10:])
            results.append({'step': name, 'command': cmd, 'rc': r.returncode,
                            'ok': r.returncode == 0, 'tail': tail[-2500:]})
            _append_job_line(job, f"verify {name}: rc={r.returncode}")
        except subprocess.TimeoutExpired:
            results.append({'step': name, 'command': cmd, 'rc': -1, 'ok': False,
                            'tail': 'timed out after 900s'})
        except Exception as e:
            results.append({'step': name, 'command': cmd, 'rc': -1, 'ok': False, 'tail': str(e)})
    job['test_results'] = results
    job['status'] = 'awaiting_commit_approval' if all(r['ok'] for r in results) else 'testing_failed'
    _append_job_line(job, f"verification done: status={job['status']}")
    _refresh_jobs_state(job)


# ── SSE: progress log + permission question surfacing ──────────────────
def _find_permission_id(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            lk = k.lower()
            if lk in ('permissionid', 'permission_id', 'permission'):
                if isinstance(v, dict):
                    pid = _find_permission_id(v)
                    if pid:
                        return pid
                elif isinstance(v, str) and v:
                    return v
            elif lk in ('id',) and isinstance(v, str) and v and 'perm' in k.lower():
                return v
        for v in obj.values():
            found = _find_permission_id(v)
            if found:
                return found
    return None


def _sse_loop(job, base, auth):
    """Tail /event, log everything, surface permission questions into job['questions']."""
    job_id = job['id']
    while _server_proc(job) and job.get('status') not in ('failed', 'cancelled'):
        try:
            with requests.get(base + '/event', auth=auth, stream=True,
                              timeout=(10, 60)) as resp:
                if not resp.ok:
                    time.sleep(3)
                    continue
                for raw in resp.iter_lines(decode_unicode=True):
                    if not raw:
                        continue
                    line = raw.strip()
                    if line.startswith('event:'):
                        last_event = line[len('event:'):].strip()
                    elif line.startswith('data:'):
                        last_event = last_event or 'message'
                        payload = line[len('data:'):].strip()
                        ev = last_event
                        last_event = None
                        if ev in ('MessagePartUpdated', 'SessionIdle', 'SessionError'):
                            continue  # too chatty for the log; message snapshot comes after prompt
                        _append_job_line(job, f"event {ev}")
                        if 'question' in ev.lower() or 'permission' in ev.lower():
                            try:
                                data = json.loads(payload) if payload else {}
                            except Exception:
                                data = {'raw': payload[:300]}
                            pid = _find_permission_id(data)
                            text = json.dumps(data, ensure_ascii=False)[:500]
                            questions = job.setdefault('questions', [])
                            if not pid or any(q.get('id') == pid for q in questions):
                                continue
                            questions.append({'id': pid, 'event': ev, 'text': text, 'answered': False,
                                              'asked_at': _now_iso()})
                            _append_job_line(job, f"PERMISSION QUESTION {pid}")
        except requests.exceptions.ReadTimeout:
            continue
        except Exception:
            # dying server (shutdown) - stop quietly
            if not _server_proc(job):
                break
            time.sleep(5)
            continue


def _start_sse(job):
    if _sse_threads.get(job['id']) and _sse_threads[job['id']].is_alive():
        return
    base = _open_oc_base(job)
    auth = _oc_auth(job)
    t = threading.Thread(target=_sse_loop, args=(job, base, auth), daemon=True)
    t.start()
    _sse_threads[job['id']] = t


# ── preview servers (authenticated proxy only) ─────────────────────────
def _start_preview(job):
    if job.get('preview', {}).get('pid') and job.get('preview', {}).get('active'):
        return job['preview']
    cfg = _config()
    repo_cfg = _load_repo_config(Path(job['repo_path']))
    if repo_cfg.get('type','').lower() in BACKEND_TYPES or not repo_cfg.get('previewEnabled', True):
        raise ValueError('preview is not available for this repo type (backend/api/cms)')
    cmd = repo_cfg.get('devCommand') or repo_cfg.get('startCommand')
    if not cmd:
        raise ValueError('no dev/start command configured for this repo')
    if not job.get('directory'):
        raise ValueError('no worktree yet - build first')
    if job.get('preview', {}).get('pid'):
        _kill_proc(job['preview']['pid'])
    port = repo_cfg.get('port') or _alloc_port('pv-' + job['id'], cfg)
    env = os.environ.copy()
    env['PORT'] = str(port)
    env['HOST'] = '127.0.0.1'
    log_fh = open(_job_log(job['id']) + '.preview', 'ab')
    try:
        proc = subprocess.Popen(cmd, shell=True, cwd=job['directory'], env=env,
                                stdout=log_fh, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)
    finally:
        log_fh.close()
    health = repo_cfg.get('healthPath') or '/'
    deadline = time.time() + 120
    ok = False
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            r = requests.get(f'http://127.0.0.1:{port}{health}', timeout=3)
            if r.ok:
                ok = True
                break
        except Exception:
            pass
        time.sleep(2)
    if not ok:
        _kill_proc(proc.pid)
        raise RuntimeError('preview server did not become healthy on port {port}')
    job['preview'] = {'port': port, 'active': True, 'pid': proc.pid,
                      'started_at': _now_iso(), 'expires_at': time.time() + cfg['preview_ttl_secs'],
                      'url': f"/api/coding/preview/{job['id']}/"}
    _append_job_line(job, f"preview on 127.0.0.1:{port}")
    return job['preview']


def _stop_preview(job):
    p = job.get('preview') or {}
    if p.get('pid'):
        _kill_proc(p['pid'])
    if p:
        job['preview'] = {'active': False}
    _append_job_line(job, 'preview stopped')


# ── gates: commit + push ───────────────────────────────────────────────
def _git_env():
    env = os.environ.copy()
    if GIT_NAME:
        env['GIT_AUTHOR_NAME'] = GIT_NAME
        env['GIT_COMMITTER_NAME'] = GIT_NAME
    if GIT_EMAIL:
        env['GIT_AUTHOR_EMAIL'] = GIT_EMAIL
        env['GIT_COMMITTER_EMAIL'] = GIT_EMAIL
    return env


def _sanitize_branch(branch):
    return re.sub(r'[^0-9A-Za-z._/-]+', '-', branch).strip('-') or 'coding'


def approve_gate(job, gate):
    if not job:
        raise ValueError('unknown job')
    if gate == 'build':
        _approve_build(job)
        th = threading.Thread(target=_build_phase, args=(job,), daemon=True)
        _running_threads[job['id']] = th
        th.start()
        return {'ok': True, 'status': job['status']}
    if gate == 'commit':
        if job['status'] not in ('awaiting_commit_approval', 'testing_failed'):
            raise ValueError(f"cannot commit from status {job['status']}")
        dir_ = job.get('directory')
        if not dir_:
            raise ValueError('no worktree to commit')
        summary = (job.get('task') or '').strip().replace('\n', ' ')[:80]
        msg = f"coding({job['id']}): {summary}"
        r = _git(dir_, 'add', '-A')
        if r.returncode != 0:
            raise RuntimeError(f"git add failed: {r.stderr[:300]}")
        r = _git(dir_, 'commit', '-m', msg, env=_git_env())
        if r.returncode != 0:
            raise RuntimeError(f"git commit failed: {r.stderr[:300]}")
        job['commit_sha'] = _git_out(dir_, 'rev-parse', 'HEAD')
        job['commit_msg'] = msg
        job['approvals']['commit'] = True
        job['status'] = 'awaiting_push_approval'
        _append_job_line(job, f"committed {job['commit_sha'][:8]} on {job['branch']}")
        return {'ok': True, 'status': job['status'], 'sha': job['commit_sha']}
    if gate == 'push':
        if job['status'] != 'awaiting_push_approval':
            raise ValueError(f"cannot push from status {job['status']}")
        branch = job.get('branch') or 'staging'
        if branch in NEVER_BRANCHES:
            raise ValueError(f"refusing to push to {branch}")
        r = _git(job['directory'], 'push', 'origin', branch, timeout=600)
        if r.returncode != 0:
            job['last_error'] = f"push failed: {(r.stderr or r.stdout).strip()[:400]}"
            _append_job_line(job, job['last_error'])
            raise RuntimeError(job['last_error'])
        job['approvals']['push'] = True
        job['status'] = 'pushed'
        _append_job_line(job, f"pushed {branch} -> origin/{branch}")
        return {'ok': True, 'status': job['status']}
    raise ValueError(f"unknown gate {gate!r}")


# ── guardrail helpers for reads ────────────────────────────────────────
def _safe_job_path(job, rel):
    base = Path(job['directory']).resolve()
    target = (base / rel.lstrip('/')).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise ValueError('path escapes the worktree')
    return target


def _refresh_jobs_state(job):
    with _jobs_lock:
        _jobs[job['id']] = job
        _save_jobs()


# ── job creation ───────────────────────────────────────────────────────
def create_job(payload, user='said'):
    cfg = _config()
    if not cfg.get('projects_root'):
        raise ValueError('CODING_PROJECTS_ROOT is not configured')
    info = _find_repo((payload.get('repo') or '').strip())
    if not info:
        raise ValueError('repo not found in CODING_PROJECTS_ROOT')
    mode = (payload.get('mode') or 'build').strip()
    if mode not in ('plan', 'build'):
        raise ValueError(f"mode must be 'plan' or 'build'")
    task = (payload.get('task') or '').strip()
    if not task:
        raise ValueError('task is required')
    branch = (payload.get('branch') or 'staging').strip()
    if branch in NEVER_BRANCHES:
        raise ValueError(f"{branch} cannot be used directly as a coding target")
    requested_ref = (payload.get('ref') or '').strip() or None

    job_id = f"coding-{int(time.time())}-{secrets.token_hex(2)}"
    job = {
        'id': job_id,
        'repo': info['name'],
        'repo_path': info['path'],
        'mode': mode,
        'task': task,
        'branch': branch,
        'requested_ref': requested_ref,
        'status': 'created',
        'user': user,
        'created_at': _now_iso(),
        'updated_at': _now_iso(),
        'approvals': {'build': False, 'commit': False, 'push': False},
        'questions': [],
        'log_tail': [],
    }

    # attachments -> shared scratch area (never inside any repo worktree)
    files = payload.get('files') or []
    if len(files) > MAX_FILES:
        raise ValueError(f'max {MAX_FILES} attachments')
    attachments = []
    scratch = Path(cfg['worktrees_root']) / '_attachments' / job_id
    scratch.mkdir(parents=True, exist_ok=True)
    for idx, f in enumerate(files):
        name = re.sub(r'[^\w.\- ]+', '_', (f.get('name') or f'file-{idx}').strip())[:120]
        content = f.get('content_b64') or ''
        try:
            data = base64.b64decode(content)
        except Exception:
            raise ValueError('attachment is not valid base64')
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f'attachment {name} exceeds {MAX_FILE_BYTES // 1024 // 1024}MB')
        target = scratch / f"{idx:02d}_{name}"
        target.write_bytes(data)
        attachments.append({'name': name, 'path': str(target.resolve())})
    job['attachments'] = attachments

    _refresh_jobs_state(job)
    th = threading.Thread(target=_run_job, args=(job,), daemon=True)
    _running_threads[job['id']] = th
    th.start()
    return public_job(job)


def stop_job(job_id):
    job = _jobs.get(job_id)
    if not job:
        raise ValueError('unknown job')
    if job.get('status') not in ('failed', 'cancelled', 'pushed'):
        _stop_server(job)
        job['status'] = 'cancelled'
        _refresh_jobs_state(job)
    return public_job(job)


def delete_job(job_id):
    """Permanently remove a finished job: stop its server if any, delete its
    worktree if present, delete its job log, and drop it from the job store."""
    job = _jobs.get(job_id)
    if not job:
        raise ValueError('unknown job')
    if job.get('status') in ('planning', 'building', 'testing', 'pushing', 'running'):
        raise ValueError('stop the job first (it is still working)')
    try:
        _stop_server(job)
    except Exception:
        pass
    try:
        _cleanup_worktree(job)
    except Exception:
        pass
    try:
        cfg = _config()
        wt_root = Path(cfg['worktrees_root']).resolve()
        job_dir = Path(job['directory']).resolve() if job.get('directory') else None
        if job_dir and str(job_dir).lower().startswith(str(wt_root).lower()) and job_dir.exists():
            _git(Path(job['repo_path']), 'worktree', 'remove', '--force', str(job_dir), timeout=60)
    except Exception:
        pass
    try:
        job_log = _job_log(job_id)
        if job_log.exists():
            job_log.unlink()
    except Exception:
        pass
    _jobs.pop(job_id, None)
    _pids.pop(job_id, None)
    _save_jobs()
    return {'ok': True}


def prompt_job(job_id, text):
    job = _jobs.get(job_id)
    if not job:
        raise ValueError('unknown job')
    text = (text or '').strip()
    if not text:
        raise ValueError('empty prompt')
    if not job.get('opencode', {}).get('session_id'):
        raise ValueError('job has no live session')
    if _server_proc(job) is None:
        raise ValueError('job server is not running')

    def _do():
        try:
            _prompt(job, text)
            _refresh_jobs_state(job)
        except Exception as e:
            job['last_error'] = str(e)
            _append_job_line(job, f"follow-up prompt failed: {e}")
            _refresh_jobs_state(job)
    threading.Thread(target=_do, daemon=True).start()
    return {'ok': True}


def answer_permission(job_id, permission_id, response):
    job = _jobs.get(job_id)
    if not job:
        raise ValueError('unknown job')
    if response not in ('allowed', 'denied'):
        raise ValueError("response must be 'allowed' or 'denied'")
    sid = job.get('opencode', {}).get('session_id')
    if not sid:
        raise ValueError('no live session')
    r = _oc(job, 'POST', f'/session/{sid}/permissions/{permission_id}',
            json_body={'response': response, 'remember': False}, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"permission reply failed ({r.status_code}): {r.text[:300]}")
    for q in job.get('questions', []):
        if q.get('id') == permission_id:
            q['answered'] = True
    _append_job_line(job, f"permission {permission_id} -> {response}")
    _refresh_jobs_state(job)
    return {'ok': True}


def safe_read_file(job_id, rel):
    job = _jobs.get(job_id)
    if not job:
        raise ValueError('unknown job')
    if not job.get('directory'):
        raise ValueError('job has no worktree yet')
    target = _safe_job_path(job, rel)
    if not target.is_file():
        raise ValueError('not a file')
    return target.read_bytes()


def job_changed_files(job_id):
    job = _jobs.get(job_id)
    if not job:
        raise ValueError('unknown job')
    dir_ = job.get('directory')
    if not dir_:
        return []
    r = _git(dir_, 'status', '--short')
    if r.returncode != 0:
        return []
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    return [{'flag': l[:2].strip() or '??', 'path': l[3:].strip()} for l in lines]


def job_diff(job_id):
    job = _jobs.get(job_id)
    if not job:
        raise ValueError('unknown job')
    dir_ = job.get('directory')
    if not dir_:
        return {'error': 'no worktree yet'}
    base = job.get('diff_base') or 'HEAD'
    r = _git(dir_, 'diff', base, 'HEAD')
    tracked = (r.stdout or '') if r.returncode == 0 else ''
    files = job_changed_files(job_id)
    return {'diff': tracked[:80000], 'files': files}


# ── sweep / GC ─────────────────────────────────────────────────────────
def sweep():
    now = time.time()
    cfg = _config()
    ttl = cfg['job_ttl_hours'] * 3600
    with _jobs_lock:
        for job in list(_jobs.values()):
            try:
                if job.get('preview', {}).get('active') and \
                   now - (job.get('preview', {}).get('expires_at') or 0) > 0:
                    _stop_preview(job)
                if job.get('status') in ('failed', 'cancelled', 'pushed'):
                    started = job.get('created_at') or job.get('updated_at') or ''
                    try:
                        age = time.time() - datetime.fromisoformat(started).timestamp()
                    except Exception:
                        age = 0
                    if age > ttl:
                        p = _server_proc(job)
                        if p:
                            _kill_proc(p.pid)
                            _pids.pop(job['id'], None)
                        _stop_preview(job)
                        job.setdefault('cleaned', True)
                        job['status'] = job['status']
                        _cleanup_worktree(job)
            except Exception:
                continue
        _save_jobs()
    _repo_sweep()


def _sweep_loop():
    while True:
        try:
            sweep()
        except Exception:
            pass
        time.sleep(900)


class _ProcStub:
    """Allows reap of a server pid we don't own a Popen handle for (restart case)."""
    def __init__(self, pid):
        self.pid = pid


def start_background():
    _loads_jobs()
    _load_sessions()
    for job in _jobs.values():
        p = job.get('opencode', {})
        if p.get('pid'):
            _pids[job['id']] = _ProcStub(p['pid'])  # sweep can reap stale pids across restarts
    # restore repo-session pid stubs so a restart can reap stale repo servers
    for name, s in _sessions.items():
        if s.get('pid'):
            _rep_pids[name] = _ProcStub(s['pid'])
    threading.Thread(target=_sweep_loop, daemon=True).start()


# ── redirects / outputs ────────────────────────────────────────────────
def public_job(job):
    out = dict(job)
    oc = out.get('opencode') or {}
    if oc:
        out['opencode'] = {k: v for k, v in oc.items() if k != 'password'}
    return out


def list_jobs():
    jobs = sorted(_jobs.values(), key=lambda j: j.get('created_at') or '', reverse=True)
    return [public_job(j) for j in jobs]


def get_job(job_id):
    job = _jobs.get(job_id)
    if not job:
        raise ValueError('unknown job')
    return public_job(job)


def get_job_log(job_id):
    job = _jobs.get(job_id)
    if not job:
        raise ValueError('unknown job')
    tail = list(job.get('log_tail') or [])
    return {'job': job_id, 'tail': tail[-60:]}


# ── per-repo persistent "terminal" sessions ────────────────────────────
# One long-lived opencode server + session per repository, rooted in the
# real checkout. Sessions persist in opencode's SQLite store, so a session_id
# resumes full context across dashboard restarts.
REPO_SESSION_TTL = float(os.environ.get('CODING_REPO_SESSION_TTL_HOURS', '24'))


def _save_sessions():
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        slim = {}
        for name, s in _sessions.items():
            slim[name] = {k: s[k] for k in
                          ('session_id', 'mode', 'created_at', 'touched_at')
                          if k in s}
        REPO_SESSION_STATE.write_text(json.dumps(slim, ensure_ascii=False, indent=2),
                                      encoding='utf-8')
    except Exception as e:
        _log(f"failed to save coding sessions: {e}")


def _load_sessions():
    global _sessions
    try:
        _sessions = json.loads(REPO_SESSION_STATE.read_text(encoding='utf-8'))
        for k in list(_sessions):
            if not isinstance(_sessions.get(k), dict):
                _sessions.pop(k, None)
    except FileNotFoundError:
        _sessions = {}
    except Exception as e:
        _log(f"failed to load coding sessions: {e}")
        _sessions = {}


def _repo_session(name):
    return _sessions.get(name) or {}


def _repo_open_oc_base(repo):
    s = _sessions.get(repo) or {}
    return f"http://127.0.0.1:{s.get('port')}"


def _repo_auth(repo):
    s = _sessions.get(repo) or {}
    return (s.get('username') or 'opencode', s.get('password') or 'opencode')


def _repo_oc(repo, method, path, json_body=None, timeout=(15, 120)):
    return requests.request(method, _repo_open_oc_base(repo) + path,
                            auth=_repo_auth(repo), json=json_body, timeout=timeout)


def _repo_server_proc(repo):
    return _rep_pids.get(repo)


def _repo_refresh_messages(repo):
    """Pull the persistent session's message history into the session record."""
    s = _sessions.get(repo)
    if not s or not s.get('session_id') or not _repo_server_proc(repo):
        return
    try:
        r = _repo_oc(repo, 'GET', f"/session/{s['session_id']}/message?limit=100", timeout=30)
        if not r.ok:
            return
        data = r.json() or []
    except Exception:
        return
    msgs = []
    for m in data:
        info = m.get('info') or {}
        role = info.get('role')
        text = _extract_text(m.get('parts'))
        if role and text:
            msgs.append({'role': role, 'text': text})
    s['messages'] = msgs[-60:]
    s['updated_at'] = _now_iso()
    if not s.get('in_flight'):
        # idle: no streaming overlay should remain
        s.pop('live', None)
        s.pop('progress', None)


def _repo_sse_loop(repo, base, auth):
    while _repo_server_proc(repo):
        try:
            with requests.get(base + '/event', auth=auth, stream=True,
                              timeout=(10, 60)) as resp:
                if not resp.ok:
                    time.sleep(3)
                    continue
                for raw in resp.iter_lines(decode_unicode=True):
                    if not raw:
                        continue
                    line = raw.strip()
                    if line.startswith('data:'):
                        payload = line[len('data:'):].strip()
                        if not payload:
                            continue
                        try:
                            data = json.loads(payload) if payload else {}
                        except Exception:
                            continue
                        ev = str(data.get('type') or '')
                        props = data.get('properties') or data.get('propertiesHTML') or {}
                        if 'question' in ev.lower() or 'permission' in ev.lower():
                            pid = _find_permission_id(data)
                            text = json.dumps(data, ensure_ascii=False)[:500]
                            questions = _sessions[repo].setdefault('questions', [])
                            if pid and not any(q.get('id') == pid for q in questions):
                                questions.append({'id': pid, 'event': ev, 'text': text,
                                                  'answered': False, 'asked_at': _now_iso()})
                        elif ev == 'message.part.updated':
                            _track_live_part(repo, props)
                        elif ev == 'message.updated':
                            _track_live_message(repo, props)
                        elif ev == 'session.status':
                            _track_session_status(repo, props)
        except requests.exceptions.ReadTimeout:
            continue
        except Exception:
            if not _repo_server_proc(repo):
                break
            time.sleep(5)
            continue


def _track_live_part(repo, props):
    """Capture in-progress assistant reply + current tool/step for live UX overlay."""
    s = _sessions.get(repo)
    if not s:
        return
    part = (props or {}).get('part') or {}
    state = part.get('state') or 'streaming'
    ptype = part.get('type') or ''
    if ptype == 'text':
        if state == 'streaming':
            s['live'] = part.get('text') or ''
        elif state == 'completed' and s.get('live') is not None:
            s.pop('live', None)
        return
    # non-text parts (tool calls, reasoning) feed the progress line
    meta = (part.get('metadata') or {}).get('step') or {}
    cand = (meta.get('log') or '').strip()
    if not cand and ptype == 'tool':
        tool = part.get('tool') or ''
        label = {'bash': 'running a command…', 'read': 'reading a file…',
                 'edit': 'editing a file…', 'write': 'writing a file…',
                 'glob': 'searching files…', 'grep': 'searching code…',
                 'todowrite': 'updating the plan…'}.get(
            tool if not isinstance(tool, str) else tool, f'using {tool}…')
        cand = label
    if cand and len(cand) < 120 and not cand.startswith('{'):
        # surface even while a text part streams, so the user sees tool -> text flow
        s['progress'] = cand
    elif state == 'completed':
        s.pop('progress', None)


def _track_live_message(repo, props):
    """Use message.updated token/finish state to refine the progress line."""
    s = _sessions.get(repo)
    if not s:
        return
    info = (props or {}).get('info') or {}
    tokens = (info.get('tokens') or {}).get('total') or 0
    finish = info.get('finish') or ''
    if finish == 'stop' and tokens:
        s['last_tokens'] = tokens
        if not s.get('live'):
            s.pop('progress', None)


def _track_session_status(repo, props):
    """Flip busy/live overlay based on opencode's authoritative running state."""
    s = _sessions.get(repo)
    if not s:
        return
    status = (props or {}).get('status') or ''
    if status == 'idle':
        s.pop('live', None)
        s.pop('progress', None)
        s['sse_status'] = 'idle'


def _repo_start_sse(repo):
    if _rep_sse.get(repo) and _rep_sse[repo].is_alive():
        return
    t = threading.Thread(target=_repo_sse_loop,
                         args=(repo, _repo_open_oc_base(repo), _repo_auth(repo)),
                         daemon=True)
    t.start()
    _rep_sse[repo] = t


def _repo_kill_server(repo):
    proc = _repo_server_proc(repo)
    if proc:
        _kill_proc(proc.pid)
    _rep_pids.pop(repo, None)
    if _sessions.get(repo):
        _sessions[repo]['active'] = False
        _sessions[repo]['pid'] = None


def _ensure_repo_session(name):
    """Ensure a live opencode server + persistent session exist for `repo`.
    Roots the server in the REAL checkout; reuses the stored session_id."""
    info = _find_repo(name)
    if not info:
        raise ValueError('repo not found')
    repo_path = Path(info['path'])
    cfg = _config()

    with _sessions_lock:
        s = _sessions.setdefault(name, {
            'mode': 'build', 'active': False, 'messages': [], 'questions': [],
            'created_at': _now_iso(), 'touched_at': _now_iso(),
        })
        s['touched_at'] = _now_iso()

    # reuse a live server
    if s.get('active') and s.get('port') and _repo_server_proc(name):
        if not s.get('session_id'):
            raise RuntimeError('session id missing from live session')
        return s

    # spawn a fresh server rooted in the real checkout
    port = _alloc_port('repo-' + name, cfg)
    password = secrets.token_urlsafe(18)
    env = os.environ.copy()
    env['OPENCODE_SERVER_PASSWORD'] = password
    env['OPENCODE_SERVER_USERNAME'] = cfg['opencode_username']
    log_fh = open(_job_log(f'repo-{name}'), 'ab')
    try:
        proc = subprocess.Popen(
            [cfg['opencode_bin'], 'serve', '--hostname', '127.0.0.1', '--port', str(port)],
            cwd=str(repo_path), env=env, stdout=log_fh, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True)
    finally:
        log_fh.close()
    _rep_pids[name] = proc

    base = f"http://127.0.0.1:{port}"
    auth = (cfg['opencode_username'], password)
    deadline = time.time() + 90
    healthy = False
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            r = requests.get(base + '/global/health', auth=auth, timeout=3)
            if r.ok and r.json().get('healthy'):
                healthy = True
                break
        except Exception:
            pass
        time.sleep(1)
    if not healthy:
        _kill_proc(proc.pid)
        _rep_pids.pop(name, None)
        raise RuntimeError('opencode serve did not become healthy for repo ' + name)

    s['port'] = port
    s['password'] = password
    s['username'] = cfg['opencode_username']
    s['pid'] = proc.pid
    s['active'] = True

    # reuse the memorized session id, else create one
    if not s.get('session_id'):
        try:
            r = requests.post(base + '/session', auth=auth,
                              json={'title': f"repo {name}"}, timeout=30)
            r.raise_for_status()
            s['session_id'] = r.json().get('id')
        except Exception as e:
            _repo_kill_server(name)
            raise RuntimeError(f"failed to create opencode session for {name}: {e}")
        _save_sessions()

    _repo_start_sse(name)
    _repo_refresh_messages(name)
    return s


def repo_session_view(name):
    """Public view of a repo's persistent session (no secrets)."""
    _ensure_repo_session(name)
    s = _sessions[name]
    info = _find_repo(name) or {}
    busy = (s.get('in_flight') and
            time.time() - (s.get('in_flight_at') or 0) < 3600) or False
    return {'repo': name,
            'mode': s.get('mode', 'build'),
            'session_id': s.get('session_id'),
            'active': bool(s.get('active')),
            'busy': bool(busy),
            'live': s.get('live') or '',
            'progress': s.get('progress') or '',
            'messages': s.get('messages', []),
            'questions': [q for q in s.get('questions', []) if not q.get('answered')],
            'repo_info': info}


def _repo_send_worker(name, text, mode):
    """Background: POST the prompt to opencode, refresh messages, clear busy."""
    s = _sessions.get(name)
    if not s:
        return
    try:
        sid = s.get('session_id')
        if not sid:
            return
        if mode and mode in ('plan', 'build') and s.get('mode') != mode:
            s['mode'] = mode
            _save_sessions()
        mode = s.get('mode', 'build')
        system = PLAN_SYSTEM if mode == 'plan' else BUILD_SYSTEM
        agent = 'plan' if mode == 'plan' else 'build'
        r = _repo_oc(name, 'POST', f"/session/{sid}/message",
                     json_body={'system': system, 'agent': agent,
                                'parts': [{'type': 'text', 'text': text}]},
                     timeout=(15, 3600))
        if r.status_code >= 400:
            s['last_error'] = f"opencode prompt failed ({r.status_code}): {r.text[:200]}"
        else:
            s['last_error'] = None
            _repo_refresh_messages(name)
    except Exception as e:
        s['last_error'] = str(e)
    finally:
        s['in_flight'] = False
        s['in_flight_at'] = 0
        s.pop('live', None)
        s.pop('progress', None)
        _save_sessions()


def repo_message(name, text, mode=None):
    if not text or not text.strip():
        raise ValueError('empty prompt')
    _ensure_repo_session(name)
    s = _sessions[name]
    # don't stack two prompts on the same repo session
    if s.get('in_flight'):
        raise RuntimeError('repo is already working on a prompt - wait for it to finish')
    s['in_flight'] = True
    s['in_flight_at'] = time.time()
    s['last_error'] = None
    _save_sessions()
    t = threading.Thread(target=_repo_send_worker, args=(name, text, mode), daemon=True)
    t.start()
    _rep_threads[name] = t
    return repo_session_view(name)


def repo_set_mode(name, mode):
    if mode not in ('plan', 'build'):
        raise ValueError("mode must be 'plan' or 'build'")
    s = _sessions.setdefault(name, {'mode': 'build', 'active': False,
                                    'messages': [], 'questions': [],
                                    'created_at': _now_iso(), 'touched_at': _now_iso()})
    s['mode'] = mode
    s['touched_at'] = _now_iso()
    _save_sessions()
    return repo_session_view(name)


def repo_answer_permission(name, permission_id, response):
    if response not in ('allowed', 'denied'):
        raise ValueError("response must be 'allowed' or 'denied'")
    _ensure_repo_session(name)
    s = _sessions[name]
    sid = s.get('session_id')
    if not sid:
        raise ValueError('no live session')
    r = _repo_oc(name, 'POST', f'/session/{sid}/permissions/{permission_id}',
                 json_body={'response': response, 'remember': False}, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"permission reply failed ({r.status_code}): {r.text[:300]}")
    for q in s.get('questions', []):
        if q.get('id') == permission_id:
            q['answered'] = True
    _repo_refresh_messages(name)
    return {'ok': True}


def repo_reset(name):
    """Forget this repo's memorized session and start a fresh one."""
    _repo_kill_server(name)
    s = _sessions.get(name)
    if s:
        s['session_id'] = None
        s['messages'] = []
        s['questions'] = []
        s['mode'] = 'build'
        s['in_flight'] = False
        s['in_flight_at'] = 0
        s['last_error'] = None
        s['created_at'] = _now_iso()
    _save_sessions()
    return repo_session_view(name)


def repo_stop(name):
    _repo_kill_server(name)
    s = _sessions.get(name)
    if s:
        s['in_flight'] = False
        s['in_flight_at'] = 0
    _save_sessions()
    return {'ok': True}


def repo_diff(name):
    info = _find_repo(name)
    if not info:
        raise ValueError('repo not found')
    r = _git(info['path'], 'diff', 'HEAD')
    tracked = (r.stdout or '') if r.returncode == 0 else ''
    files = []
    st = _git(info['path'], 'status', '--short')
    if st.returncode == 0:
        files = [{'flag': l[:2].strip() or '??', 'path': l[3:].strip()}
                 for l in st.stdout.splitlines() if l.strip()]
    return {'diff': tracked[:80000], 'files': files}


def _repo_sweep():
    now = time.time()
    ttl = float(os.environ.get('CODING_REPO_SESSION_TTL_HOURS', '24')) * 3600
    with _sessions_lock:
        for name, s in list(_sessions.items()):
            if not s.get('active'):
                continue
            try:
                touched = datetime.fromisoformat(s.get('touched_at') or s.get('created_at')).timestamp()
            except Exception:
                touched = 0
            if now - touched > ttl:
                _repo_kill_server(name)


# ── HTTP routing hook (called from server.py handlers) ─────────────────
def route_get(handler):
    path = handler.path.split('?', 1)[0]
    if path == '/api/coding/repos':
        handler._send_json(200, json.dumps(_scan_repos(), ensure_ascii=False))
        return True
    if path.startswith('/api/coding/repos/'):
        parts = urllib.parse.unquote(path[len('/api/coding/repos/'):]).split('/')
        name = parts[0]
        sub = parts[1] if len(parts) > 1 else None
        try:
            if sub is None:
                info = _find_repo(name)
                if not info:
                    handler._send_json(404, json.dumps({'error': 'repo not found'}))
                else:
                    handler._send_json(200, json.dumps(info, ensure_ascii=False))
            elif sub == 'session':
                handler._send_json(200, json.dumps(repo_session_view(name), ensure_ascii=False))
            elif sub == 'diff':
                handler._send_json(200, json.dumps(repo_diff(name), ensure_ascii=False))
            else:
                handler._send_json(404, json.dumps({'error': 'not found'}))
        except ValueError as e:
            handler._send_json(400, json.dumps({'error': str(e)}))
        return True
    if path == '/api/coding/jobs':
        handler._send_json(200, json.dumps({'jobs': list_jobs()}, ensure_ascii=False))
        return True
    if path.startswith('/api/coding/jobs/'):
        rest = path[len('/api/coding/jobs/'):].split('/')
        job_id = rest[0]
        sub = rest[1] if len(rest) > 1 else None
        try:
            if sub is None:
                handler._send_json(200, json.dumps(get_job(job_id), ensure_ascii=False))
            elif sub == 'log':
                handler._send_json(200, json.dumps(get_job_log(job_id), ensure_ascii=False))
            elif sub == 'changed-files':
                handler._send_json(200, json.dumps({'files': job_changed_files(job_id)},
                                                   ensure_ascii=False))
            elif sub in ('diff', 'diff/'):
                handler._send_json(200, json.dumps(job_diff(job_id), ensure_ascii=False))
            elif sub == 'file':
                qs = {}
                for kv in (handler.path.split('?', 1)[1] if '?' in handler.path else '').split('&'):
                    if '=' in kv:
                        k, v = kv.split('=', 1)
                        qs[k] = urllib.parse.unquote(v)
                try:
                    data = safe_read_file(job_id, qs.get('path') or '')
                    handler.send_response(200)
                    handler.send_header('Content-Type', 'application/octet-stream')
                    handler.send_header('Access-Control-Allow-Origin', '*')
                    handler.end_headers()
                    handler.wfile.write(data)
                except ValueError as e:
                    handler._send_json(400, json.dumps({'error': str(e)}))
            else:
                handler._send_json(404, json.dumps({'error': 'not found'}))
        except ValueError as e:
            handler._send_json(404, json.dumps({'error': str(e)}))
        return True
    if path.startswith('/api/coding/preview/'):
        rest = path[len('/api/coding/preview/'):]
        job_id, _, sub = rest.partition('/')
        try:
            job = _jobs.get(job_id)
            pv = (job or {}).get('preview') or {}
            if not job or not pv.get('active'):
                handler._send_json(404, json.dumps({'error': 'no active preview'}))
                return True
            if time.time() > pv.get('expires_at', 0):
                _stop_preview(job)
                handler._send_json(410, json.dumps({'error': 'preview expired'}))
                return True
            upstream = f"http://127.0.0.1:{pv['port']}/{sub}"
            r = requests.get(upstream, timeout=60, stream=True)
            handler.send_response(r.status_code)
            for h in ('Content-Type', 'Content-Length'):
                if h in r.headers:
                    handler.send_header(h, r.headers[h])
            handler.send_header('Access-Control-Allow-Origin', '*')
            handler.end_headers()
            for chunk in r.iter_content(64 * 1024):
                handler.wfile.write(chunk)
        except Exception as e:
            handler._send_json(502, json.dumps({'error': 'preview proxy failed', 'details': str(e)}))
        return True
    return False


def route_post(handler):
    path = handler.path.split('?', 1)[0]
    preview_stop = False
    if path.startswith('/api/coding/repos/'):
        rest = urllib.parse.unquote(path[len('/api/coding/repos/'):]).split('/')
        name = rest[0]
        body = _read_json(handler)
        sub = rest[1] if len(rest) > 1 else None
        subsub = rest[2] if len(rest) > 2 else None
        try:
            if sub is None:
                rtype = (body or {}).get('type', '')
                info = _set_repo_type(name, rtype)
                handler._send_json(200, json.dumps({'ok': True, 'repo': info}, ensure_ascii=False))
            elif sub == 'session':
                if subsub == 'message':
                    handler._send_json(200, json.dumps(
                        repo_message(name, (body or {}).get('text'),
                                     (body or {}).get('mode')), ensure_ascii=False))
                elif subsub == 'mode':
                    handler._send_json(200, json.dumps(
                        repo_set_mode(name, (body or {}).get('mode')), ensure_ascii=False))
                elif subsub == 'reset':
                    handler._send_json(200, json.dumps(repo_reset(name), ensure_ascii=False))
                elif subsub == 'stop':
                    handler._send_json(200, json.dumps(repo_stop(name), ensure_ascii=False))
                else:
                    handler._send_json(404, json.dumps({'error': 'not found'}))
            elif sub == 'permission':
                handler._send_json(200, json.dumps(
                    repo_answer_permission(name, (body or {}).get('permission_id'),
                                           (body or {}).get('response')), ensure_ascii=False))
            else:
                handler._send_json(404, json.dumps({'error': 'not found'}))
        except ValueError as e:
            handler._send_json(400, json.dumps({'error': str(e)}))
        except RuntimeError as e:
            handler._send_json(409, json.dumps({'error': str(e)}))
        return True
    if path == '/api/coding/jobs':
        body = _read_json(handler)
        try:
            obj = create_job(body)
            handler._send_json(200, json.dumps({'ok': True, 'job': obj}, ensure_ascii=False))
        except ValueError as e:
            handler._send_json(400, json.dumps({'error': str(e)}))
        return True
    if path.startswith('/api/coding/jobs/'):
        rest = path[len('/api/coding/jobs/'):].split('/')
        job_id = rest[0]
        sub = rest[1] if len(rest) > 1 else None
        body = _read_json(handler)
        try:
            if sub == 'prompt':
                handler._send_json(200, json.dumps(prompt_job(job_id, (body or {}).get('text')),
                                                   ensure_ascii=False))
            elif sub == 'stop':
                handler._send_json(200, json.dumps(stop_job(job_id), ensure_ascii=False))
            elif sub == 'delete':
                handler._send_json(200, json.dumps(delete_job(job_id), ensure_ascii=False))
            elif sub == 'approve':
                gate = (body or {}).get('gate')
                if gate not in GATES:
                    handler._send_json(400, json.dumps({'error': f"gate must be one of {GATES}"}))
                else:
                    handler._send_json(200, json.dumps(approve_gate(_jobs.get(job_id), gate),
                                                       ensure_ascii=False))
            elif sub == 'permission':
                handler._send_json(200, json.dumps(
                    answer_permission(job_id, (body or {}).get('permission_id'),
                                      (body or {}).get('response')), ensure_ascii=False))
            elif sub == 'preview' and len(rest) > 2 and rest[2] == 'start':
                try:
                    handler._send_json(200, json.dumps(_start_preview(_jobs.get(job_id)),
                                                       ensure_ascii=False))
                except ValueError as e:
                    handler._send_json(400, json.dumps({'error': str(e)}))
            elif sub == 'preview' and len(rest) > 2 and rest[2] == 'stop':
                _stop_preview(_jobs.get(job_id))
                handler._send_json(200, json.dumps({'ok': True}))
            else:
                handler._send_json(404, json.dumps({'error': 'not found'}))
        except ValueError as e:
            handler._send_json(400, json.dumps({'error': str(e)}))
        except RuntimeError as e:
            handler._send_json(409, json.dumps({'error': str(e)}))
        return True
    return False


def _read_json(handler):
    try:
        length = int(handler.headers.get('Content-Length', 0))
        if length <= 0:
            return {}
        return json.loads(handler.rfile.read(length).decode('utf-8'))
    except Exception:
        return {}