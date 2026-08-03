#!/usr/bin/env python3
"""
meeting_engine.py — Meeting Intelligence Engine orchestrator.

Fetches meetings from configured sources, stores immutable records, and
extracts normalized tasks + knowledge.

Usage:
    meeting_engine.py sync [--workspace X] [--since YYYY-MM-DD] [--limit N]
    meeting_engine.py ingest --title "..." --file path [--workspace X] [--project P] [--attendees "A,B"]
    meeting_engine.py list [--workspace X] [--limit N]
    meeting_engine.py tasks [--workspace X]
    meeting_engine.py extract --meeting-id <id>
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, '..', '..', '..', '..'))

sys.path.insert(0, os.path.join(REPO_ROOT, '.agent', 'workspaces'))
sys.path.insert(0, os.path.join(REPO_ROOT, '.agent', 'scripts'))
sys.path.insert(0, SCRIPT_DIR)

import workspace_resolver as ws
import task_store
from adapters.fathom_adapter import FathomAdapter
from adapters.manual_adapter import ManualAdapter
from extractor import extract_from_meeting

WIB = timezone(timedelta(hours=7))
MEETINGS_BASE = os.path.join(REPO_ROOT, 'journal', 'meetings')

# UTF-8 output on Windows
try:
    if sys.stdout.encoding != 'utf-8':
        sys.stdout.reconfigure(encoding='utf-8')
    if sys.stderr.encoding != 'utf-8':
        sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

# ---------- meeting store ----------

def _slugify(text):
    return re.sub(r'[^a-z0-9]+', '-', text.lower().strip()).strip('-')[:50]


def _meeting_dir(workspace, date_str):
    """Return the directory path for a meeting by workspace and date."""
    year = date_str[:4] if len(date_str) >= 4 else datetime.now(WIB).strftime('%Y')
    month = date_str[5:7] if len(date_str) >= 7 else datetime.now(WIB).strftime('%m')
    return os.path.join(MEETINGS_BASE, workspace, year, month)


def _store_meeting(record_dict):
    """Store a meeting record as an immutable JSON file.

    Returns the stored file path (relative to repo root).
    """
    workspace = record_dict.get('workspace', 'unknown')
    date_str = record_dict.get('date', datetime.now(WIB).strftime('%Y-%m-%d'))
    title = record_dict.get('title', 'meeting')
    slug = _slugify(title)

    directory = _meeting_dir(workspace, date_str)
    os.makedirs(directory, exist_ok=True)

    filename = f"{date_str}_{slug}.json"
    filepath = os.path.join(directory, filename)

    # Don't overwrite existing (immutable store)
    if os.path.exists(filepath):
        return os.path.relpath(filepath, REPO_ROOT).replace('\\', '/')

    record_dict['stored_path'] = os.path.relpath(filepath, REPO_ROOT).replace('\\', '/')

    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(record_dict, f, ensure_ascii=False, indent=2)

    return record_dict['stored_path']


def _is_meeting_stored(meeting_id, workspace):
    """Check if a meeting with this ID has already been stored."""
    ws_dir = os.path.join(MEETINGS_BASE, workspace)
    if not os.path.exists(ws_dir):
        return False

    for root, dirs, files in os.walk(ws_dir):
        for f in files:
            if f.endswith('.json'):
                try:
                    path = os.path.join(root, f)
                    with open(path, encoding='utf-8') as fh:
                        data = json.load(fh)
                    if data.get('id') == meeting_id:
                        return True
                except Exception:
                    continue
    return False


def _load_stored_meeting(meeting_id, workspace):
    """Load a stored meeting record by ID."""
    ws_dir = os.path.join(MEETINGS_BASE, workspace)
    if not os.path.exists(ws_dir):
        return None

    for root, dirs, files in os.walk(ws_dir):
        for f in files:
            if f.endswith('.json'):
                try:
                    path = os.path.join(root, f)
                    with open(path, encoding='utf-8') as fh:
                        data = json.load(fh)
                    if data.get('id') == meeting_id:
                        return data
                except Exception:
                    continue
    return None

# ---------- commands ----------

def cmd_sync(args):
    """Fetch recent meetings from all configured sources, store, and extract."""
    ctx = ws.get(args.workspace)
    print(f"[{ctx.name}] Syncing meetings...", file=sys.stderr)

    # Determine which adapters to use based on workspace tools
    adapters = []
    if ctx.has_tool('fathom'):
        adapters.append(('fathom', FathomAdapter()))

    if not adapters:
        print(f"[{ctx.name}] No meeting sources configured. Add fathom.env to workspace.")
        return

    total_new = 0
    total_skipped = 0
    total_tasks = 0

    for source_name, adapter in adapters:
        print(f"[{ctx.name}] Fetching from {source_name}...", file=sys.stderr)
        records = adapter.fetch_recent(ctx, since=args.since, limit=args.limit)

        for record in records:
            record_dict = record.to_dict() if hasattr(record, 'to_dict') else record

            # Check if already stored
            if _is_meeting_stored(record_dict['id'], ctx.name):
                total_skipped += 1
                continue

            # Store
            stored_path = _store_meeting(record_dict)
            total_new += 1

            # Extract knowledge
            stats = extract_from_meeting(record_dict)
            total_tasks += stats['tasks_created']

            print(f"  [{record_dict['date']}] {record_dict['title']}")
            print(f"    Stored: {stored_path}")
            print(f"    Tasks: {stats['tasks_created']} new, {stats['tasks_skipped']} skipped")
            if stats['decisions']:
                print(f"    Decisions: {stats['decisions']}")

    print(f"\n[{ctx.name}] Sync complete: "
          f"{total_new} new meetings, {total_skipped} already stored, "
          f"{total_tasks} tasks extracted.")


def cmd_ingest(args):
    """Ingest a manual meeting notes file."""
    ctx = ws.get(args.workspace)

    adapter = ManualAdapter()
    record = adapter.from_file(ctx, args.file, args.title,
                               project=args.project or '',
                               attendees_str=args.attendees or '')
    if not record:
        print(f"Error: could not read file '{args.file}'", file=sys.stderr)
        sys.exit(1)

    record_dict = record.to_dict()

    # Store
    stored_path = _store_meeting(record_dict)
    print(f"[{ctx.name}] Stored: {stored_path}")

    # Extract
    stats = extract_from_meeting(record_dict)
    print(f"  Tasks: {stats['tasks_created']} new, {stats['tasks_skipped']} skipped")


def cmd_list(args):
    """List stored meetings for the active workspace."""
    ctx = ws.get(args.workspace)
    ws_dir = os.path.join(MEETINGS_BASE, ctx.name)

    if not os.path.exists(ws_dir):
        print(f"[{ctx.name}] No meetings stored yet.")
        return

    meetings = []
    for root, dirs, files in os.walk(ws_dir):
        for f in files:
            if f.endswith('.json'):
                try:
                    path = os.path.join(root, f)
                    with open(path, encoding='utf-8') as fh:
                        data = json.load(fh)
                    meetings.append(data)
                except Exception:
                    continue

    meetings.sort(key=lambda m: m.get('date', ''), reverse=True)
    meetings = meetings[:args.limit]

    if not meetings:
        print(f"[{ctx.name}] No meetings stored.")
        return

    print(f"[{ctx.name}] Stored meetings ({len(meetings)}):\n")
    for m in meetings:
        n_actions = len(m.get('action_items', []))
        n_attendees = len(m.get('attendees', []))
        dur = m.get('duration_minutes', 0)
        dur_str = f"{dur}min" if dur else ''
        print(f"  [{m.get('date', '?')}] {m.get('title', '?')} "
              f"({m.get('source', '?')}) {dur_str}")
        print(f"    ID: {m.get('id', '?')}  attendees: {n_attendees}  actions: {n_actions}")


def cmd_tasks(args):
    """Show tasks extracted from meetings."""
    ctx = ws.get(args.workspace)
    tasks = task_store.get_tasks(workspace=ctx.name, source='meeting', limit=30)

    if not tasks:
        print(f"[{ctx.name}] No meeting-sourced tasks.")
        return

    print(f"[{ctx.name}] Meeting tasks ({len(tasks)}):\n")
    for t in tasks:
        status_icon = {'new': '○', 'open': '◐', 'in_progress': '●', 'done': '✓', 'dismissed': '✗'}
        icon = status_icon.get(t['status'], '?')
        meta = t.get('metadata', {})
        meeting_info = f" (from: {meta.get('meeting_title', '?')}, {meta.get('meeting_date', '')})"
        print(f"  {icon} [{t['id']}] {t['title'][:60]}")
        print(f"    assignee: {t.get('assignee', '-')}  "
              f"conf: {t.get('confidence', 0):.0%}  "
              f"status: {t['status']}{meeting_info}")


def cmd_extract(args):
    """Re-run extraction on an already stored meeting."""
    ctx = ws.get(args.workspace)
    record = _load_stored_meeting(args.meeting_id, ctx.name)

    if not record:
        print(f"Error: meeting '{args.meeting_id}' not found in {ctx.name} store.",
              file=sys.stderr)
        sys.exit(1)

    stats = extract_from_meeting(record)
    print(f"[{ctx.name}] Re-extracted from: {record.get('title', '?')}")
    print(f"  Tasks: {stats['tasks_created']} new, {stats['tasks_skipped']} skipped")
    if stats['decisions']:
        print(f"  Decisions: {stats['decisions']}")

# ---------- CLI ----------

def main():
    p = argparse.ArgumentParser(description='Meeting Intelligence Engine')
    sub = p.add_subparsers(dest='cmd')

    # sync
    sp = sub.add_parser('sync', help='Fetch + store + extract from configured sources')
    sp.add_argument('--workspace', default=None)
    sp.add_argument('--since', help='Only meetings after this date (ISO)')
    sp.add_argument('--limit', type=int, default=10)

    # ingest
    ip = sub.add_parser('ingest', help='Ingest manual meeting notes')
    ip.add_argument('--workspace', default=None)
    ip.add_argument('--title', required=True)
    ip.add_argument('--file', required=True)
    ip.add_argument('--project', default='')
    ip.add_argument('--attendees', default='')

    # list
    lp = sub.add_parser('list', help='List stored meetings')
    lp.add_argument('--workspace', default=None)
    lp.add_argument('--limit', type=int, default=20)

    # tasks
    tp = sub.add_parser('tasks', help='Show meeting-extracted tasks')
    tp.add_argument('--workspace', default=None)

    # extract
    ep = sub.add_parser('extract', help='Re-run extraction on a stored meeting')
    ep.add_argument('--workspace', default=None)
    ep.add_argument('--meeting-id', dest='meeting_id', required=True)

    args = p.parse_args()

    handlers = {
        'sync': cmd_sync,
        'ingest': cmd_ingest,
        'list': cmd_list,
        'tasks': cmd_tasks,
        'extract': cmd_extract,
    }

    handler = handlers.get(args.cmd)
    if handler:
        handler(args)
    else:
        p.print_help()


if __name__ == '__main__':
    main()
