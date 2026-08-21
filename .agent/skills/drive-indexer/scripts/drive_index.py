#!/usr/bin/env python3
"""drive_index.py - Recursively index a Google Drive folder tree.

Scans a configured root folder (and all subfolders) in the owner's personal
Google Drive, classifies top-level folders as 'project' or 'general', and
writes a local JSON index so other agents can search without API calls.

New top-level folders are auto-detected on every scan. General folders are
identified by explicit overrides first, then by pattern matching as fallback.

Usage:
  python3 drive_index.py scan --workspace samudera
  python3 drive_index.py scan --workspace samudera --folder CUSTOM_ROOT
  python3 drive_index.py status --workspace samudera
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
if sys.stderr.encoding != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8')

WIB = timezone(timedelta(hours=7))
BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent

SCOPES = ['https://www.googleapis.com/auth/drive']


def _config_path():
    return Path(__file__).resolve().parent.parent / 'config.json'


def _load_config():
    with open(_config_path(), 'r', encoding='utf-8') as fh:
        return json.load(fh)


def _index_path(ws):
    return BASE_DIR / '.agent' / 'workspaces' / ws / 'state' / 'drive_index.json'


def _token_path(cfg):
    p = Path(cfg.get('token_path', ''))
    if p.is_absolute():
        return p
    return BASE_DIR / p


def _authenticate(cfg):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token_path = _token_path(cfg)
    if not token_path.exists():
        raise FileNotFoundError('token not found: %s' % token_path)

    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise RuntimeError('Drive credentials invalid and cannot refresh')
        with open(token_path, 'w', encoding='utf-8') as fh:
            fh.write(creds.to_json())

    return build('drive', 'v3', credentials=creds)


def _is_google_native(mime):
    return mime.startswith('application/vnd.google-apps.')


def _extract_google_content(service, file_id, mime):
    """Export Google Docs/Sheets/Slides as text."""
    export_map = {
        'application/vnd.google-apps.document': 'text/plain',
        'application/vnd.google-apps.spreadsheet': 'text/csv',
        'application/vnd.google-apps.presentation': 'text/plain',
    }
    export_mime = export_map.get(mime)
    if not export_mime:
        return ''
    try:
        req = service.files().export_media(fileId=file_id, mimeType=export_mime)
        import io
        buf = io.BytesIO()
        downloader = __import__('googleapiclient.http', fromlist=['MediaIoBaseDownload']).MediaIoBaseDownload(buf, req)
        done = False
        while not done:
            status, done = downloader.next_chunk()
        return buf.getvalue().decode('utf-8', errors='replace')[:50000]
    except Exception:
        return ''


def _extract_pdf_content(service, file_id):
    """Download PDF from Drive and extract text with pdfplumber."""
    try:
        import pdfplumber
        import io
        req = service.files().get_media(fileId=file_id)
        import googleapiclient.http
        buf = io.BytesIO()
        downloader = googleapiclient.http.MediaIoBaseDownload(buf, req)
        done = False
        while not done:
            status, done = downloader.next_chunk()
        buf.seek(0)
        text_parts = []
        with pdfplumber.open(buf) as pdf:
            for page in pdf.pages[:30]:
                t = page.extract_text()
                if t:
                    text_parts.append(t)
        return '\n'.join(text_parts)[:50000]
    except Exception:
        return ''


def _classify_folder(name, cfg):
    """Classify a top-level folder. Explicit overrides first, then patterns."""
    general_overrides = cfg.get('general_folders', [])
    if name in general_overrides:
        return 'general'
    lower = name.lower()
    for pattern in cfg.get('general_folder_patterns', []):
        if pattern in lower:
            return 'general'
    return 'project'


def _list_folder(service, folder_id, page_size=200):
    """List all files/folders in a folder. Returns items list."""
    items = []
    page_token = None
    while True:
        q = "'%s' in parents and trashed = false" % folder_id
        resp = service.files().list(
            q=q,
            pageSize=page_size,
            fields='nextPageToken, files(id, name, mimeType, size, modifiedTime, webViewLink)',
            pageToken=page_token,
            orderBy='name',
        ).execute()
        items.extend(resp.get('files', []))
        page_token = resp.get('nextPageToken')
        if not page_token:
            break
    return items


def _walk(service, folder_id, folder_path, cfg, depth=0, max_depth=10,
          extract_content=False):
    """Recursively walk a folder. Yields (file_info, folder_path, project, project_type)."""
    if depth > max_depth:
        return

    items = _list_folder(service, folder_id, cfg.get('max_results_per_folder', 200))

    for item in items:
        mime = item.get('mimeType', '')
        name = item.get('name', '')

        if mime == 'application/vnd.google-apps.folder':
            sub_path = folder_path + name + '/'
            for result in _walk(service, item['id'], sub_path, cfg,
                                depth=depth + 1, max_depth=max_depth,
                                extract_content=extract_content):
                yield result
        else:
            file_info = {
                'id': item['id'],
                'name': name,
                'mimeType': mime,
                'folder_path': folder_path,
                'modifiedTime': item.get('modifiedTime', ''),
                'size': int(item.get('size', 0) or 0),
                'webViewLink': item.get('webViewLink', ''),
            }
            if extract_content:
                content = ''
                if _is_google_native(mime):
                    content = _extract_google_content(service, item['id'], mime)
                elif mime == 'application/pdf':
                    content = _extract_pdf_content(service, item['id'])
                if content:
                    file_info['content'] = content
                    print('    [content] %s (%d chars)' % (name, len(content)))
            yield file_info


def cmd_scan(args):
    cfg = _load_config()
    ws = args.workspace or 'samudera'
    root_id = args.folder or cfg.get('root_folder_id')
    root_name = cfg.get('root_folder_name', 'Samudera Indonesia')
    extract_content = getattr(args, 'content', False)

    if not root_id:
        print('Error: no root_folder_id configured and no --folder provided')
        sys.exit(1)

    service = _authenticate(cfg)
    extensions = set(cfg.get('index_file_extensions', []))
    google_mimes = set(cfg.get('google_native_mimes', []))

    print('Scanning Drive folder: %s (id: %s)' % (root_name, root_id))
    if extract_content:
        print('  Content extraction: ENABLED (Google Docs + PDFs)')

    projects = {}
    files = []
    seen_projects = set()

    root_items = _list_folder(service, root_id, cfg.get('max_results_per_folder', 200))

    for item in root_items:
        mime = item.get('mimeType', '')
        name = item.get('name', '')

        if mime != 'application/vnd.google-apps.folder':
            if _should_index(name, mime, extensions, google_mimes):
                files.append({
                    'id': item['id'],
                    'name': name,
                    'mimeType': mime,
                    'folder_path': '',
                    'project': name,
                    'project_type': 'general',
                    'modifiedTime': item.get('modifiedTime', ''),
                    'size': int(item.get('size', 0) or 0),
                    'webViewLink': item.get('webViewLink', ''),
                })
            continue

        ptype = _classify_folder(name, cfg)
        projects[name] = {
            'folder_id': item['id'],
            'project_type': ptype,
            'folder_path': name + '/',
            'file_count': 0,
            'last_modified': '',
        }
        seen_projects.add(name)

        print('  %s [%s]' % (name, ptype))
        for file_info in _walk(service, item['id'], name + '/', cfg,
                               max_depth=cfg.get('max_depth', 10),
                               extract_content=extract_content):
            if _should_index(file_info['name'], file_info['mimeType'],
                             extensions, google_mimes):
                file_info['project'] = name
                file_info['project_type'] = ptype
                files.append(file_info)

    for pname in projects:
        pfiles = [f for f in files if f.get('project') == pname]
        projects[pname]['file_count'] = len(pfiles)
        if pfiles:
            projects[pname]['last_modified'] = max(
                f.get('modifiedTime', '') for f in pfiles)

    stats = {
        'total_files': len(files),
        'total_projects': len(projects),
        'by_project': {p: projects[p]['file_count'] for p in projects},
    }

    now = datetime.now(WIB).isoformat(timespec='seconds')
    index = {
        'version': 1,
        'indexed_wib': now,
        'root_folder_id': root_id,
        'root_folder_name': root_name,
        'projects': projects,
        'files': files,
        'stats': stats,
    }

    out = _index_path(ws)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(out) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(index, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, str(out))

    print('Index written: %s' % out)
    print('  %d files across %d projects' % (stats['total_files'],
                                               stats['total_projects']))
    for pname, count in stats['by_project'].items():
        print('    %s: %d files' % (pname, count))


def _should_index(name, mime, extensions, google_mimes):
    if mime in google_mimes:
        return True
    ext = os.path.splitext(name)[1].lower()
    return ext in extensions


def cmd_status(args):
    ws = args.workspace or 'samudera'
    path = _index_path(ws)
    if not path.exists():
        print('No index found for workspace "%s"' % ws)
        print('Run: drive_index.py scan --workspace %s' % ws)
        return

    with open(path, 'r', encoding='utf-8') as fh:
        index = json.load(fh)

    print('Drive Index - workspace: %s' % ws)
    print('Root: %s (id: %s)' % (index.get('root_folder_name', '?'),
                                   index.get('root_folder_id', '?')))
    print('Indexed: %s' % index.get('indexed_wib', '?'))
    stats = index.get('stats', {})
    print('Total: %d files across %d projects' % (stats.get('total_files', 0),
                                                    stats.get('total_projects', 0)))
    for pname, count in stats.get('by_project', {}).items():
        pinfo = index.get('projects', {}).get(pname, {})
        ptype = pinfo.get('project_type', 'project')
        print('  %-30s %d files  [%s]' % (pname, count, ptype))


def main():
    p = argparse.ArgumentParser(description='Drive Folder Indexer')
    sub = p.add_subparsers(dest='cmd')
    sc = sub.add_parser('scan')
    sc.add_argument('--workspace', default='samudera')
    sc.add_argument('--folder', default=None)
    sc.add_argument('--content', action='store_true',
                    help='Extract text content from Google Docs and PDFs')
    st = sub.add_parser('status')
    st.add_argument('--workspace', default='samudera')
    args = p.parse_args()
    if args.cmd == 'scan':
        cmd_scan(args)
    elif args.cmd == 'status':
        cmd_status(args)
    else:
        p.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
