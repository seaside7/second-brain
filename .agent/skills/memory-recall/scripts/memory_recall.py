#!/usr/bin/env python3
"""memory_recall.py - Unified memory recall pipeline.

Searches three sources in parallel:
  1. Knowledge store (FAISS semantic search, falls back to substring)
  2. Drive index (keyword search on local JSON index)
  3. State files (keyword search on tasks, timeline, milestones)

Ranks by: semantic_score * 0.5 + recency_score * 0.3 + confidence_score * 0.2

Usage:
  python3 memory_recall.py recall --workspace samudera --query "ETOS milestones"
  python3 memory_recall.py recall --workspace samudera --query "CRM pricing" --top 5
  python3 memory_recall.py last --workspace samudera
  python3 memory_recall.py status --workspace samudera
"""
import argparse
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

BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent
sys.path.insert(0, str(BASE_DIR / '.agent' / 'scripts'))

try:
    import workspace_resolver as ws
except ImportError:
    ws = None

WIB = timezone(timedelta(hours=7))
WEIGHTS = {'semantic': 0.5, 'recency': 0.3, 'confidence': 0.2}
CONFIDENCE_SCORES = {'high': 1.0, 'medium': 0.6, 'low': 0.3}


def _ws_dir(ws_name):
    if ws:
        ctx = ws.get(ws_name)
        return ctx.dir
    return str(BASE_DIR / '.agent' / 'workspaces' / ws_name)


# ONE canonical brain — recall searches it from every view. The workspace
# argument only influences ranking (context boost), never the search scope.
BRAIN_DIR = os.path.join(BASE_DIR, '.agent', 'brain')


def _load_json(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return None


def _read_file(path):
    if not os.path.exists(path):
        return ''
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            return fh.read()
    except Exception:
        return ''


def _keyword_score(query, text):
    """Simple keyword overlap score (punctuation-insensitive)."""
    terms = [re.sub(r'\W+', '', t).lower() for t in query.split()]
    terms = [t for t in terms if len(t) > 1]
    if not terms:
        return 0
    text_lower = text.lower()
    matches = sum(1 for t in terms if t in text_lower)
    return matches / len(terms)


def _recency_score(date_str):
    """Score based on how recent the date is (0-1)."""
    if not date_str:
        return 0.3
    try:
        dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        days_ago = (datetime.now(WIB) - dt).days
        if days_ago < 7:
            return 1.0
        elif days_ago < 30:
            return 0.8
        elif days_ago < 90:
            return 0.6
        elif days_ago < 365:
            return 0.4
        else:
            return 0.2
    except Exception:
        return 0.3


def _today_score():
    return 1.0


def _search_knowledge(ws_name, query, top_k=10):
    """Search the canonical brain via FAISS semantic, fallback to substring."""
    state_dir = os.path.join(BRAIN_DIR, 'state')
    faiss_path = os.path.join(state_dir, 'knowledge_embeddings.faiss')
    meta_path = os.path.join(state_dir, 'knowledge_embeddings_meta.json')

    results = []

    if os.path.exists(faiss_path) and os.path.exists(meta_path):
        results = _semantic_knowledge_search(meta_path, faiss_path, query, top_k)
        return results

    kdir = os.path.join(BRAIN_DIR, 'knowledge')
    if not os.path.exists(kdir):
        return []

    for md in sorted(os.listdir(kdir)):
        if not md.endswith('.md'):
            continue
        filepath = os.path.join(kdir, md)
        category = md[:-3]
        content = _read_file(filepath)
        blocks = re.split(r'\n---\n', content)
        for block in blocks:
            block = block.strip()
            if not block or block.startswith('# ') and '###' not in block:
                continue
            if block.startswith('# '):
                block = re.sub(r'^# .+\n+', '', block).strip()
            if not block:
                continue

            score = _keyword_score(query, block)
            if score == 0:
                continue

            title_match = re.search(r'^### (.+)', block, re.M)
            title = title_match.group(1).strip() if title_match else 'untitled'
            date_match = re.search(r'\*\*Date:\*\*\s*(.+)', block)
            date_str = date_match.group(1).strip() if date_match else ''
            conf_match = re.search(r'\*\*Confidence:\*\*\s*(\w+)', block)
            conf = conf_match.group(1).strip() if conf_match else 'medium'

            results.append({
                'source': 'knowledge',
                'category': category,
                'title': title,
                'date': date_str,
                'confidence': conf,
                'content': block,
                'score': round(score, 3),
            })

    results.sort(key=lambda x: -x['score'])
    return results[:top_k]


def _semantic_knowledge_search(meta_path, faiss_path, query, top_k):
    """FAISS-based semantic search of the knowledge store."""
    try:
        import faiss
        import numpy as np
    except ImportError:
        return []

    with open(meta_path, 'r', encoding='utf-8') as fh:
        meta = json.load(fh)

    index = faiss.read_index(faiss_path)

    api_key = None
    env_path = os.path.join(BASE_DIR, '.env')
    if os.path.exists(env_path):
        with open(env_path, 'r', encoding='utf-8') as fh:
            for line in fh:
                if line.strip().startswith('OPENAI_API_KEY='):
                    api_key = line.strip().split('=', 1)[1].strip()
    if not api_key:
        api_key = os.environ.get('OPENAI_API_KEY')
    if not api_key:
        return []

    import urllib.request
    url = 'https://api.openai.com/v1/embeddings'
    payload = json.dumps({
        'model': meta.get('model', 'text-embedding-3-small'),
        'input': [query],
    }).encode('utf-8')
    req = urllib.request.Request(url, data=payload, headers={
        'Content-Type': 'application/json',
        'Authorization': 'Bearer %s' % api_key,
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        q_embedding = data['data'][0]['embedding']
    except Exception:
        return []

    q_vec = np.array([q_embedding], dtype=np.float32)
    distances, indices = index.search(q_vec, min(top_k, len(meta['entries'])))

    results = []
    for dist, idx in zip(distances[0], indices[0]):
        if idx < 0 or idx >= len(meta['entries']):
            continue
        e = meta['entries'][idx]
        semantic_score = max(0.0, 1.0 - dist / 100.0)
        rec = _recency_score(e.get('date', ''))
        conf = CONFIDENCE_SCORES.get(e.get('confidence', 'medium'), 0.6)
        combined = (semantic_score * WEIGHTS['semantic'] +
                    rec * WEIGHTS['recency'] +
                    conf * WEIGHTS['confidence'])

        results.append({
            'source': 'knowledge',
            'category': e['category'],
            'title': e['title'],
            'date': e.get('date', ''),
            'confidence': e.get('confidence', 'medium'),
            'content': e.get('body', ''),
            'score': round(combined, 3),
            'semantic_raw': round(semantic_score, 3),
        })

    return results


def _search_drive(ws_name, query, top_k=10):
    """Search Drive indexes. One brain: scan EVERY workspace's index and tag
    provenance; the active view only boosts ranking later."""
    terms = [t.lower() for t in query.split() if len(t) > 1]
    if not terms:
        return []

    results = []
    seen_ids = set()
    for ws_dir in sorted((BASE_DIR / '.agent' / 'workspaces').iterdir()):
        if not ws_dir.is_dir() or ws_dir.name.startswith('__'):
            continue
        index_path = ws_dir / 'state' / 'drive_index.json'
        index = _load_json(str(index_path))
        if not index:
            continue

        for f in index.get('files', []):
            fid = f.get('id') or (f.get('name', '') + f.get('folder_path', ''))
            if fid in seen_ids:
                continue
            blob = (f.get('name', '') + ' ' + f.get('folder_path', '') +
                    ' ' + f.get('project', '') + ' ' + f.get('content', '')).lower()
            score = sum(1 for t in terms if t in blob)
            if score == 0:
                continue
            seen_ids.add(fid)

            rec = _recency_score(f.get('modifiedTime', ''))
            combined = (score / len(terms) * 0.7 + rec * 0.3)

            content_preview = f.get('content', '')[:300] if f.get('content') else ''
            results.append({
                'source': 'drive',
                'title': f.get('name', '?'),
                'project': f.get('project', '?'),
                'project_type': f.get('project_type', '?'),
                'folder_path': f.get('folder_path', ''),
                'date': f.get('modifiedTime', ''),
                'id': f.get('id', ''),
                'mimeType': f.get('mimeType', ''),
                'source_ws': ws_dir.name,
                'content': content_preview or '%s (in %s / %s)' % (
                    f.get('name', '?'),
                    f.get('project', '?'),
                    f.get('folder_path', '/'),
                ),
                'score': round(combined, 3),
            })

    results.sort(key=lambda x: -x['score'])
    return results[:top_k]


def _search_state(ws_name, query, top_k=10):
    """Search operational state + the canonical brain notes.

    - tasks/timeline/milestones: stay workspace-local (operational trackers)
    - reminders: unified store (personal brain) — searched for every view
    - memory notes: ONE brain index, private entries hidden from samudera"""
    state_dir = os.path.join(_ws_dir(ws_name), 'state')
    results = []

    for filename in ['tasks.json', 'timeline.json', 'milestones.json']:
        path = os.path.join(state_dir, filename)
        data = _load_json(path)
        if not data:
            continue

        entries = data if isinstance(data, list) else data.get('entries', {})
        if isinstance(data, dict) and isinstance(entries, dict):
            entries = list(entries.values())
        elif isinstance(data, dict) and not isinstance(entries, list):
            entries = [data]

        for i, entry in enumerate(entries):
            entry_text = json.dumps(entry, ensure_ascii=False)
            score = _keyword_score(query, entry_text)
            if score == 0:
                continue

            date_str = entry.get('date', entry.get('trigger_date', entry.get('created', '')))
            rec = _recency_score(date_str)
            if filename == 'milestones.json':
                score = min(1.0, score * 0.7 + rec * 0.3)

            results.append({
                'source': 'state',
                'file': filename,
                'entry_index': i,
                'title': entry.get('name', entry.get('title', entry.get('task', '?'))),
                'date': date_str,
                'content': entry_text[:500],
                'score': round(score, 3),
            })

    # Unified reminders (single store in the personal workspace)
    rem_path = os.path.join(BASE_DIR, '.agent', 'workspaces', 'personal',
                            'state', 'reminders.json')
    rem_data = _load_json(rem_path)
    if rem_data:
        for i, entry in enumerate(rem_data.get('reminders', [])):
            if entry.get('done'):
                continue
            entry_text = json.dumps(entry, ensure_ascii=False)
            score = _keyword_score(query, entry_text)
            if score == 0:
                continue
            rec = _recency_score(entry.get('due', ''))
            score = min(1.0, score * 0.7 + rec * 0.3)
            results.append({
                'source': 'state',
                'file': 'reminders.json',
                'entry_index': i,
                'title': entry.get('text', '?')[:60],
                'date': entry.get('due', ''),
                'content': entry_text[:500],
                'score': round(score, 3),
            })

    # ONE brain: memory notes index (private hidden from samudera view)
    notes_path = os.path.join(BRAIN_DIR, 'memory_notes.json')
    notes_data = _load_json(notes_path)
    if notes_data and isinstance(notes_data, dict):
        for nid, entry in notes_data.get('entries', {}).items():
            if entry.get('status') != 'active':
                continue
            if ws_name == 'samudera' and entry.get('scope') == 'private':
                continue
            entry_text = json.dumps(entry, ensure_ascii=False)
            score = _keyword_score(query, entry_text)
            if score == 0:
                continue
            # context boost: memory originating from the active view ranks higher
            boost = 0.05 if entry.get('source_ws') == ws_name else 0.0
            results.append({
                'source': 'memory_note',
                'file': 'brain/memory_notes.json',
                'entry_index': nid,
                'title': entry.get('title', '?'),
                'date': entry.get('date', entry.get('created_wib', '')),
                'source_ws': entry.get('source_ws', ''),
                'scope': entry.get('scope', 'global'),
                'content': entry.get('text', entry_text[:500]),
                'score': round(min(1.0, score + boost), 3),
            })

    results.sort(key=lambda x: -x['score'])
    return results[:top_k]


def _recall(ws_name, query, top_k=10):
    """Run all three searches and merge + rank results."""
    knowledge_results = _search_knowledge(ws_name, query, top_k=top_k)
    drive_results = _search_drive(ws_name, query, top_k=top_k)
    state_results = _search_state(ws_name, query, top_k=top_k)

    all_results = knowledge_results + drive_results + state_results
    all_results.sort(key=lambda x: -x['score'])

    seen = set()
    deduped = []
    for r in all_results:
        key = (r['source'], r.get('title', ''), r.get('category', ''))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)

    return deduped[:top_k]


def cmd_recall(args):
    ws_name = args.workspace or 'samudera'
    query = (args.query or '').strip()
    if not query:
        print('recall --query "<search term>"')
        sys.exit(1)

    top_k = args.top or 10
    results = _recall(ws_name, query, top_k)

    if not results:
        print('[%s] No memory results for "%s"' % (ws_name, query))
        return

    print('[%s] Memory recall: "%s" (%d results)' % (ws_name, query, len(results)))
    print()

    by_source = {}
    for r in results:
        src = r['source']
        if src not in by_source:
            by_source[src] = []
        by_source[src].append(r)

    for src in ['knowledge', 'drive', 'state']:
        items = by_source.get(src, [])
        if not items:
            continue
        print('  --- %s (%d) ---' % (src, len(items)))
        for r in items:
            date_str = r.get('date', '')
            if date_str:
                date_str = ' (%s)' % date_str[:10]
            print('  [%.3f] %s%s' % (r['score'], r.get('title', '?'), date_str))
            if r.get('category'):
                print('          category: %s' % r['category'])
            if r.get('project'):
                print('          project: %s [%s]' % (r['project'], r.get('project_type', '')))
            if r.get('semantic_raw'):
                print('          semantic: %.3f' % r['semantic_raw'])
        print()

    cache_path = os.path.join(_ws_dir(ws_name), 'state', 'last_recall.json')
    cache = {
        'query': query,
        'top_k': top_k,
        'timestamp_wib': datetime.now(WIB).isoformat(timespec='seconds'),
        'result_count': len(results),
        'results': results[:20],
    }
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, 'w', encoding='utf-8') as fh:
        json.dump(cache, fh, ensure_ascii=False, indent=2, default=lambda o: float(o) if hasattr(o, '__float__') else str(o))


def cmd_last(args):
    ws_name = args.workspace or 'samudera'
    cache_path = os.path.join(_ws_dir(ws_name), 'state', 'last_recall.json')
    cache = _load_json(cache_path)
    if not cache:
        print('[%s] No cached recall results' % ws_name)
        return

    print('[%s] Last recall: "%s" at %s (%d results)' % (
        ws_name, cache.get('query', '?'),
        cache.get('timestamp_wib', '?'),
        cache.get('result_count', 0)))
    print()
    for r in cache.get('results', []):
        print('  [%s %.3f] %s' % (r['source'], r['score'], r.get('title', '?')))


def cmd_status(args):
    ws_name = args.workspace or 'samudera'
    state_dir = os.path.join(_ws_dir(ws_name), 'state')
    kdir = os.path.join(_ws_dir(ws_name), 'knowledge')

    print('[%s] Memory Recall Status' % ws_name)
    print()

    has_faiss = os.path.exists(os.path.join(state_dir, 'knowledge_embeddings.faiss'))
    has_meta = os.path.exists(os.path.join(state_dir, 'knowledge_embeddings_meta.json'))
    print('  Knowledge FAISS index: %s' % ('available' if (has_faiss and has_meta) else 'not built'))

    has_drive = os.path.exists(os.path.join(state_dir, 'drive_index.json'))
    if has_drive:
        idx = _load_json(os.path.join(state_dir, 'drive_index.json'))
        print('  Drive index: %d files (%s)' % (
            idx.get('stats', {}).get('total_files', 0),
            idx.get('indexed_wib', '?')))
    else:
        print('  Drive index: not built')

    for f in ['tasks.json', 'timeline.json', 'milestones.json']:
        exists = os.path.exists(os.path.join(state_dir, f))
        print('  %s: %s' % (f, 'exists' if exists else 'not found'))


def main():
    p = argparse.ArgumentParser(description='Memory Recall Pipeline')
    sub = p.add_subparsers(dest='cmd')

    rc = sub.add_parser('recall')
    rc.add_argument('--workspace', default='samudera')
    rc.add_argument('--query', required=True)
    rc.add_argument('--top', type=int, default=10)

    la = sub.add_parser('last')
    la.add_argument('--workspace', default='samudera')

    st = sub.add_parser('status')
    st.add_argument('--workspace', default='samudera')

    args = p.parse_args()
    if args.cmd == 'recall':
        cmd_recall(args)
    elif args.cmd == 'last':
        cmd_last(args)
    elif args.cmd == 'status':
        cmd_status(args)
    else:
        p.print_help()


if __name__ == '__main__':
    main()
