#!/usr/bin/env python3
"""drive_search.py - Search the local Drive index and read files.

Searches the local JSON index built by drive_index.py. Returns results
without calling the Drive API. For reading a matched file, exports content
via the personal Drive API on demand.

Usage:
  python3 drive_search.py search --workspace samudera --query "pricing"
  python3 drive_search.py search --workspace samudera --query "CRM" --project "CRM - Salesforce"
  python3 drive_search.py read --workspace samudera --id FILE_ID
  python3 drive_search.py projects --workspace samudera
"""
import argparse
import json
import os
import sys
from pathlib import Path

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
if sys.stderr.encoding != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8')

BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent


def _index_path(ws):
    return BASE_DIR / '.agent' / 'workspaces' / ws / 'state' / 'drive_index.json'


def _load_index(ws):
    path = _index_path(ws)
    if not path.exists():
        return None
    with open(path, 'r', encoding='utf-8') as fh:
        return json.load(fh)


def _search(index, query, project=None, limit=20):
    """Search files by name + folder_path + content. Optional project filter."""
    terms = [t.lower() for t in query.split() if len(t) > 1]
    results = []
    for f in index.get('files', []):
        if project and f.get('project') != project:
            continue
        blob = (f.get('name', '') + ' ' + f.get('folder_path', '') +
                ' ' + f.get('project', '') + ' ' + f.get('content', '')).lower()
        score = sum(1 for t in terms if t in blob)
        if terms and score == 0:
            continue
        results.append({**f, '_score': score})
    results.sort(key=lambda x: (-x['_score'], x.get('name', '')))
    return results[:limit]


def cmd_search(args):
    index = _load_index(args.workspace)
    if not index:
        print('No index found. Run: drive_index.py scan --workspace %s' % args.workspace)
        sys.exit(1)

    query = (args.query or '').strip()
    if not query:
        print('search --query "<search term>"')
        sys.exit(1)

    results = _search(index, query, project=args.project, limit=args.limit)
    if not results:
        print('No files matching "%s"' % query)
        if args.project:
            print('  (filtered to project: %s)' % args.project)
        return

    print('Drive search: "%s" (%d results)' % (query, len(results)))
    if args.project:
        print('  Project filter: %s' % args.project)
    print()
    for r in results:
        path = r.get('folder_path', '') or '/'
        print('  %s' % r.get('name', '?'))
        print('    path: %s' % path)
        print('    project: %s [%s]' % (r.get('project', '?'),
                                         r.get('project_type', '?')))
        print('    id: %s' % r.get('id', '?'))
        print('    modified: %s' % r.get('modifiedTime', '?'))
        print()


def cmd_read(args):
    index = _load_index(args.workspace)
    if not index:
        print('No index found. Run: drive_index.py scan --workspace %s' % args.workspace)
        sys.exit(1)

    file_id = (args.id or '').strip()
    if not file_id:
        print('read --id FILE_ID')
        sys.exit(1)

    file_info = None
    for f in index.get('files', []):
        if f['id'] == file_id:
            file_info = f
            break

    if not file_info:
        print('File id %s not found in index' % file_id)
        sys.exit(1)

    mime = file_info.get('mimeType', '')
    name = file_info.get('name', '')
    print('Reading: %s (id: %s, type: %s)' % (name, file_id, mime))

    try:
        content = _export_file(file_id, mime)
        print(content)
    except Exception as e:
        print('Export failed: %s' % e)
        print('Link: %s' % file_info.get('webViewLink', 'N/A'))


def _export_file(file_id, mime):
    """Export a file from Drive. For Google-native types, export as text."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    cfg_path = Path(__file__).resolve().parent / 'config.json'
    with open(cfg_path, 'r', encoding='utf-8') as fh:
        cfg = json.load(fh)

    token_path = Path(cfg.get('token_path', ''))
    if not token_path.is_absolute():
        token_path = BASE_DIR / token_path

    SCOPES = ['https://www.googleapis.com/auth/drive']
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise RuntimeError('Drive credentials invalid')

    service = build('drive', 'v3', credentials=creds)

    if mime.startswith('application/vnd.google-apps.document'):
        resp = service.files().export_media(
            fileId=file_id, mimeType='text/plain').execute()
        return resp.decode('utf-8')
    elif mime.startswith('application/vnd.google-apps.spreadsheet'):
        resp = service.files().export_media(
            fileId=file_id, mimeType='text/csv').execute()
        return resp.decode('utf-8')
    elif mime.startswith('application/vnd.google-apps.presentation'):
        resp = service.files().export_media(
            fileId=file_id, mimeType='text/plain').execute()
        return resp.decode('utf-8')
    else:
        resp = service.files().get_media(fileId=file_id).execute()
        return resp.decode('utf-8', errors='replace')


def cmd_projects(args):
    index = _load_index(args.workspace)
    if not index:
        print('No index found. Run: drive_index.py scan --workspace %s' % args.workspace)
        sys.exit(1)

    projects = index.get('projects', {})
    print('Projects in Drive index:')
    for name, info in sorted(projects.items()):
        print('  %-30s %d files  [%s]' % (name, info.get('file_count', 0),
                                            info.get('project_type', '?')))


def main():
    p = argparse.ArgumentParser(description='Drive Index Search')
    sub = p.add_subparsers(dest='cmd')
    se = sub.add_parser('search')
    se.add_argument('--workspace', default='samudera')
    se.add_argument('--query', required=True)
    se.add_argument('--project', default=None)
    se.add_argument('--limit', type=int, default=20)
    re = sub.add_parser('read')
    re.add_argument('--workspace', default='samudera')
    re.add_argument('--id', required=True)
    pr = sub.add_parser('projects')
    pr.add_argument('--workspace', default='samudera')
    args = p.parse_args()
    if args.cmd == 'search':
        cmd_search(args)
    elif args.cmd == 'read':
        cmd_read(args)
    elif args.cmd == 'projects':
        cmd_projects(args)
    else:
        p.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
