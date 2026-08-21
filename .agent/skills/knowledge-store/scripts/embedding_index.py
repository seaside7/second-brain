#!/usr/bin/env python3
"""embedding_index.py - FAISS semantic search for the knowledge store.

Reads all knowledge entries across categories, generates embeddings via
OpenAI text-embedding-3-small (1536 dimensions), and stores them in a local
FAISS index. Supports semantic search: embed query -> find nearest neighbors.

Usage:
  python3 embedding_index.py build --workspace samudera
  python3 embedding_index.py search --workspace samudera --query "what business lines" --top 5
  python3 embedding_index.py status --workspace samudera
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
if sys.stderr.encoding != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8')

WIB = timezone(timedelta(hours=7))
BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent
sys.path.insert(0, str(BASE_DIR / '.agent' / 'scripts'))

try:
    import faiss
    import numpy as np
except ImportError:
    print('faiss-cpu not installed. Run: pip install faiss-cpu')
    sys.exit(1)


EMBEDDING_MODEL = 'text-embedding-3-small'
EMBEDDING_DIM = 1536
BATCH_SIZE = 50


def _knowledge_dir(ws):
    return BASE_DIR / '.agent' / 'workspaces' / ws / 'knowledge'


def _index_dir(ws):
    d = BASE_DIR / '.agent' / 'workspaces' / ws / 'state'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _faiss_path(ws):
    return _index_dir(ws) / 'knowledge_embeddings.faiss'


def _meta_path(ws):
    return _index_dir(ws) / 'knowledge_embeddings_meta.json'


def _openai_key():
    env = BASE_DIR / '.env'
    if env.exists():
        with open(env, 'r', encoding='utf-8') as fh:
            for line in fh:
                if line.strip().startswith('OPENAI_API_KEY='):
                    return line.strip().split('=', 1)[1].strip()
    return os.environ.get('OPENAI_API_KEY')


def _parse_entries(ws):
    """Parse all knowledge entries from category .md files."""
    kdir = _knowledge_dir(ws)
    if not kdir.exists():
        return []

    entries = []
    for md in sorted(kdir.glob('*.md')):
        category = md.stem
        text = md.read_text(encoding='utf-8')
        blocks = re.split(r'\n---\n', text)
        for i, block in enumerate(blocks):
            block = block.strip()
            if not block:
                continue
            if '###' not in block:
                continue
            title_match = re.search(r'^### (.+)', block, re.M)
            title = title_match.group(1).strip() if title_match else 'untitled'

            date_match = re.search(r'\*\*Date:\*\*\s*(.+)', block)
            date_str = date_match.group(1).strip() if date_match else ''

            tags_match = re.search(r'\*\*Tags:\*\*\s*(.+)', block)
            tags = tags_match.group(1).strip() if tags_match else ''

            conf_match = re.search(r'\*\*Confidence:\*\*\s*(\w+)', block)
            confidence = conf_match.group(1).strip() if conf_match else 'medium'

            body = re.sub(r'^### .+\n?', '', block, count=1)
            body = re.sub(r'\*\*(Date|Tags|Source|Confidence):\*\*\s*.+\n?', '', body)
            body = body.strip()

            searchable = '%s %s %s %s %s' % (title, category, tags, body, date_str)
            searchable = re.sub(r'\s+', ' ', searchable).strip()

            entries.append({
                'category': category,
                'title': title,
                'date': date_str,
                'tags': tags,
                'confidence': confidence,
                'body': body,
                'searchable': searchable,
                'file': str(md.name),
                'block_index': i,
            })

    return entries


def _embed_batch(texts, api_key):
    """Embed a batch of texts via OpenAI API."""
    import urllib.request
    url = 'https://api.openai.com/v1/embeddings'
    payload = json.dumps({
        'model': EMBEDDING_MODEL,
        'input': texts,
    }).encode('utf-8')
    req = urllib.request.Request(url, data=payload, headers={
        'Content-Type': 'application/json',
        'Authorization': 'Bearer %s' % api_key,
    })
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        return [item['embedding'] for item in data['data']]
    except Exception as e:
        print('Embedding API error: %s' % e)
        return None


def cmd_build(args):
    ws = args.workspace or 'samudera'
    api_key = _openai_key()
    if not api_key:
        print('Error: OPENAI_API_KEY not found in .env or environment')
        sys.exit(1)

    entries = _parse_entries(ws)
    if not entries:
        print('No knowledge entries found for workspace "%s"' % ws)
        print('Add entries first: knowledge_store.py add --workspace %s' % ws)
        return

    print('Building embedding index for %d entries...' % len(entries))

    texts = [e['searchable'] for e in entries]
    all_embeddings = []

    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i:i + BATCH_SIZE]
        print('  Embedding batch %d-%d of %d...' % (i, min(i + BATCH_SIZE, len(texts)),
                                                     len(texts)))
        embeddings = _embed_batch(batch, api_key)
        if embeddings is None:
            print('Embedding failed at batch %d. Aborting.' % (i // BATCH_SIZE))
            sys.exit(1)
        all_embeddings.extend(embeddings)

    vectors = np.array(all_embeddings, dtype=np.float32)
    dim = vectors.shape[1]

    index = faiss.IndexFlatL2(dim)
    index.add(vectors)

    faiss.write_index(index, str(_faiss_path(ws)))

    meta = {
        'version': 1,
        'model': EMBEDDING_MODEL,
        'dimension': dim,
        'count': len(entries),
        'built_wib': datetime.now(WIB).isoformat(timespec='seconds'),
        'entries': entries,
    }
    with open(_meta_path(ws), 'w', encoding='utf-8') as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)

    print('Index built: %s' % _faiss_path(ws))
    print('  %d entries, %d dimensions' % (len(entries), dim))


def cmd_search(args):
    ws = args.workspace or 'samudera'
    query = (args.query or '').strip()
    top_k = args.top or 5

    faiss_p = _faiss_path(ws)
    meta_p = _meta_path(ws)
    if not faiss_p.exists() or not meta_p.exists():
        print('No embedding index found. Run: embedding_index.py build --workspace %s' % ws)
        sys.exit(1)

    api_key = _openai_key()
    if not api_key:
        print('Error: OPENAI_API_KEY not found')
        sys.exit(1)

    with open(meta_p, 'r', encoding='utf-8') as fh:
        meta = json.load(fh)

    index = faiss.read_index(str(faiss_p))

    q_embeddings = _embed_batch([query], api_key)
    if not q_embeddings:
        print('Embedding failed')
        sys.exit(1)

    q_vec = np.array([q_embeddings[0]], dtype=np.float32)
    distances, indices = index.search(q_vec, min(top_k, len(meta['entries'])))

    print('Semantic search: "%s" (top %d)' % (query, top_k))
    print()
    for dist, idx in zip(distances[0], indices[0]):
        if idx < 0 or idx >= len(meta['entries']):
            continue
        e = meta['entries'][idx]
        score = max(0, 1 - dist / 100)
        print('  [%s] %s' % (e['category'], e['title']))
        print('    score: %.3f  confidence: %s' % (score, e['confidence']))
        if e.get('tags'):
            print('    tags: %s' % e['tags'])
        if e.get('date'):
            print('    date: %s' % e['date'])
        body_preview = (e.get('body', '')[:150] + '...') if len(e.get('body', '')) > 150 else e.get('body', '')
        print('    %s' % body_preview)
        print()


def cmd_status(args):
    ws = args.workspace or 'samudera'
    meta_p = _meta_path(ws)
    if not meta_p.exists():
        print('No embedding index for workspace "%s"' % ws)
        return

    with open(meta_p, 'r', encoding='utf-8') as fh:
        meta = json.load(fh)

    print('Embedding index - workspace: %s' % ws)
    print('  model: %s' % meta.get('model', '?'))
    print('  dimension: %s' % meta.get('dimension', '?'))
    print('  entries: %s' % meta.get('count', '?'))
    print('  built: %s' % meta.get('built_wib', '?'))

    categories = {}
    for e in meta.get('entries', []):
        c = e.get('category', '?')
        categories[c] = categories.get(c, 0) + 1
    print('  by category:')
    for c, n in sorted(categories.items()):
        print('    %s: %d' % (c, n))


def main():
    p = argparse.ArgumentParser(description='Knowledge Embedding Index')
    sub = p.add_subparsers(dest='cmd')
    b = sub.add_parser('build')
    b.add_argument('--workspace', default='samudera')
    s = sub.add_parser('search')
    s.add_argument('--workspace', default='samudera')
    s.add_argument('--query', required=True)
    s.add_argument('--top', type=int, default=5)
    st = sub.add_parser('status')
    st.add_argument('--workspace', default='samudera')
    args = p.parse_args()
    if args.cmd == 'build':
        cmd_build(args)
    elif args.cmd == 'search':
        cmd_search(args)
    elif args.cmd == 'status':
        cmd_status(args)
    else:
        p.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
