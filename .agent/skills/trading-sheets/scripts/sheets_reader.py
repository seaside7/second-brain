"""Read-only Google Sheets connector for the Trading Brain journal.

The VPS trading-brain paper traders WRITE one row per closed trade into this
spreadsheet. This connector is the second-brain's READ side: it fetches tab
metadata, raw values, and formulas, and snapshots them locally so the AI can
learn + analyze the daily record.

Read-only by design: it only issues GET calls against the Sheets API. It never
writes, never enqueues, never modifies the spreadsheet. App credentials live in
the personal workspace (gitignored), so the OAuth scopes follow the personal
Drive token with no code dependency on the trading-brain repo.

Usage:
    python sheets_reader.py --status
    python sheets_reader.py --dump "CURRENT Sep 2026"
    python sheets_reader.py --dump-all
    python sheets_reader.py --dump "CURRENT Sep 2026" --rows 2000
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
REPO_ROOT = SKILL_DIR.parent.parent.parent

SPREADSHEET_ID = os.environ.get(
    'TRADING_SHEET_ID',
    '1TVyrR6-bg4fDrcljInk_cWi0h8j_zD5AePRmbwz_rmo')

WORKSPACE_DIR = REPO_ROOT / '.agent' / 'workspaces' / 'personal'
TOKEN_FILE = Path(os.environ.get(
    'GOOGLE_TOKEN_PATH',
    WORKSPACE_DIR / 'token_drive.json'))
CREDENTIALS_FILE = Path(os.environ.get(
    'GOOGLE_CREDENTIALS_PATH',
    WORKSPACE_DIR / 'credentials.json'))
STATE_DIR = WORKSPACE_DIR / 'state' / 'trading_sheets'


def _build_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE))
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json(), encoding='utf-8')
    return build('sheets', 'v4', credentials=creds, cache_discovery=False)


def _all_values(service, tab, value_render='FORMULA', rows=2000):
    """Fetch tab values with formulas intact (value-ish, not computed)."""
    rng = f"'{tab}'!A1:ZZ{rows}"
    resp = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range=rng,
        valueRenderOption=value_render, majorDimension='ROWS').execute()
    return resp.get('values', [])


def update_seen_snapshot(service, tab, values):
    """Keep a local history of what we have already learned per tab.

    Stores the stub times of computed cells (formula columns) plus row count so
    future runs can diff 'what changed since last read'. Read-only against the
    spreadsheet; the snapshot itself lives in gitignored workspace state.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    seen_path = STATE_DIR / 'seen.json'
    if seen_path.exists():
        seen = json.loads(seen_path.read_text(encoding='utf-8'))
    else:
        seen = {}
    seen[tab] = {
        'snapshot_at': datetime.now().astimezone().isoformat(timespec='seconds'),
        'rows': len(values),
        'cols': max((len(r) for r in values), default=0),
    }
    seen_path.write_text(json.dumps(seen, indent=2), encoding='utf-8')
    return seen[tab]


def dump_tab(service, tab, rows, all_tabs=None):
    values = _all_values(service, tab, rows=rows)
    if not values:
        print(f'{tab}: empty')
        return

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    safe = ''.join(c if c.isalnum() or c in ' _-' else '_' for c in tab).strip()
    json_path = STATE_DIR / f'{safe}_{stamp}.json'
    json_path.write_text(
        json.dumps({'tab': tab, 'snapshot_at': stamp, 'values': values},
                   indent=2, ensure_ascii=False), encoding='utf-8')

    header = values[0]
    width = max(len(r) for r in values)
    print(f'{tab}: {len(values)} rows (incl header) x {width} cols')
    print(f'  snapshot -> {json_path.name}')
    print(f'  headers : {header}')

    # Analyze shape beyond header: which columns carry formulas?
    formula_cols = {}
    for ri, row in enumerate(values):
        for ci, cell in enumerate(row):
            if isinstance(cell, str) and cell.startswith('='):
                formula_cols.setdefault(ci, []).append(ri + 1)
    if formula_cols:
        print('  formula columns:')
        for ci, rids in sorted(formula_cols.items()):
            col = header[ci] if ci < len(header) else f'COL{ci+1}'
            samples = sorted(rids)[:3]
            more = '' if len(rids) <= len(samples) else f' ... +{len(rids)-len(samples)}'
            print(f'    {ci+1} [{col}]: first rows {samples}{more}')

    if all_tabs is not None:
        # Cross-tab: last row per tab for header comparison.
        pass


def main():
    ap = argparse.ArgumentParser(description='Read trading journal sheet (read-only)')
    ap.add_argument('--status', action='store_true', help='List tabs + sizes')
    ap.add_argument('--dump', metavar='TAB', help='Dump one tab (values + formulas)')
    ap.add_argument('--dump-all', action='store_true', help='Dump every tab')
    ap.add_argument('--rows', type=int, default=2000, help='Max rows to read')
    args = ap.parse_args()

    service = _build_service()
    meta = service.spreadsheets().get(
        spreadsheetId=SPREADSHEET_ID, fields='properties.title,sheets').execute()
    tabs = [s['properties'] for s in meta.get('sheets', [])]
    print(f"spreadsheet: {meta.get('properties', {}).get('title')}")

    if args.status or (not args.dump and not args.dump_all):
        for p in tabs:
            g = p.get('gridProperties', {})
            print(f"  {p.get('title')!r:28} rows={g.get('rowCount'):>5} "
                  f"cols={g.get('columnCount'):>3} hidden={p.get('hidden', False)}")
        return

    targets = (tabs if args.dump_all
               else [{'title': args.dump}])
    for p in targets:
        try:
            dump_tab(service, p['title'], args.rows)
        except Exception as e:  # noqa: BLE001
            print(f"  FAILED {p['title']}: {e}", file=sys.stderr)


if __name__ == '__main__':
    main()