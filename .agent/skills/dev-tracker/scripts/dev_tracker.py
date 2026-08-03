#!/usr/bin/env python3
"""
dev_tracker.py — Development task session tracker.

Manages the lifecycle of a development task: start → log events → complete → report.
State: journal/state/dev_sessions.json
Reports: journal/dev_reports/DEV-NNNN_<slug>.md
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, '..', '..', '..', '..'))
STATE_PATH = os.path.join(REPO_ROOT, 'journal', 'state', 'dev_sessions.json')
REPORTS_DIR = os.path.join(REPO_ROOT, 'journal', 'dev_reports')
WIB = timezone(timedelta(hours=7))

# Workspace resolver
sys.path.insert(0, os.path.join(REPO_ROOT, '.agent', 'workspaces'))
import workspace_resolver as ws

_current_workspace = None


def get_current_workspace():
    """Return the resolved workspace name for this invocation."""
    return _current_workspace


def get_workspace_config():
    """Return the full WorkspaceContext for the current invocation."""
    return ws.get(_current_workspace)

# UTF-8 output on Windows
try:
    if sys.stdout.encoding != 'utf-8':
        sys.stdout.reconfigure(encoding='utf-8')
    if sys.stderr.encoding != 'utf-8':
        sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

# ---------- state management ----------

def load_state():
    try:
        with open(STATE_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {'next_seq': 1, 'sessions': {}}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def now_wib():
    return datetime.now(WIB).isoformat(timespec='seconds')


def slugify(text):
    s = re.sub(r'[^a-z0-9]+', '-', text.lower().strip())
    return s.strip('-')[:50]


def next_id(state):
    seq = state.get('next_seq', 1)
    dev_id = f"DEV-{seq:04d}"
    state['next_seq'] = seq + 1
    return dev_id


def get_active_session(state):
    """Return the currently active (or paused) session, or None."""
    for sid, session in state.get('sessions', {}).items():
        if session.get('status') in ('active', 'paused'):
            return session
    return None

# ---------- commands ----------

def cmd_start(args):
    state = load_state()
    active = get_active_session(state)
    if active:
        print(f"Error: session {active['id']} is already active ({active['title']}).")
        print("Complete or pause it first.")
        sys.exit(1)

    # Resolve workspace
    global _current_workspace
    ctx = ws.get(args.workspace)
    _current_workspace = ctx.name

    dev_id = next_id(state)
    session = {
        'id': dev_id,
        'title': args.title,
        'workspace': ctx.name,
        'source': args.source or 'manual',
        'source_ref': args.source_ref or '',
        'project': args.project or '',
        'repo': args.repo or '',
        'requester': args.requester or '',
        'priority': args.priority or 'medium',
        'status': 'active',
        'branch': '',
        'started_at': now_wib(),
        'paused_at': None,
        'completed_at': None,
        'total_paused_seconds': 0,
        'working_duration_minutes': None,
        'summary': None,
        'events': [],
        'report_path': None,
    }
    state['sessions'][dev_id] = session
    save_state(state)
    print(f"[{ctx.name}] Started task session: {dev_id}")
    print(f"  Title:     {session['title']}")
    print(f"  Workspace: {ctx.display_name}")
    print(f"  Source:    {session['source']}")
    print(f"  Project:   {session['project']}")
    print(f"  Repo:      {session['repo']}")
    print(f"  Requester: {session['requester']}")
    print(f"  Priority:  {session['priority']}")
    print(f"  Started:   {session['started_at']}")


def cmd_log(args):
    state = load_state()
    active = get_active_session(state)
    if not active:
        print("Error: no active task session. Run 'start' first.")
        sys.exit(1)

    event = {
        'ts': now_wib(),
        'type': args.type,
        'note': args.note or '',
    }
    active['events'].append(event)

    # Auto-set branch if logging a branch event
    if args.type == 'branch' and args.note:
        active['branch'] = args.note.strip()

    save_state(state)
    print(f"[{active['id']}] Logged {args.type}: {args.note or '(no note)'}")


def cmd_status(args):
    state = load_state()
    active = get_active_session(state)
    if not active:
        print("No active task session.")
        return

    print(f"Active session: {active['id']}")
    print(f"  Title:    {active['title']}")
    print(f"  Status:   {active['status']}")
    print(f"  Project:  {active['project']}")
    print(f"  Repo:     {active['repo']}")
    print(f"  Branch:   {active['branch'] or '(not set)'}")
    print(f"  Priority: {active['priority']}")
    print(f"  Started:  {active['started_at']}")
    print(f"  Events:   {len(active['events'])}")

    if active['events']:
        print("\n  Recent activity:")
        for ev in active['events'][-10:]:
            print(f"    [{ev['ts'][-8:]}] {ev['type']:10s} {ev['note'][:80]}")


def cmd_pause(args):
    state = load_state()
    active = get_active_session(state)
    if not active:
        print("Error: no active task session.")
        sys.exit(1)
    if active['status'] == 'paused':
        print(f"Already paused since {active['paused_at']}.")
        return

    active['status'] = 'paused'
    active['paused_at'] = now_wib()
    save_state(state)
    print(f"[{active['id']}] Paused.")


def cmd_resume(args):
    state = load_state()
    active = get_active_session(state)
    if not active:
        print("Error: no task session to resume.")
        sys.exit(1)
    if active['status'] != 'paused':
        print(f"Session is not paused (status: {active['status']}).")
        return

    # Calculate paused duration
    if active.get('paused_at'):
        try:
            paused_start = datetime.fromisoformat(active['paused_at'])
            now = datetime.now(WIB)
            paused_secs = (now - paused_start).total_seconds()
            active['total_paused_seconds'] = active.get('total_paused_seconds', 0) + int(paused_secs)
        except Exception:
            pass

    active['status'] = 'active'
    active['paused_at'] = None
    save_state(state)
    print(f"[{active['id']}] Resumed.")


def cmd_complete(args):
    state = load_state()
    active = get_active_session(state)
    if not active:
        print("Error: no active task session to complete.")
        sys.exit(1)

    # If paused, resume first to account for final pause duration
    if active['status'] == 'paused' and active.get('paused_at'):
        try:
            paused_start = datetime.fromisoformat(active['paused_at'])
            now = datetime.now(WIB)
            paused_secs = (now - paused_start).total_seconds()
            active['total_paused_seconds'] = active.get('total_paused_seconds', 0) + int(paused_secs)
        except Exception:
            pass

    active['status'] = 'completed'
    active['completed_at'] = now_wib()
    active['summary'] = args.summary or ''

    # Calculate working duration
    try:
        start = datetime.fromisoformat(active['started_at'])
        end = datetime.fromisoformat(active['completed_at'])
        total_secs = (end - start).total_seconds()
        working_secs = total_secs - active.get('total_paused_seconds', 0)
        active['working_duration_minutes'] = round(max(0, working_secs) / 60, 1)
    except Exception:
        active['working_duration_minutes'] = None

    # Generate report
    report_path = generate_report(active)
    active['report_path'] = report_path

    save_state(state)
    duration = active.get('working_duration_minutes')
    dur_str = f"{duration:.0f} min" if duration else "unknown"
    print(f"[{active['id']}] Completed.")
    print(f"  Duration:  {dur_str}")
    print(f"  Events:    {len(active['events'])}")
    print(f"  Report:    {report_path}")


def cmd_list(args):
    state = load_state()
    sessions = list(state.get('sessions', {}).values())

    # Filters
    if args.status:
        sessions = [s for s in sessions if s['status'] == args.status]
    if args.project:
        sessions = [s for s in sessions if args.project.lower() in s.get('project', '').lower()]
    if args.since:
        sessions = [s for s in sessions if s.get('started_at', '') >= args.since]

    sessions.sort(key=lambda s: s.get('started_at', ''), reverse=True)

    if not sessions:
        print("No matching sessions.")
        return

    print(f"{'ID':<10} {'Status':<10} {'Priority':<8} {'Project':<15} {'Title':<40} {'Duration'}")
    print("-" * 100)
    for s in sessions[:30]:
        dur = s.get('working_duration_minutes')
        dur_str = f"{dur:.0f}m" if dur else '-'
        title = s['title'][:38] + '..' if len(s['title']) > 40 else s['title']
        print(f"{s['id']:<10} {s['status']:<10} {s.get('priority','?'):<8} "
              f"{s.get('project',''):<15} {title:<40} {dur_str}")


def cmd_report(args):
    state = load_state()
    session = state.get('sessions', {}).get(args.session_id)
    if not session:
        print(f"Error: session {args.session_id} not found.")
        sys.exit(1)

    report_path = generate_report(session)
    session['report_path'] = report_path
    save_state(state)
    print(f"Report generated: {report_path}")
    # Print the report content
    with open(os.path.join(REPO_ROOT, report_path), encoding='utf-8') as f:
        print(f.read())

# ---------- report generation ----------

def generate_report(session):
    """Generate a development report markdown file from session data."""
    os.makedirs(REPORTS_DIR, exist_ok=True)
    slug = slugify(session['title'])
    filename = f"{session['id']}_{slug}.md"
    filepath = os.path.join(REPORTS_DIR, filename)
    rel_path = os.path.relpath(filepath, REPO_ROOT).replace('\\', '/')

    events = session.get('events', [])
    analyses = [e for e in events if e['type'] == 'analysis']
    files = [e for e in events if e['type'] == 'file']
    commands = [e for e in events if e['type'] == 'command']
    tests = [e for e in events if e['type'] == 'test']
    bugs = [e for e in events if e['type'] == 'bug']
    fixes = [e for e in events if e['type'] == 'fix']
    commits = [e for e in events if e['type'] == 'commit']
    notes = [e for e in events if e['type'] == 'note']

    dur = session.get('working_duration_minutes')
    dur_str = f"{dur:.0f} minutes" if dur else "N/A"
    if dur and dur >= 60:
        dur_str = f"{dur/60:.1f} hours ({dur:.0f} min)"

    lines = []
    lines.append(f"# Development Report: {session['id']}")
    lines.append("")
    lines.append(f"**{session['title']}**")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("|---|---|")
    lines.append(f"| Task ID | {session['id']} |")
    lines.append(f"| Source | {session.get('source', 'manual')} |")
    if session.get('source_ref'):
        lines.append(f"| Source Ref | {session['source_ref']} |")
    lines.append(f"| Project | {session.get('project', '-')} |")
    lines.append(f"| Repository | {session.get('repo', '-')} |")
    lines.append(f"| Requester | {session.get('requester', '-')} |")
    lines.append(f"| Priority | {session.get('priority', '-')} |")
    lines.append(f"| Branch | {session.get('branch', '-')} |")
    lines.append(f"| Started | {session.get('started_at', '-')} |")
    lines.append(f"| Completed | {session.get('completed_at', '-')} |")
    lines.append(f"| Working Duration | {dur_str} |")
    lines.append("")

    # Summary
    if session.get('summary'):
        lines.append("## Summary")
        lines.append("")
        lines.append(session['summary'])
        lines.append("")

    # Analysis / Root Cause
    if analyses:
        lines.append("## Technical Analysis")
        lines.append("")
        for e in analyses:
            lines.append(f"- {e['note']}")
        lines.append("")

    # Bugs Found
    if bugs:
        lines.append("## Bugs Found")
        lines.append("")
        for e in bugs:
            lines.append(f"- {e['note']}")
        lines.append("")

    # Fixes Applied
    if fixes:
        lines.append("## Fixes Applied")
        lines.append("")
        for e in fixes:
            lines.append(f"- {e['note']}")
        lines.append("")

    # Files Changed
    if files:
        lines.append("## Files Changed")
        lines.append("")
        for e in files:
            lines.append(f"- {e['note']}")
        lines.append("")

    # Testing
    if tests:
        lines.append("## Testing")
        lines.append("")
        for e in tests:
            lines.append(f"- {e['note']}")
        lines.append("")

    # Git Activity
    if commits:
        lines.append("## Git Commits")
        lines.append("")
        for e in commits:
            lines.append(f"- {e['note']}")
        lines.append("")

    # Commands Executed
    if commands:
        lines.append("## Commands Executed")
        lines.append("")
        for e in commands[:20]:  # cap at 20 for readability
            lines.append(f"- `{e['note']}`")
        if len(commands) > 20:
            lines.append(f"- ... and {len(commands) - 20} more")
        lines.append("")

    # Notes
    if notes:
        lines.append("## Notes")
        lines.append("")
        for e in notes:
            lines.append(f"- {e['note']}")
        lines.append("")

    # Full timeline
    lines.append("## Activity Timeline")
    lines.append("")
    lines.append(f"Total events: {len(events)}")
    lines.append("")
    lines.append("| Time | Type | Detail |")
    lines.append("|---|---|---|")
    for e in events:
        ts_short = e.get('ts', '')[-8:] if e.get('ts') else ''
        note_short = e['note'][:80] + '...' if len(e.get('note', '')) > 80 else e.get('note', '')
        lines.append(f"| {ts_short} | {e['type']} | {note_short} |")
    lines.append("")

    content = '\n'.join(lines)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)

    return rel_path

# ---------- CLI ----------

def main():
    p = argparse.ArgumentParser(description='Development Task Session Tracker')
    sub = p.add_subparsers(dest='cmd')

    # start
    sp = sub.add_parser('start', help='Start a new task session')
    sp.add_argument('--title', required=True, help='Task title/description')
    sp.add_argument('--workspace', default=None,
                    help='Workspace name (default: active workspace)')
    sp.add_argument('--source', choices=['mattermost', 'trello', 'gmail', 'fathom', 'gitlab', 'manual'],
                    default='manual', help='Where the task came from')
    sp.add_argument('--source-ref', dest='source_ref', help='URL or ID of source item')
    sp.add_argument('--project', help='Project name')
    sp.add_argument('--repo', help='Repository path')
    sp.add_argument('--requester', help='Who requested this task')
    sp.add_argument('--priority', choices=['high', 'medium', 'low'], default='medium')

    # log
    lp = sub.add_parser('log', help='Log a development event')
    lp.add_argument('--type', required=True,
                    choices=['analysis', 'file', 'command', 'test', 'bug', 'fix', 'commit', 'branch', 'note'])
    lp.add_argument('--note', required=True, help='Event description')

    # status
    sub.add_parser('status', help='Show current active session')

    # pause / resume
    sub.add_parser('pause', help='Pause the active session')
    sub.add_parser('resume', help='Resume a paused session')

    # complete
    cp = sub.add_parser('complete', help='Complete the active session')
    cp.add_argument('--summary', help='Brief summary of what was done')

    # list
    lsp = sub.add_parser('list', help='List task sessions')
    lsp.add_argument('--status', choices=['active', 'paused', 'completed'])
    lsp.add_argument('--project', help='Filter by project name')
    lsp.add_argument('--since', help='Filter by start date (YYYY-MM-DD)')

    # report
    rp = sub.add_parser('report', help='View/regenerate a report')
    rp.add_argument('session_id', help='Session ID (e.g. DEV-0001)')

    args = p.parse_args()

    handlers = {
        'start': cmd_start,
        'log': cmd_log,
        'status': cmd_status,
        'pause': cmd_pause,
        'resume': cmd_resume,
        'complete': cmd_complete,
        'list': cmd_list,
        'report': cmd_report,
    }

    handler = handlers.get(args.cmd)
    if handler:
        handler(args)
    else:
        p.print_help()


if __name__ == '__main__':
    main()
