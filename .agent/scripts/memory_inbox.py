#!/usr/bin/env python3
"""memory_inbox.py - Memory Inbox storage layer.

Routes classified notes to the correct storage:
  - definition/fact/observation/strategy/decision/project_knowledge -> knowledge_store.py
  - task -> task_store.py
  - milestone -> milestones.json
  - reminder -> reminders.json

Maintains a memory_notes.json index for browsing/editing/deactivating notes.

Usage:
  python3 memory_inbox.py store --workspace samudera --classification '{"type":"definition",...}'
  python3 memory_inbox.py list --workspace samudera
  python3 memory_inbox.py edit --workspace samudera --id MEM-0001 --text "new text"
  python3 memory_inbox.py deactivate --workspace samudera --id MEM-0001
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
if sys.stderr.encoding != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8')

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR / '.agent' / 'scripts'))
sys.path.insert(0, str(BASE_DIR / '.agent' / 'workspaces'))

try:
    import workspace_resolver as ws
except ImportError:
    ws = None

import brain_store

WIB = timezone(timedelta(hours=7))
MEMORY_NOTES_FILE = 'memory_notes.json'


def _ws_dir(ws_name):
    if ws:
        ctx = ws.get(ws_name)
        return ctx.dir
    return str(BASE_DIR / '.agent' / 'workspaces' / ws_name)


def _load_json(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return None


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _next_id(data):
    seq = data.get('next_seq', 1)
    data['next_seq'] = seq + 1
    return 'MEM-%04d' % seq


def _now_wib():
    return datetime.now(WIB).isoformat(timespec='seconds')


def _content_hash(text):
    normalized = re.sub(r'\s+', ' ', text.lower().strip())
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:16]


def _store_to_knowledge(cls, ws_name):
    script = str(BASE_DIR / '.agent' / 'skills' / 'knowledge-store' / 'scripts' / 'knowledge_store.py')
    category = cls.get('category') or 'misc'
    title = cls.get('title', '')
    content = cls.get('content', '')
    tags = cls.get('entities', [])

    cmd = [sys.executable, script, 'add',
           '--workspace', ws_name,
           '--category', category,
           '--content', content,
           '--source', 'user_note',
           '--confidence', cls.get('confidence', 'high')]
    if title:
        cmd.extend(['--title', title])
    if tags:
        cmd.extend(['--tags', ','.join(tags)])

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(BASE_DIR), timeout=30)
        output = r.stdout + r.stderr
        is_dup = 'Duplicate detected' in output
        return {'ok': r.returncode == 0, 'duplicate': is_dup, 'output': output.strip()}
    except Exception as e:
        return {'ok': False, 'duplicate': False, 'output': str(e)}


def _store_to_task(cls, ws_name):
    title = cls.get('title', cls.get('content', '')[:80])
    desc = cls.get('content', '')
    due = cls.get('date')
    project = cls.get('project', '')

    data = {
        'workspace': ws_name,
        'source': 'manual',
        'title': title,
        'description': desc,
        'priority': 'medium',
        'status': 'new',
        'confidence': 0.8,
    }
    if due:
        data['due_date'] = due
    if project:
        data['project'] = project

    try:
        sys.path.insert(0, str(BASE_DIR / '.agent' / 'scripts'))
        import task_store
        task = task_store.append_task(data)
        if task:
            return {'ok': True, 'task_id': task['id']}
        return {'ok': False, 'output': 'duplicate or error'}
    except Exception as e:
        return {'ok': False, 'output': str(e)}


def _store_to_milestones(cls, ws_name):
    state_dir = os.path.join(_ws_dir(ws_name), 'state')
    path = os.path.join(state_dir, 'milestones.json')
    data = _load_json(path) or {'next_seq': 1, 'entries': {}}

    seq = data.get('next_seq', 1)
    mid = 'MS-%04d' % seq
    data['next_seq'] = seq + 1

    entry = {
        'id': mid,
        'title': cls.get('title', ''),
        'content': cls.get('content', ''),
        'project': cls.get('project', ''),
        'date': cls.get('date', ''),
        'status': 'planned',
        'source': 'user_note',
        'created_wib': _now_wib(),
    }
    data['entries'][mid] = entry
    _save_json(path, data)
    return {'ok': True, 'milestone_id': mid}


def _store_to_reminders(cls, ws_name):
    """Delegate to reminder_engine — the single reminders store (personal
    brain, real date parsing, served via /api/reminders). Ignores ws_name:
    reminders are always personal, never office-scoped."""
    try:
        import reminder_engine
        text = (cls.get('content') or cls.get('title') or '').strip()
        due = cls.get('due_date')
        if not due:
            try:
                due = reminder_engine.parse_due(text)
            except Exception:
                due = None
        entry = reminder_engine.add(text, due=due, source='note')
        return {'ok': True, 'reminder_id': entry['id']}
    except Exception as e:
        return {'ok': False, 'output': str(e)}


def _rebuild_faiss(ws_name):
    script = str(BASE_DIR / '.agent' / 'skills' / 'knowledge-store' / 'scripts' / 'embedding_index.py')
    if not os.path.exists(script):
        return
    try:
        subprocess.Popen(
            [sys.executable, script, 'build', '--workspace', ws_name],
            cwd=str(BASE_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def store_note(ws_name, classification):
    """Store into the ONE canonical brain. ws_name is provenance only —
    it stamps source_ws and routes work trackers; it never splits storage."""
    mem_type = classification.get('type', 'fact')
    text = classification.get('content', '')

    notes_data = brain_store.load_notes()
    hash_val = brain_store.content_hash(text)
    existing = brain_store.find_by_hash(notes_data, hash_val)
    if existing:
        result = {'ok': True, 'duplicate': True, 'note_id': existing['id'],
                  'message': 'This note already exists'}
        # A duplicate note must still guarantee its reminder exists —
        # older notes may predate the reminder engine entirely.
        if mem_type == 'reminder':
            backend = _store_to_reminders(classification, ws_name)
            result['stored_to'] = 'reminder'
            result['stored_id'] = backend.get('reminder_id', '')
            if backend.get('reminder_id'):
                result['message'] = 'Reminder already active'
        return result

    stored_to = None
    stored_id = None
    backend_result = None

    if mem_type in ('definition', 'fact', 'project_knowledge', 'decision', 'observation', 'strategy'):
        backend_result = _store_to_knowledge(classification, ws_name)
        stored_to = 'knowledge'
        stored_id = classification.get('category', 'misc')
        if backend_result.get('duplicate'):
            return {'ok': True, 'duplicate': True, 'message': 'Duplicate knowledge entry'}
        _rebuild_faiss(ws_name)

    elif mem_type == 'task':
        backend_result = _store_to_task(classification, ws_name)
        stored_to = 'task'
        stored_id = backend_result.get('task_id', '')

    elif mem_type == 'milestone':
        backend_result = _store_to_milestones(classification, ws_name)
        stored_to = 'milestone'
        stored_id = backend_result.get('milestone_id', '')

    elif mem_type == 'reminder':
        backend_result = _store_to_reminders(classification, ws_name)
        stored_to = 'reminder'
        stored_id = backend_result.get('reminder_id', '')

    else:
        classification['category'] = 'misc'
        backend_result = _store_to_knowledge(classification, ws_name)
        stored_to = 'knowledge'
        stored_id = 'misc'
        _rebuild_faiss(ws_name)

    note_id = brain_store.next_id(notes_data)
    note_entry = {
        'id': note_id,
        'text': classification.get('content', text),
        'type': mem_type,
        'title': classification.get('title', ''),
        'entities': classification.get('entities', []),
        'project': classification.get('project'),
        'category': classification.get('category'),
        'date': classification.get('date'),
        'source_ws': ws_name or 'personal',
        'scope': classification.get('scope') or brain_store.scope_for(text, ws_name),
        'stored_to': stored_to,
        'stored_id': stored_id,
        'status': 'active',
        'hash': hash_val,
        'created_wib': _now_wib(),
        'updated_wib': None,
    }
    notes_data['entries'][note_id] = note_entry
    brain_store.save_notes(notes_data)

    return {
        'ok': True,
        'duplicate': False,
        'note_id': note_id,
        'type': mem_type,
        'title': classification.get('title', ''),
        'summary': classification.get('summary', ''),
        'stored_to': stored_to,
        'stored_id': stored_id,
        'project': classification.get('project'),
        'entities': classification.get('entities', []),
        'date': classification.get('date'),
    }


def list_notes(ws_name, limit=20, mem_type=None, status='active'):
    """List from the ONE brain. ws_name only filters private scope."""
    return brain_store.list_notes(limit=limit, mem_type=mem_type,
                                  status=status, view=ws_name)


def edit_note(ws_name, note_id, new_text):
    notes_data = brain_store.load_notes()

    entry = notes_data.get('entries', {}).get(note_id)
    if not entry:
        return {'ok': False, 'error': 'Note not found'}

    entry['status'] = 'superseded'
    entry['updated_wib'] = _now_wib()
    brain_store.save_notes(notes_data)

    sys.path.insert(0, str(BASE_DIR / '.agent' / 'scripts'))
    from memory_classifier import classify
    new_cls = classify(new_text)
    return store_note(ws_name, new_cls)


def deactivate_note(ws_name, note_id):
    notes_data = brain_store.load_notes()

    entry = notes_data.get('entries', {}).get(note_id)
    if not entry:
        return {'ok': False, 'error': 'Note not found'}

    entry['status'] = 'inactive'
    entry['updated_wib'] = _now_wib()
    brain_store.save_notes(notes_data)
    return {'ok': True, 'note_id': note_id}


def get_stats(ws_name):
    return brain_store.get_stats(view=ws_name)


def cmd_store(args):
    cls = json.loads(args.classification)
    result = store_note(args.workspace, cls)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_list(args):
    entries = list_notes(args.workspace, limit=args.limit)
    print(json.dumps(entries, ensure_ascii=False, indent=2))


def cmd_edit(args):
    result = edit_note(args.workspace, args.id, args.text)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_deactivate(args):
    result = deactivate_note(args.workspace, args.id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_stats(args):
    result = get_stats(args.workspace)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    p = argparse.ArgumentParser(description='Memory Inbox')
    sub = p.add_subparsers(dest='cmd')

    st = sub.add_parser('store')
    st.add_argument('--workspace', default='samudera')
    st.add_argument('--classification', required=True)

    li = sub.add_parser('list')
    li.add_argument('--workspace', default='samudera')
    li.add_argument('--limit', type=int, default=20)

    ed = sub.add_parser('edit')
    ed.add_argument('--workspace', default='samudera')
    ed.add_argument('--id', required=True)
    ed.add_argument('--text', required=True)

    de = sub.add_parser('deactivate')
    de.add_argument('--workspace', default='samudera')
    de.add_argument('--id', required=True)

    stats = sub.add_parser('stats')
    stats.add_argument('--workspace', default='samudera')

    args = p.parse_args()
    handlers = {
        'store': cmd_store, 'list': cmd_list, 'edit': cmd_edit,
        'deactivate': cmd_deactivate, 'stats': cmd_stats,
    }
    handler = handlers.get(args.cmd)
    if handler:
        handler(args)
    else:
        p.print_help()


if __name__ == '__main__':
    main()
