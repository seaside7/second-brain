#!/usr/bin/env python3
"""
timesheet_writer.py — Append time entries to a workspace's timesheet Google Sheet.

Sheet ID, valid projects, and column config are loaded from workspace timesheet.json.
Auth: reuses the workspace's token_drive.json (Drive scope includes Sheets).
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, '..', '..', '..', '..'))
DEV_SESSIONS_PATH = os.path.join(REPO_ROOT, 'journal', 'state', 'dev_sessions.json')

sys.path.insert(0, os.path.join(REPO_ROOT, '.agent', 'workspaces'))
import workspace_resolver as ws

WIB = timezone(timedelta(hours=7))

# UTF-8 output on Windows
try:
    if sys.stdout.encoding != 'utf-8':
        sys.stdout.reconfigure(encoding='utf-8')
    if sys.stderr.encoding != 'utf-8':
        sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

# ---------- workspace-aware config ----------

_current_workspace = None


def get_current_workspace():
    """Return the resolved workspace name for this invocation."""
    return _current_workspace


def get_workspace_config():
    """Return the full WorkspaceContext for the current invocation."""
    return ws.get(_current_workspace)


def resolve_workspace(workspace_name=None):
    """Resolve workspace and load timesheet config."""
    global _current_workspace
    ctx = ws.get(workspace_name)
    _current_workspace = ctx.name

    cfg = ctx.config('timesheet')
    if not cfg:
        print(f"Error: No timesheet.json found for workspace '{_current_workspace}'.", file=sys.stderr)
        print(f"Create: .agent/workspaces/{_current_workspace}/timesheet.json", file=sys.stderr)
        sys.exit(1)

    return ctx, cfg

# ---------- auth ----------

def get_sheets_service(ctx):
    """Build a Google Sheets API service using the workspace's Drive token."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    scopes = ['https://www.googleapis.com/auth/drive']
    token_file = ctx.token('drive')

    # Fallback to old location if workspace token doesn't exist yet
    if not os.path.exists(token_file):
        old_token = os.path.join(REPO_ROOT, '.agent', 'skills', 'work-drive-connector', 'token.json')
        if os.path.exists(old_token):
            token_file = old_token
        else:
            print(f"Error: No Drive token found for workspace '{_current_workspace}'.", file=sys.stderr)
            print(f"Expected: {ctx.token('drive')}", file=sys.stderr)
            sys.exit(1)

    creds = Credentials.from_authorized_user_file(token_file, scopes)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(token_file, 'w') as f:
                f.write(creds.to_json())
        else:
            print("Error: Token expired and cannot refresh.", file=sys.stderr)
            sys.exit(1)

    return build('sheets', 'v4', credentials=creds)

# ---------- helpers ----------

def current_month_name():
    now = datetime.now(WIB)
    return now.strftime('%B')


def find_next_empty_row(service, spreadsheet_id, sheet_name, project_col='C'):
    range_str = f"'{sheet_name}'!{project_col}:{project_col}"
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=range_str,
        majorDimension='COLUMNS',
    ).execute()

    values = result.get('values', [[]])
    col = values[0] if values else []
    next_row = len(col) + 1
    return next_row


def validate_project(project, valid_projects):
    for p in valid_projects:
        if p.lower() == project.lower():
            return p
    for p in valid_projects:
        if project.lower() in p.lower():
            return p
    return None

# ---------- commands ----------

def cmd_append(args):
    ctx, cfg = resolve_workspace(args.workspace)
    spreadsheet_id = cfg.get('spreadsheet_id')
    valid_projects = cfg.get('valid_projects', [])
    col_project = cfg.get('columns', {}).get('project', 'C')
    col_desc = cfg.get('columns', {}).get('description', 'D')

    if not spreadsheet_id:
        print(f"Error: No spreadsheet_id in timesheet.json for '{_current_workspace}'.", file=sys.stderr)
        sys.exit(1)

    project = validate_project(args.project, valid_projects)
    if not project:
        print(f"Error: '{args.project}' is not a valid project for workspace '{_current_workspace}'.", file=sys.stderr)
        print("Valid projects:", file=sys.stderr)
        for p in valid_projects:
            print(f"  - {p}", file=sys.stderr)
        sys.exit(1)

    description = args.description.strip()
    if not description:
        print("Error: description cannot be empty.", file=sys.stderr)
        sys.exit(1)

    sheet_name = args.month or current_month_name()

    if args.dry_run:
        print(f"[DRY RUN] [{_current_workspace}] Would append to sheet '{sheet_name}':")
        print(f"  Column {col_project} (Projects):    {project}")
        print(f"  Column {col_desc} (Description): {description}")
        return

    service = get_sheets_service(ctx)
    next_row = find_next_empty_row(service, spreadsheet_id, sheet_name, col_project)

    range_str = f"'{sheet_name}'!{col_project}{next_row}:{col_desc}{next_row}"
    body = {'values': [[project, description]]}

    result = service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=range_str,
        valueInputOption='USER_ENTERED',
        body=body,
    ).execute()

    updated = result.get('updatedCells', 0)
    print(f"[{_current_workspace}] Appended to '{sheet_name}' row {next_row}:")
    print(f"  Project:     {project}")
    print(f"  Description: {description}")
    print(f"  Cells updated: {updated}")


def cmd_from_session(args):
    ctx, cfg = resolve_workspace(args.workspace)
    spreadsheet_id = cfg.get('spreadsheet_id')
    valid_projects = cfg.get('valid_projects', [])
    col_project = cfg.get('columns', {}).get('project', 'C')
    col_desc = cfg.get('columns', {}).get('description', 'D')

    if not spreadsheet_id:
        print(f"Error: No spreadsheet_id in timesheet.json.", file=sys.stderr)
        sys.exit(1)

    try:
        with open(DEV_SESSIONS_PATH, encoding='utf-8') as f:
            state = json.load(f)
    except Exception as e:
        print(f"Error reading dev sessions: {e}", file=sys.stderr)
        sys.exit(1)

    session = state.get('sessions', {}).get(args.session_id)
    if not session:
        print(f"Error: session {args.session_id} not found.", file=sys.stderr)
        sys.exit(1)

    if session.get('status') != 'completed':
        print(f"Error: session {args.session_id} is not completed.", file=sys.stderr)
        sys.exit(1)

    project_raw = session.get('project', '')
    project = validate_project(project_raw, valid_projects)
    if not project:
        print(f"Warning: project '{project_raw}' not in valid list.", file=sys.stderr)
        project = project_raw

    description = session.get('summary') or session.get('title', '')
    sheet_name = args.month or current_month_name()

    if args.dry_run:
        print(f"[DRY RUN] [{_current_workspace}] Would append to sheet '{sheet_name}':")
        print(f"  Column {col_project} (Projects):    {project}")
        print(f"  Column {col_desc} (Description): {description}")
        return

    service = get_sheets_service(ctx)
    next_row = find_next_empty_row(service, spreadsheet_id, sheet_name, col_project)

    range_str = f"'{sheet_name}'!{col_project}{next_row}:{col_desc}{next_row}"
    body = {'values': [[project, description]]}

    result = service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=range_str,
        valueInputOption='USER_ENTERED',
        body=body,
    ).execute()

    updated = result.get('updatedCells', 0)
    print(f"[{_current_workspace}] Appended from {args.session_id} to '{sheet_name}' row {next_row}:")
    print(f"  Project:     {project}")
    print(f"  Description: {description}")
    print(f"  Cells updated: {updated}")


def cmd_projects(args):
    ctx, cfg = resolve_workspace(args.workspace)
    valid_projects = cfg.get('valid_projects', [])
    print(f"[{_current_workspace}] Valid project names for the timesheet:")
    for p in valid_projects:
        print(f"  - {p}")

# ---------- CLI ----------

def main():
    p = argparse.ArgumentParser(description='Workspace-aware Timesheet Writer')
    sub = p.add_subparsers(dest='cmd')

    # append
    ap = sub.add_parser('append', help='Append a timesheet entry')
    ap.add_argument('--workspace', default=None, help='Workspace (default: active)')
    ap.add_argument('--project', required=True, help='Project name')
    ap.add_argument('--description', required=True, help='Work description')
    ap.add_argument('--month', help='Override month tab name')
    ap.add_argument('--dry-run', dest='dry_run', action='store_true')

    # from-session
    fp = sub.add_parser('from-session', help='Append from a dev session')
    fp.add_argument('session_id', help='Dev session ID (e.g. DEV-0001)')
    fp.add_argument('--workspace', default=None, help='Workspace (default: active)')
    fp.add_argument('--month', help='Override month tab name')
    fp.add_argument('--dry-run', dest='dry_run', action='store_true')

    # projects
    pp = sub.add_parser('projects', help='List valid project names')
    pp.add_argument('--workspace', default=None, help='Workspace (default: active)')

    args = p.parse_args()

    handlers = {
        'append': cmd_append,
        'from-session': cmd_from_session,
        'projects': cmd_projects,
    }

    handler = handlers.get(args.cmd)
    if handler:
        handler(args)
    else:
        p.print_help()


if __name__ == '__main__':
    main()
