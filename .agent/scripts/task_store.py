#!/usr/bin/env python3
"""
task_store.py — Universal Task Store

Manages journal/state/tasks.json: the single normalized task store that every
source producer writes to and the Inbox Engine reads from.

Every task follows the Universal Task Schema regardless of origin (meeting,
mattermost, trello, gmail, gitlab, manual).

Usage:
    from task_store import append_task, get_tasks, update_status, get_by_id

    # Producer appends a normalized task (dedupe handled automatically)
    task = append_task({
        "workspace": "catalyze",
        "source": "meeting",
        "source_ref": "https://fathom.video/xyz123#t=932",
        "source_id": "rec_abc123",
        "project": "ABNJ",
        "title": "Fix SSO redirect bug",
        "description": "...",
        "priority": "unknown",
        "requester": "Andry Muharyo",
        "assignee": "Said Iskandar",
        "due_date": "2026-08-07",
        "meeting_id": "fathom:catalyze:rec_abc123",
        "confidence": 0.95,
    })
    # Returns the task dict with id, dedupe_key, timestamps — or None if duplicate

    # Inbox Engine reads all tasks
    tasks = get_tasks(workspace="catalyze", status="new")

    # User acts on a task
    update_status("TASK-0001", "in_progress")
    update_status("TASK-0001", "done")
"""
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, '..', '..'))
STATE_PATH = os.path.join(REPO_ROOT, 'journal', 'state', 'tasks.json')
WIB = timezone(timedelta(hours=7))

VALID_SOURCES = ('meeting', 'mattermost', 'trello', 'gmail', 'gitlab', 'manual')
VALID_STATUSES = ('new', 'open', 'in_progress', 'done', 'dismissed')
VALID_PRIORITIES = ('high', 'medium', 'low', 'unknown')

# ---------- state management ----------

def _load():
    try:
        with open(STATE_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {'next_seq': 1, 'tasks': {}}


def _save(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def _now():
    return datetime.now(WIB).isoformat(timespec='seconds')


def _next_id(state):
    seq = state.get('next_seq', 1)
    task_id = f"TASK-{seq:04d}"
    state['next_seq'] = seq + 1
    return task_id


def _compute_dedupe_key(source, source_id, title, description=''):
    """Deterministic dedupe key: source + source_id + content hash."""
    content = (title or '').lower().strip() + '|' + (description or '').lower().strip()
    content_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()[:12]
    src_id = source_id or 'nosrc'
    return f"{source}:{src_id}:{content_hash}"


def _is_duplicate(state, dedupe_key):
    """Check if a task with this dedupe_key already exists."""
    for task in state.get('tasks', {}).values():
        if task.get('dedupe_key') == dedupe_key:
            return True
    return False

# ---------- public API ----------

def append_task(data):
    """Append a normalized task to the store.

    Args:
        data: dict with task fields (see Universal Task Schema).
              Required: workspace, source, title, confidence.
              All other fields optional.

    Returns:
        The complete task dict (with id, dedupe_key, timestamps) if new.
        None if the task is a duplicate (same dedupe_key exists).
    """
    state = _load()

    source = data.get('source', 'manual')
    source_id = data.get('source_id', '')
    title = data.get('title', '')
    description = data.get('description', '')

    if not title:
        raise ValueError("Task title is required")
    if source not in VALID_SOURCES:
        raise ValueError(f"Invalid source '{source}'. Must be one of: {VALID_SOURCES}")

    dedupe_key = data.get('dedupe_key') or _compute_dedupe_key(source, source_id, title, description)

    if _is_duplicate(state, dedupe_key):
        return None

    task_id = _next_id(state)
    now = _now()

    task = {
        'id': task_id,
        'dedupe_key': dedupe_key,
        'workspace': data.get('workspace', ''),
        'source': source,
        'source_ref': data.get('source_ref', ''),
        'source_id': source_id,
        'project': data.get('project', ''),
        'title': title,
        'description': description,
        'priority': data.get('priority', 'unknown'),
        'requester': data.get('requester', ''),
        'assignee': data.get('assignee', ''),
        'due_date': data.get('due_date'),
        'meeting_id': data.get('meeting_id', ''),
        'status': 'new',
        'confidence': data.get('confidence', 0.5),
        'created_at': now,
        'updated_at': now,
        'metadata': data.get('metadata', {}),
    }

    # Validate priority
    if task['priority'] not in VALID_PRIORITIES:
        task['priority'] = 'unknown'

    state['tasks'][task_id] = task
    _save(state)
    return task


def get_tasks(workspace=None, source=None, status=None, project=None, limit=50):
    """Read tasks with optional filters.

    Returns a list of task dicts, sorted newest first.
    """
    state = _load()
    tasks = list(state.get('tasks', {}).values())

    if workspace:
        tasks = [t for t in tasks if t.get('workspace') == workspace]
    if source:
        tasks = [t for t in tasks if t.get('source') == source]
    if status:
        if isinstance(status, str):
            tasks = [t for t in tasks if t.get('status') == status]
        elif isinstance(status, (list, tuple)):
            tasks = [t for t in tasks if t.get('status') in status]
    if project:
        tasks = [t for t in tasks if project.lower() in (t.get('project') or '').lower()]

    tasks.sort(key=lambda t: t.get('created_at', ''), reverse=True)
    return tasks[:limit]


def get_by_id(task_id):
    """Get a single task by ID. Returns None if not found."""
    state = _load()
    return state.get('tasks', {}).get(task_id)


def update_status(task_id, new_status):
    """Update a task's status. Returns the updated task or None."""
    if new_status not in VALID_STATUSES:
        raise ValueError(f"Invalid status '{new_status}'. Must be one of: {VALID_STATUSES}")

    state = _load()
    task = state.get('tasks', {}).get(task_id)
    if not task:
        return None

    task['status'] = new_status
    task['updated_at'] = _now()
    _save(state)
    return task


def update_task(task_id, updates):
    """Update arbitrary fields on a task. Returns the updated task or None."""
    state = _load()
    task = state.get('tasks', {}).get(task_id)
    if not task:
        return None

    allowed = ('priority', 'assignee', 'due_date', 'project', 'status',
               'linked_dev_session', 'linked_ticket', 'metadata')
    for k, v in updates.items():
        if k in allowed:
            task[k] = v
    task['updated_at'] = _now()
    _save(state)
    return task


def count_by_status(workspace=None):
    """Count tasks grouped by status. Returns dict like {'new': 3, 'open': 1, ...}."""
    state = _load()
    tasks = state.get('tasks', {}).values()
    if workspace:
        tasks = [t for t in tasks if t.get('workspace') == workspace]
    counts = {}
    for t in tasks:
        s = t.get('status', 'unknown')
        counts[s] = counts.get(s, 0) + 1
    return counts


# ---------- CLI ----------

def main():
    """Simple CLI for inspection and testing."""
    import argparse
    p = argparse.ArgumentParser(description='Universal Task Store')
    sub = p.add_subparsers(dest='cmd')

    sub.add_parser('list', help='List tasks')
    sub.add_parser('count', help='Count by status')

    sp = sub.add_parser('status', help='Update task status')
    sp.add_argument('task_id')
    sp.add_argument('new_status', choices=VALID_STATUSES)

    args = p.parse_args()

    if args.cmd == 'list':
        tasks = get_tasks()
        if not tasks:
            print("No tasks in store.")
            return
        print(f"{'ID':<12} {'Status':<12} {'Source':<12} {'Workspace':<12} {'Title'}")
        print("-" * 80)
        for t in tasks:
            print(f"{t['id']:<12} {t['status']:<12} {t['source']:<12} "
                  f"{t['workspace']:<12} {t['title'][:40]}")

    elif args.cmd == 'count':
        counts = count_by_status()
        print("Tasks by status:")
        for s, n in sorted(counts.items()):
            print(f"  {s}: {n}")

    elif args.cmd == 'status':
        result = update_status(args.task_id, args.new_status)
        if result:
            print(f"Updated {args.task_id} -> {args.new_status}")
        else:
            print(f"Task {args.task_id} not found.")

    else:
        p.print_help()


if __name__ == '__main__':
    main()
