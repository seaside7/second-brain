#!/usr/bin/env python3
"""migrate_brain.py - One-time (idempotent) migration into the canonical brain.

Merges every workspace's memory_notes.json + knowledge/*.md into
.agent/brain/, preserving provenance via source_ws and marking migrated
notes scope='global' (visible from every view). Originals are backed up,
never deleted. Re-running is a no-op once the manifest exists.

Usage:
    python3 migrate_brain.py            # run migration (no-op if done)
    python3 migrate_brain.py --status   # show manifest / dry report
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR / '.agent' / 'scripts'))

import brain_store  # noqa: E402

WORKSPACES_DIR = BASE_DIR / '.agent' / 'workspaces'
WIB = timezone(timedelta(hours=7))
MANIFEST_PATH = brain_store.BRAIN_DIR / 'migration_manifest.json'


def backup(src: Path):
    """Copy src (file or dir) next to itself with a .pre-brain.bak suffix.
    Never overwrites an existing backup."""
    dst = src.with_name(src.name + '.pre-brain.bak')
    if not src.exists() or dst.exists():
        return None
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)
    return str(dst)


def migrate_notes():
    """Merge all workspace memory_notes.json into the canonical index.

    Entries are re-id'd sequentially in chronological order so ids stay
    unique across the merged store; provenance is preserved per entry."""
    merged = {}
    ordered = []
    for ws_dir in sorted(WORKSPACES_DIR.iterdir()):
        if not ws_dir.is_dir() or ws_dir.name.startswith('__'):
            continue
        notes_path = ws_dir / 'state' / 'memory_notes.json'
        if not notes_path.exists():
            continue
        backup(notes_path)
        try:
            data = json.loads(notes_path.read_text(encoding='utf-8'))
        except Exception:
            continue
        for e in data.get('entries', {}).values():
            if not isinstance(e, dict) or not e.get('id'):
                continue
            entry = dict(e)
            entry['source_ws'] = ws_dir.name
            entry.setdefault('scope', 'global')
            ordered.append(entry)

    # chronological order for stable, readable ids
    ordered.sort(key=lambda x: x.get('created_wib') or '')
    data = brain_store.load_notes()
    existing_hashes = {e.get('hash') for e in data['entries'].values()}
    moved = 0
    for entry in ordered:
        h = entry.get('hash')
        if h and h in existing_hashes:
            continue  # identical content already in the brain
        nid = brain_store.next_id(data)
        entry['id'] = nid
        if entry.get('hash'):
            existing_hashes.add(entry['hash'])
        data['entries'][nid] = entry
        moved += 1
    brain_store.save_notes(data)
    return {'moved': moved, 'total': len(data['entries'])}


_KNOWLEDGE_HEADER = re.compile(r'^<!--\s*source:\s*(?P<ws>[\w-]+)/(?P<file>[\w./-]+)\s*-->\s*$')


def _parse_blocks(md_text, source_ws, source_file):
    """Split a knowledge .md file into dedupable blocks.

    Provenance headers from earlier migrations are honoured; everything else
    becomes a block under the given source."""
    blocks = []
    current_tag = f'{source_ws}/{source_file}'
    for para in re.split(r'\n\s*\n', md_text.strip()):
        para = para.strip()
        if not para:
            continue
        m = _KNOWLEDGE_HEADER.match(para)
        if m:
            current_tag = m.group('ws') + '/' + m.group('file')
            continue
        blocks.append((current_tag, para))
    return blocks


def migrate_knowledge():
    """Merge every workspace's knowledge/*.md into the canonical dir."""
    brain_store.ensure_knowledge_dir()
    by_cat = {}
    for ws_dir in sorted(WORKSPACES_DIR.iterdir()):
        if not ws_dir.is_dir() or ws_dir.name.startswith('__'):
            continue
        kdir = ws_dir / 'knowledge'
        backup(kdir)
        if not kdir.is_dir():
            continue
        for md in kdir.glob('*.md'):
            cat = md.stem
            try:
                text = md.read_text(encoding='utf-8')
            except Exception:
                continue
            by_cat.setdefault(cat, []).extend(
                _parse_blocks(text, ws_dir.name, md.name))

    written = {}
    for cat, blocks in sorted(by_cat.items()):
        target = brain_store.category_file(cat)
        seen = set()
        if target.exists():
            # idempotency: keep already-migrated content exactly as-is
            existing_text = target.read_text(encoding='utf-8')
            for tag, para in _parse_blocks(existing_text, '', ''):
                seen.add(re.sub(r'\s+', ' ', para.lower()))
            out_parts = [existing_text.rstrip()]
        else:
            out_parts = [f'# {cat.replace("_", " ").title()}']
            out_parts.append('')

        added = 0
        for tag, para in blocks:
            key = re.sub(r'\s+', ' ', para.lower())
            if key in seen:
                continue
            seen.add(key)
            out_parts.append(f'<!-- source: {tag} -->')
            out_parts.append('')
            out_parts.append(para)
            out_parts.append('')
            added += 1
        if added or not target.exists():
            target.write_text('\n'.join(out_parts).rstrip() + '\n', encoding='utf-8')
        written[cat] = {'added_blocks': added}
    return written


def rebuild_index():
    """Rebuild the single FAISS index over the canonical knowledge dir."""
    script = BASE_DIR / '.agent' / 'skills' / 'knowledge-store' / 'scripts' / 'embedding_index.py'
    if not script.exists():
        return 'embedding_index.py missing; skipped'
    try:
        r = subprocess.run([sys.executable, str(script), 'build'],
                           capture_output=True, text=True,
                           cwd=str(BASE_DIR), timeout=120)
        return (r.stdout + r.stderr).strip()[-300:] or 'ok'
    except Exception as e:
        return f'rebuild failed: {e}'


def run_migration():
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    if MANIFEST_PATH.exists():
        return {'status': 'already-migrated',
                'manifest': json.loads(MANIFEST_PATH.read_text(encoding='utf-8'))}

    notes_result = migrate_notes()
    knowledge_result = migrate_knowledge()
    index_result = rebuild_index()

    manifest = {
        'migrated_at': datetime.now(WIB).isoformat(timespec='seconds'),
        'version': 1,
        'notes': notes_result,
        'knowledge': knowledge_result,
        'index_rebuild': index_result,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                             encoding='utf-8')
    return {'status': 'done', 'manifest': manifest}


def main():
    ap = argparse.ArgumentParser(description='One-brain migration')
    ap.add_argument('--status', action='store_true')
    args = ap.parse_args()

    if args.status:
        if MANIFEST_PATH.exists():
            print(MANIFEST_PATH.read_text(encoding='utf-8'))
        else:
            print('not migrated yet')
        return

    result = run_migration()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
