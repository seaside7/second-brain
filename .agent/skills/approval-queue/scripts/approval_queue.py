#!/usr/bin/env python3
"""approval_queue.py - human-approval gate for external actions.

Proposed actions (send email, post to a channel, create a document, commit,
etc.) are queued as pending items instead of being executed speculatively. A
human approves or rejects each one. Every state transition is appended to
journal/state/action_audit.jsonl (append-only) so there is a complete,
unalterable trail of what was proposed and how it was decided.

Single-writer pattern: dashboard endpoints shell out to this CLI (mirrors
commitment_ledger.py / inbox_sweep.py / command_queue.py), so the queue file
and the audit log each have exactly one writer at a time.

Workspace scoping: items are tagged with their workspace; 'samudera' items are
never served to non-samudera views and vice versa. The ledger file is SHARED
(journal/state/approval_queue.json); only the listing filters by workspace.

Usage:
  python3 .../approval_queue.py add    --workspace samudera --action gmail_send --target gmail:id --detail "Send C-MAP summary to Pak Rando" --project C-MAP
  python3 .../approval_queue.py list   --workspace samudera
  python3 .../approval_queue.py list   --status pending
  python3 .../approval_queue.py approve --id APR-0001 --workspace samudera --note "go ahead"
  python3 .../approval_queue.py reject  --id APR-0001 --workspace samudera --note "hold"
  python3 .../approval_queue.py execute --id APR-0001 --workspace samudera
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

WIB = timezone(timedelta(hours=7))
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))
QUEUE_PATH = os.path.join(BASE_DIR, 'journal', 'state', 'approval_queue.json')
AUDIT_PATH = os.path.join(BASE_DIR, 'journal', 'state', 'action_audit.jsonl')

# Action types with a registered executor. EXTEND here (never add a write-path
# executor without a review) - anything not in this map refuses to execute.
EXECUTORS = {}

EMPTY_QUEUE = {'next_seq': 1, 'items': {}}


def _now_wib():
    return datetime.now(WIB).isoformat(timespec='seconds')


def _audit(actor, action, workspace, item_id, detail):
    """Append one immutable line to the audit log. Append-only by construction."""
    entry = {
        'ts_wib': _now_wib(),
        'actor': actor,
        'action': action,
        'workspace': workspace,
        'item_id': item_id,
        'detail': detail,
    }
    os.makedirs(os.path.dirname(AUDIT_PATH), exist_ok=True)
    with open(AUDIT_PATH, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + '\n')


def _load_queue():
    try:
        with open(QUEUE_PATH, 'r', encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return dict(EMPTY_QUEUE)


def _save_queue(q):
    os.makedirs(os.path.dirname(QUEUE_PATH), exist_ok=True)
    tmp = QUEUE_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(q, fh, ensure_ascii=False, indent=2)
    json.loads(open(tmp, encoding='utf-8').read())
    os.replace(tmp, QUEUE_PATH)


def cmd_add(args):
    q = _load_queue()
    seq = int(q.get('next_seq', 1))
    iid = 'APR-%04d' % seq
    q['next_seq'] = seq + 1
    item = {
        'id': iid,
        'workspace': args.workspace,
        'action': args.action,
        'target': args.target,
        'detail': args.detail,
        'project': args.project,
        'status': 'pending',
        'proposed_wib': _now_wib(),
        'decided_wib': None,
        'decision_note': None,
        'executed_wib': None,
    }
    q.setdefault('items', {})[iid] = item
    _save_queue(q)
    _audit('system', 'propose', args.workspace, iid, f'{args.action} -> {args.target}: {args.detail[:160]}')
    print(json.dumps({'ok': True, 'id': iid, 'status': 'pending'}, ensure_ascii=False))


def cmd_list(args):
    q = _load_queue()
    items = list((q.get('items') or {}).values())
    if args.workspace:
        items = [i for i in items if i.get('workspace') == args.workspace]
    if args.status:
        items = [i for i in items if i.get('status') == args.status]
    items.sort(key=lambda i: i.get('proposed_wib', ''))
    if args.json:
        print(json.dumps({'count': len(items), 'items': items}, ensure_ascii=False, indent=2))
    else:
        for i in items:
            print(f'{i["id"]} [{i.get("workspace")}] {i.get("status"):9s} '
                  f'{i.get("action")} -> {i.get("target")}: {i.get("detail", "")[:120]}')
        print(f'({len(items)} item(s))')


def _find(args):
    q = _load_queue()
    item = (q.get('items') or {}).get(args.id)
    if not item:
        print(json.dumps({'ok': False, 'error': f'no queue item {args.id}'}))
        sys.exit(1)
    if args.workspace and item.get('workspace') != args.workspace:
        print(json.dumps({'ok': False, 'error': f'{args.id} belongs to workspace {item.get("workspace")}'}))
        sys.exit(1)
    return q, item


def cmd_approve(args):
    q, item = _find(args)
    if item['status'] != 'pending':
        print(json.dumps({'ok': False, 'error': f'{args.id} is already {item["status"]}'}))
        sys.exit(1)
    item['status'] = 'approved'
    item['decided_wib'] = _now_wib()
    item['decision_note'] = args.note
    _save_queue(q)
    _audit('owner', 'approve', item['workspace'], args.id, args.note or '')
    print(json.dumps({'ok': True, 'id': args.id, 'status': 'approved'}, ensure_ascii=False))


def cmd_reject(args):
    q, item = _find(args)
    if item['status'] != 'pending':
        print(json.dumps({'ok': False, 'error': f'{args.id} is already {item["status"]}'}))
        sys.exit(1)
    item['status'] = 'rejected'
    item['decided_wib'] = _now_wib()
    item['decision_note'] = args.note
    _save_queue(q)
    _audit('owner', 'reject', item['workspace'], args.id, args.note or '')
    print(json.dumps({'ok': True, 'id': args.id, 'status': 'rejected'}, ensure_ascii=False))


def cmd_execute(args):
    q, item = _find(args)
    if item['status'] != 'approved':
        print(json.dumps({'ok': False, 'error': f'{args.id} is {item["status"]}; only approved items execute'}))
        sys.exit(1)
    executor = EXECUTORS.get(item.get('action'))
    if executor is None:
        print(json.dumps({'ok': False, 'error': f'no executor registered for action type '
                                                 f'{item.get("action")} - external dispatch not configured'}))
        sys.exit(1)
    executor(item)
    item['executed_wib'] = _now_wib()
    _save_queue(q)
    _audit('system', 'execute', item['workspace'], args.id, item.get('action'))
    print(json.dumps({'ok': True, 'id': args.id, 'status': 'executed'}, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description='Human-approval gate for external actions')
    sub = p.add_subparsers(dest='cmd')

    a = sub.add_parser('add')
    a.add_argument('--workspace', required=True)
    a.add_argument('--action', required=True, help='action type, e.g. gmail_send, slack_post, drive_doc')
    a.add_argument('--target', required=True, help='target ref, e.g. gmail:<thread>')
    a.add_argument('--detail', required=True)
    a.add_argument('--project', default='')

    l = sub.add_parser('list')
    l.add_argument('--workspace', default=None)
    l.add_argument('--status', default=None, choices=('pending', 'approved', 'rejected', 'executed'))
    l.add_argument('--json', action='store_true')

    for name, note in (('approve', 'approved'), ('reject', 'rejected')):
        c = sub.add_parser(name)
        c.add_argument('--id', required=True)
        c.add_argument('--workspace', default=None)
        c.add_argument('--note', default='')

    e = sub.add_parser('execute')
    e.add_argument('--id', required=True)
    e.add_argument('--workspace', default=None)

    args = p.parse_args()
    if args.cmd == 'add':
        cmd_add(args)
    elif args.cmd == 'list':
        cmd_list(args)
    elif args.cmd == 'approve':
        cmd_approve(args)
    elif args.cmd == 'reject':
        cmd_reject(args)
    elif args.cmd == 'execute':
        cmd_execute(args)
    else:
        p.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
