#!/usr/bin/env python3
"""brain_store.py - The ONE canonical Second Brain.

All notes and knowledge belong to a single brain regardless of which
dashboard view (/personal or /samudera) created them. Views are interfaces,
not separate memories. Provenance is kept via `source_ws`; visibility is
controlled by `scope` (a retrieval filter, never a separate store):

    global   -> available from every view (default)
    private  -> personal view only (hidden when context is samudera)
    samudera / personal -> reserved for future finer filtering; treated
               like global for availability but boosted in ranking

Layout:
    .agent/brain/
    ├── memory_notes.json        one note index
    ├── knowledge/<category>.md  one knowledge universe
    └── state/                   one FAISS embedding index

Usage (CLI, mainly for debugging):
    python3 brain_store.py list [--view samudera] [--limit N]
    python3 brain_store.py stats
"""
import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
if sys.stderr.encoding != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8')

BASE_DIR = Path(__file__).resolve().parent.parent.parent

BRAIN_DIR = BASE_DIR / '.agent' / 'brain'
NOTES_PATH = BRAIN_DIR / 'memory_notes.json'
KNOWLEDGE_DIR = BRAIN_DIR / 'knowledge'
BRAIN_STATE_DIR = BRAIN_DIR / 'state'

WIB = timezone(timedelta(hours=7))

VALID_SCOPES = ('global', 'samudera', 'personal', 'private')

# Heuristic private-scope triggers. Conservative on purpose: only clearly
# personal-life content becomes private; everything else stays global so
# recall works across views.
_PRIVATE_HINTS = (
    'istri', 'suami', 'anak', 'keluarga', 'family', 'wife', 'husband',
    'my kids', 'kesehatan', 'sakit', 'dokter', 'doctor appointment',
    'gaji pribadi', 'pinjaman pribadi', 'hutang pribadi',
)


def now_wib():
    return datetime.now(WIB).isoformat(timespec='seconds')


def content_hash(text):
    normalized = re.sub(r'\s+', ' ', (text or '').lower().strip())
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:16]


def scope_for(text, source_ws=''):
    """Decide the retrieval scope for a new memory. Default is global —
    the brain is shared. Only clearly personal-life content is private."""
    t = (text or '').lower()
    if any(h in t for h in _PRIVATE_HINTS):
        return 'private'
    return 'global'


# ── notes index ──────────────────────────────────────────────────────────────

def load_notes():
    if not NOTES_PATH.exists():
        return {'next_seq': 1, 'entries': {}}
    try:
        data = json.loads(NOTES_PATH.read_text(encoding='utf-8'))
        data.setdefault('next_seq', 1)
        data.setdefault('entries', {})
        return data
    except Exception:
        return {'next_seq': 1, 'entries': {}}


def save_notes(data):
    BRAIN_DIR.mkdir(parents=True, exist_ok=True)
    tmp = str(NOTES_PATH) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, str(NOTES_PATH))


def next_id(data):
    seq = data.get('next_seq', 1)
    data['next_seq'] = seq + 1
    return 'MEM-%04d' % seq


def find_by_hash(data, hash_val):
    """Active entry with identical content, or None."""
    for e in data.get('entries', {}).values():
        if e.get('hash') == hash_val and e.get('status') == 'active':
            return e
    return None


def visible_entries(entries, view):
    """Retrieval filter: hide private memories outside the personal view."""
    if (view or '').strip().lower() == 'samudera':
        return [e for e in entries if e.get('scope') != 'private']
    return list(entries)


def list_notes(limit=20, mem_type=None, status='active', view=''):
    data = load_notes()
    entries = list(data.get('entries', {}).values())
    if status:
        entries = [e for e in entries if e.get('status') == status]
    if mem_type:
        entries = [e for e in entries if e.get('type') == mem_type]
    entries.sort(key=lambda x: x.get('created_wib', ''), reverse=True)
    entries = visible_entries(entries, view)
    return entries[:limit]


def get_stats(view=''):
    data = load_notes()
    by_type = {}
    active = 0
    for e in visible_entries(list(data['entries'].values()), view):
        if e.get('status') == 'active':
            active += 1
            t = e.get('type', 'unknown')
            by_type[t] = by_type.get(t, 0) + 1
    return {'active': active, 'by_type': by_type}


def set_scope(note_id, scope):
    """Change a memory's retrieval scope. Returns (ok, message)."""
    if scope not in VALID_SCOPES:
        return False, f'invalid scope {scope!r}; use one of {VALID_SCOPES}'
    data = load_notes()
    entry = data.get('entries', {}).get(note_id)
    if not entry:
        return False, f'note {note_id} not found'
    old = entry.get('scope', 'global')
    entry['scope'] = scope
    entry['updated_wib'] = now_wib()
    save_notes(data)
    return True, f'{note_id}: {old} -> {scope}'


# ── knowledge dir ────────────────────────────────────────────────────────────

def category_file(category):
    return KNOWLEDGE_DIR / f'{category}.md'


def ensure_knowledge_dir():
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    return KNOWLEDGE_DIR


if __name__ == '__main__':
    p = argparse.ArgumentParser(description='Canonical Second Brain store')
    sub = p.add_subparsers(dest='cmd')

    li = sub.add_parser('list')
    li.add_argument('--view', default='')
    li.add_argument('--limit', type=int, default=20)

    stats = sub.add_parser('stats')
    stats.add_argument('--view', default='')

    sc = sub.add_parser('set-scope')
    sc.add_argument('--id', required=True)
    sc.add_argument('--scope', required=True)

    args = p.parse_args()
    if args.cmd == 'list':
        print(json.dumps(list_notes(view=args.view, limit=args.limit),
                         ensure_ascii=False, indent=2))
    elif args.cmd == 'stats':
        print(json.dumps(get_stats(getattr(args, 'view', '')),
                         ensure_ascii=False, indent=2))
    elif args.cmd == 'set-scope':
        ok, msg = set_scope(args.id, args.scope)
        print(msg)
        sys.exit(0 if ok else 1)
    else:
        p.print_help()
