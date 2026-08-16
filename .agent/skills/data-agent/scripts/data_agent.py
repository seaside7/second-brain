#!/usr/bin/env python3
"""data_agent.py - the Data/BI agent (Phase 3).

Read-only Data/BI agent for the Samudera workspace. Its two jobs:

  1. `availability` - report exactly what data is usable today (per the shared
     availability registry: credentials_status.json + the owner data drop
     folder). Nothing is assumed.
  2. `query` - answer a data question ONLY from sources that are genuinely
     available (owner-provided export files, or configured_working sources).
     For anything that needs Samudera corporate data that has not actually
     been provided, it replies with a graceful "data unavailable" message that
     states WHAT is missing, WHY, WHEN it is expected, and WHERE to drop it.

This agent NEVER fabricates numbers. If a number is not in a real file the
agent has seen, the agent says the data is unavailable.

Usage:
  python3 .agent/skills/data-agent/scripts/data_agent.py availability --workspace samudera
  python3 .agent/skills/data-agent/scripts/data_agent.py availability --workspace samudera --json
  python3 .agent/skills/data-agent/scripts/data_agent.py query --workspace samudera --question "What is the current fleet size?"
  python3 .agent/skills/data-agent/scripts/data_agent.py query --workspace samudera --question "..." --json
"""
import argparse
import csv
import io
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent
sys.path.insert(0, str(BASE_DIR / '.agent' / 'scripts'))
from availability_registry import (  # noqa: E402
    data_drop_dir,
    load,
    metric_domain,
    resolve,
    summary,
)

GRACE = (
    'DATA UNAVAILABLE - workspace "{ws}"\n'
    'The requested data is not available yet. This answer is NOT a number - '
    'no Samudera corporate data is assumed before {join}.\n\n'
    '{reason}\n\n'
    'What to do (after {join}):\n'
    '  - Provide a read-only export (CSV/XLSX/JSON) and drop it in:\n'
    '      {drop}\n'
    '  - Or grant read-only access to the source ({source}) and mark it '
    'configured in credentials_status.json.\n'
    'Until a real file or configured source exists, this query returns '
    '"data unavailable" - never a guess.'
)


def _fmt_available(question, domain, files):
    lines = ['DATA AVAILABLE - domain "%s" (real owner-provided file(s))' % domain]
    for f in files:
        lines.append('  - %s (%d bytes, modified %s)'
                     % (f['path'], f['size'], f['modified']))
        lines.append('    Header: %s' % _csv_header(f['path']))
    lines.append('')
    lines.append('Only figures that actually appear in the above file(s) may be '
                 'reported. No numbers were fabricated.')
    return '\n'.join(lines)


def _csv_header(path):
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as fh:
            sample = fh.read(8192)
        dialect = csv.Sniffer().sniff(sample, delimiters=',;\t')
        row = next(csv.reader(io.StringIO(sample), dialect))[:12]
        return ', '.join(c for c in row if c)
    except Exception:
        return '(unreadable / not CSV)'


def cmd_availability(args):
    ws = args.workspace or 'samudera'
    if args.json:
        print(json.dumps(load(ws), ensure_ascii=False, indent=2))
    else:
        print(summary(ws))


def cmd_query(args):
    ws = args.workspace or 'samudera'
    question = (args.question or '').strip()
    if not question:
        print(json.dumps({'ok': False, 'error': 'question is required'}))
        sys.exit(1)
    res = resolve(question, ws)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return
    if res['status'] == 'available' and res['files']:
        print(_fmt_available(question, res['domain'], res['files']))
    elif res['status'] == 'available':
        print('DATA AVAILABLE - source "%s" is configured_working.' % res['source'])
    elif res['status'] == 'unknown':
        print('Unable to route this question to a data domain.')
        print('Available domains: fleet/ops, finance, HR, procurement, customer, '
              'KPIs, BI, IT.')
        print('Run: data_agent.py availability --workspace %s' % ws)
    else:
        print(GRACE.format(
            ws=ws, join=res['expected'] or 'the join date',
            reason=res['reason'],
            source=res['source'] or '(no provisioned source)',
            drop=data_drop_dir(ws)))


def main():
    p = argparse.ArgumentParser(description='Data/BI agent (Phase 3, read-only)')
    sub = p.add_subparsers(dest='cmd')
    a = sub.add_parser('availability')
    a.add_argument('--workspace', default='samudera')
    a.add_argument('--json', action='store_true')
    q = sub.add_parser('query')
    q.add_argument('--workspace', default='samudera')
    q.add_argument('--question', required=True)
    q.add_argument('--json', action='store_true')
    args = p.parse_args()
    if args.cmd == 'availability':
        cmd_availability(args)
    elif args.cmd == 'query':
        cmd_query(args)
    else:
        p.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
