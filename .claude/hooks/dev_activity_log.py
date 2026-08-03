#!/usr/bin/env python3
"""PostToolUse hook: auto-log file edits and bash commands into the active dev-tracker session.

Fires after Write/Edit/Bash tool use. If a dev session is active, appends an event.
If no session is active, does nothing (silent pass-through).

Contract: always exit 0, never block the session.
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

WIB = timezone(timedelta(hours=7))
REPO_ROOT = os.environ.get('CLAUDE_PROJECT_DIR', '.')
STATE_PATH = os.path.join(REPO_ROOT, 'journal', 'state', 'dev_sessions.json')

# Paths that are noise — never log edits to these
IGNORE_PATTERNS = (
    'journal/state/dev_sessions.json',
    'journal/state/',
    'dashboard-data/',
    '.agent/harness.json',
    'Dashboard.md',
    '.tmp',
    '__pycache__',
)

# Commands that are noise — never log these
IGNORE_COMMANDS = (
    'dev_tracker.py',
    'heartbeat.py',
    'cat ',
    'type ',
    'echo ',
    'ls ',
    'dir ',
    'pwd',
    'cd ',
)


def load_state():
    try:
        with open(STATE_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def save_state(state):
    tmp = STATE_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def get_active_session(state):
    if not state:
        return None
    for sid, session in state.get('sessions', {}).items():
        if session.get('status') == 'active':
            return session
    return None


def should_ignore_file(path):
    for pat in IGNORE_PATTERNS:
        if pat in path:
            return True
    return False


def should_ignore_command(cmd):
    cmd_lower = cmd.lower().strip()
    for pat in IGNORE_COMMANDS:
        if pat in cmd_lower:
            return True
    return False


def now_wib():
    return datetime.now(WIB).isoformat(timespec='seconds')


def main():
    # Read the hook input from stdin
    try:
        raw = sys.stdin.read()
    except Exception:
        sys.exit(0)

    if not raw:
        sys.exit(0)

    try:
        payload = json.loads(raw)
    except Exception:
        sys.exit(0)

    # Check if a dev session is active
    state = load_state()
    session = get_active_session(state)
    if not session:
        sys.exit(0)

    # Extract tool info from the hook payload
    tool_name = payload.get('tool_name', '') or ''
    tool_input = payload.get('tool_input', {}) or {}
    tool_output = payload.get('tool_output', '') or ''

    event = None

    # File write/edit
    if 'write' in tool_name.lower() or 'edit' in tool_name.lower():
        file_path = tool_input.get('file_path') or tool_input.get('path') or ''
        if file_path and not should_ignore_file(file_path):
            # Make path relative to repo root if possible
            try:
                rel = os.path.relpath(file_path, REPO_ROOT).replace('\\', '/')
            except Exception:
                rel = file_path
            event = {'ts': now_wib(), 'type': 'file', 'note': rel}

    # Bash command
    elif 'bash' in tool_name.lower():
        command = tool_input.get('command') or tool_input.get('cmd') or ''
        if command and not should_ignore_command(command):
            # Truncate very long commands
            if len(command) > 150:
                command = command[:147] + '...'
            event = {'ts': now_wib(), 'type': 'command', 'note': command}

    if event:
        # Dedupe: don't log the same note twice in a row
        events = session.get('events', [])
        if events and events[-1].get('note') == event['note'] and events[-1].get('type') == event['type']:
            sys.exit(0)

        session['events'].append(event)
        save_state(state)

    sys.exit(0)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        sys.exit(0)
