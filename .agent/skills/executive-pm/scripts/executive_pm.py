#!/usr/bin/env python3
"""executive_pm.py - deterministic executive digest for a workspace.

Reads the workspace's state ledgers (tickets, commitments, waiting_on,
decisions, inbox) and produces a compact executive digest: what is overdue,
due today, blocked, waiting on others, open decisions/commitments, and inbox
items needing action. No LLM call - pure, instant, traceable.

Workspace scoping mirrors server.py's _workspace_ledger_path: 'samudera'
resolves to .agent/workspaces/samudera/state/ (never the shared journal/state/
sources); every other workspace reads the shared files. A missing file yields
empty data - there is NO fallback across workspaces.

Usage:
  python3 .agent/skills/executive-pm/scripts/executive_pm.py digest --workspace samudera
  python3 .agent/skills/executive-pm/scripts/executive_pm.py digest --workspace samudera --json
  python3 .agent/skills/executive-pm/scripts/executive_pm.py risk --workspace samudera
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

WIB = timezone(timedelta(hours=7))
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))

SHARED_STATE = os.path.join(BASE_DIR, 'journal', 'state')
SAMUDERA_STATE = os.path.join(BASE_DIR, '.agent', 'workspaces', 'samudera', 'state')

OPEN_TICKET_STATES = ('todo', 'in_progress', 'blocked', 'waiting')


def _state_dir(ws):
    return SAMUDERA_STATE if ws == 'samudera' else SHARED_STATE


def _load(ws, name):
    path = os.path.join(_state_dir(ws), name)
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return None


def _label(item):
    for key in ('title', 'text', 'what', 'summary'):
        v = item.get(key)
        if v:
            return str(v)
    return '?'


def _tickets(ws):
    doc = _load(ws, 'tickets.json') or {}
    return doc.get('tickets', []) if isinstance(doc, dict) else []


def _items(ws, name):
    doc = _load(ws, name) or {}
    raw = doc.get('items') if isinstance(doc, dict) else None
    if isinstance(raw, dict):
        return list(raw.values())
    if isinstance(raw, list):
        return raw
    return []


def _today():
    return datetime.now(WIB).strftime('%Y-%m-%d')


def _digest_data(ws):
    """Return the full structured digest as a dict. Deterministic."""
    today = _today()
    tickets = _tickets(ws)
    open_tickets = [t for t in tickets if t.get('status') in OPEN_TICKET_STATES]

    def overdue(t):
        return bool(t.get('due') and t['due'] < today)

    def due_today(t):
        return t.get('due') == today

    def sort_key(t):
        p = t.get('priority', 'P3')
        order = {'P0': 0, 'P1': 1, 'P2': 2, 'P3': 3, 'P4': 4}
        return (order.get(p, 5), t.get('due') or '9999-12-31')

    waiting_items = _items(ws, 'waiting_on.json')
    decision_items = _items(ws, 'decisions.json')
    commitment_items = _items(ws, 'commitments.json')

    inbox = _load(ws, 'inbox.json') or {}
    inbox_items = inbox.get('inbox', inbox.get('items', [])) if isinstance(inbox, dict) else []
    if isinstance(inbox_items, dict):
        inbox_items = list(inbox_items.values())
    inbox_action = [i for i in inbox_items if i.get('status') in ('pending', 'new', 'needs_action')]

    open_commitments = [c for c in commitment_items if c.get('status') == 'open']
    open_decisions = [d for d in decision_items if d.get('status') == 'open']
    breached_waiting = [w for w in waiting_items if w.get('status') == 'breached']

    data = {
        'workspace': ws,
        'generated_wib': datetime.now(WIB).isoformat(timespec='seconds'),
        'counts': {
            'open_tickets': len(open_tickets),
            'overdue': len([t for t in open_tickets if overdue(t)]),
            'due_today': len([t for t in open_tickets if due_today(t)]),
            'blocked': len([t for t in open_tickets if t.get('status') == 'blocked']),
            'waiting_on_others': len(waiting_items),
            'breached_waiting': len(breached_waiting),
            'open_decisions': len(open_decisions),
            'open_commitments': len(open_commitments),
            'inbox_needs_action': len(inbox_action),
        },
        'overdue': sorted([t for t in open_tickets if overdue(t)], key=sort_key),
        'due_today': sorted([t for t in open_tickets if due_today(t)], key=sort_key),
        'blocked': sorted([t for t in open_tickets if t.get('status') == 'blocked'], key=sort_key),
        'breached_waiting': breached_waiting,
        'open_decisions': open_decisions,
        'open_commitments': open_commitments,
        'inbox_needs_action': inbox_action,
    }
    return data


def _fmt_ticket(t):
    pri = t.get('priority', 'P3')
    due = t.get('due') or 'no due'
    return f'{t.get("id", "?")} [{pri}] {_label(t)} (due {due})'


def _fmt_waiting(w):
    return f'{_label(w)} -> {w.get("owner", w.get("on", "?"))}'


def _fmt_decision(d):
    return f'{d.get("id", "?")} {_label(d)}'


def _fmt_commitment(c):
    return f'{c.get("id", "?")} {_label(c)} -> {c.get("to", "?")} (due {c.get("due") or "no due"})'


def _fmt_inbox(i):
    return f'{i.get("id", "?")} {_label(i)}'


def _digest_markdown(data):
    c = data['counts']
    lines = []
    lines.append(f'# Executive Digest - {data["workspace"]}')
    lines.append(f'Generated {data["generated_wib"]} WIB.')
    lines.append('')
    lines.append(f'**Status:** {c["open_tickets"]} open ticket(s), '
                 f'{c["overdue"]} overdue, {c["due_today"]} due today, '
                 f'{c["blocked"]} blocked | {c["waiting_on_others"]} waiting on others '
                 f'({c["breached_waiting"]} breached) | {c["open_decisions"]} open decision(s), '
                 f'{c["open_commitments"]} open commitment(s), '
                 f'{c["inbox_needs_action"]} inbox item(s) need action.')
    lines.append('')

    sections = [
        ('Overdue', data['overdue'], _fmt_ticket),
        ('Due today', data['due_today'], _fmt_ticket),
        ('Blocked', data['blocked'], _fmt_ticket),
        ('Waiting on others (breached)', data['breached_waiting'], _fmt_waiting),
        ('Open decisions', data['open_decisions'], _fmt_decision),
        ('Open commitments', data['open_commitments'], _fmt_commitment),
        ('Inbox - needs action', data['inbox_needs_action'], _fmt_inbox),
    ]
    for title, items, fmt in sections:
        lines.append(f'## {title}')
        if items:
            lines.extend(f'- {fmt(i)}' for i in items)
        else:
            lines.append('_none_')
        lines.append('')
    return '\n'.join(lines).strip()


def _risk_markdown(data):
    c = data['counts']
    risks = []
    if c['overdue']:
        risks.append(f'{c["overdue"]} overdue ticket(s) - the C-MAP commitments (due 2026-08-13) '
                     'are the known pre-join items; verify actual status.')
    if c['breached_waiting']:
        risks.append(f'{c["breached_waiting"]} waiting-on item(s) breached SLA.')
    if c['blocked']:
        risks.append(f'{c["blocked"]} ticket(s) blocked.')
    if c['open_commitments']:
        risks.append(f'{c["open_commitments"]} open commitment(s) pending delivery.')
    if c['inbox_needs_action']:
        risks.append(f'{c["inbox_needs_action"]} inbox item(s) awaiting a decision.')
    if not risks:
        risks.append('No immediate risks detected.')
    return ('# Executive Risk Snapshot\n\n' + '\n'.join(f'- {r}' for r in risks) + '\n'
            + f'\n_(generated {data["generated_wib"]} WIB from {data["workspace"]} ledgers)_')


def cmd_digest(args):
    data = _digest_data(args.workspace)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(_digest_markdown(data))


def cmd_risk(args):
    data = _digest_data(args.workspace)
    print(_risk_markdown(data))


def main():
    p = argparse.ArgumentParser(description='Executive PM digest')
    sub = p.add_subparsers(dest='cmd')
    d = sub.add_parser('digest', help='Full executive digest')
    d.add_argument('--workspace', default='samudera')
    d.add_argument('--json', action='store_true')
    r = sub.add_parser('risk', help='Risk snapshot only')
    r.add_argument('--workspace', default='samudera')
    args = p.parse_args()
    if args.cmd == 'digest':
        cmd_digest(args)
    elif args.cmd == 'risk':
        cmd_risk(args)
    else:
        p.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
