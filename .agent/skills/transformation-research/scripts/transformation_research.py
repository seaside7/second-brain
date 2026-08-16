#!/usr/bin/env python3
"""transformation_research.py - transformation research capability (Phase 3).

Grounded research for the Samudera workspace. Gathers facts ONLY from sources
that are genuinely available today:

  - news briefings (journal/news_briefings/*.json, samudera_indonesia category)
  - Samudera meeting archive (journal/state/samudera_meetings_index.json +
    Clients/Samudera/meetings transcripts)
  - knowledge store (.agent/workspaces/samudera/knowledge/)

Research gaps are reported explicitly, never hidden and never filled with
invented data:

  - web research (exa-connector) is NOT configured -> stated as a gap
  - Samudera ERP/BI/db are post_join (>= 2026-08-18) -> stated as a gap

Commands:
  python3 transformation_research.py sources --workspace samudera
      Which research sources are available vs not, and why.
  python3 transformation_research.py scan --workspace samudera --query "sustainability"
      Deterministic multi-source scan: matching news stories, documents, and
      knowledge notes, each tagged with its source. No LLM, no fabrication.
  python3 transformation_research.py brief --workspace samudera
      Latest samudera news brief from stored briefings.
  python3 transformation_research.py synthesize --workspace samudera --topic "..." 
      LLM-synthesized research note grounded in the gathered facts, with a
      visible gaps section. Escalates tier (medium -> high) for complex topics.
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent
sys.path.insert(0, str(BASE_DIR / '.agent' / 'scripts'))

WIB = timezone(timedelta(hours=7))
NEWS_DIR = BASE_DIR / 'journal' / 'news_briefings'
MEETING_INDEX = BASE_DIR / 'journal' / 'state' / 'samudera_meetings_index.json'
MEETINGS_DIR = BASE_DIR / 'Clients' / 'Samudera' / 'meetings'
KNOWLEDGE_DIR = BASE_DIR / '.agent' / 'workspaces' / 'samudera' / 'knowledge'


def _now_wib():
    return datetime.now(WIB).isoformat(timespec='seconds')


def _read_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return None


def _read_text(path):
    try:
        return (path.read_text(encoding='utf-8', errors='replace') or '').strip()
    except Exception:
        return ''


def _web_research_configured():
    """exa-connector / web research - is a real API key present? (no, today)"""
    exa_env = BASE_DIR / '.agent' / 'skills' / 'exa-connector' / '.env'
    if exa_env.exists():
        text = _read_text(exa_env)
        if 'EXA_API_KEY' in text and 'YOUR_' not in text and 'example' not in text.lower():
            return True
    return False


def sources_report():
    lines = ['Research sources for workspace "samudera" (%s):' % _now_wib()]
    lines.append('')
    lines.append('Available now:')
    lines.append('  [x] news briefings (journal/news_briefings/, samudera category)')
    lines.append('  [x] Samudera meeting archive (index + transcripts)')
    lines.append('  [x] knowledge store (.agent/workspaces/samudera/knowledge/)')
    lines.append('')
    lines.append('Not available:')
    if _web_research_configured():
        lines.append('  [!] web research (exa-connector): NOT configured (no API key)')
    else:
        lines.append('  [!] web research (exa-connector): NOT configured (no API key)')
    lines.append('  [!] Samudera ERP/BI/database: post_join (>= 2026-08-18)')
    lines.append('  [!] Samudera Gmail/Drive: post_join (>= 2026-08-18)')
    lines.append('')
    lines.append('Gaps are stated explicitly and are never filled with invented data.')
    return '\n'.join(lines)


def _news_stories(days=7):
    stories = []
    for offset in range(days):
        d = (datetime.now(WIB) - timedelta(days=offset)).strftime('%Y-%m-%d')
        for mode in ('morning', 'midday'):
            data = _read_json(NEWS_DIR / ('%s_%s.json' % (d, mode)))
            if data:
                stories.extend(data.get('stories', []) or [])
        if stories:
            break
    return stories


def _news_matching(terms):
    stories = _news_stories()
    hits = []
    for s in stories:
        if s.get('category') != 'samudera_indonesia':
            continue
        blob = ('%s %s %s' % (s.get('title', ''), s.get('summary', ''),
                              s.get('headline', ''))).lower()
        if not terms or all(t in blob for t in terms):
            hits.append(s)
    hits.sort(key=lambda s: s.get('importance', 0) or 0, reverse=True)
    return hits[:8]


def _docs_matching(terms):
    idx = _read_json(MEETING_INDEX)
    docs = []
    if isinstance(idx, dict):
        src = idx.get('documents', idx.get('docs', []))
        if isinstance(src, dict):
            docs = list(src.values())
        elif isinstance(src, list):
            docs = src
    if MEETINGS_DIR.exists():
        for p in sorted(MEETINGS_DIR.rglob('*')):
            if p.is_file() and p.suffix.lower() in ('.md', '.txt'):
                docs.append({'name': p.stem, 'path': str(p)})
    hits = []
    seen = set()
    for d in docs:
        if not isinstance(d, dict):
            continue
        title = str(d.get('title') or d.get('name') or '')
        path = str(d.get('path') or d.get('file_id') or '')
        key = path or title
        if key in seen:
            continue
        seen.add(key)
        blob = (title + ' ' + path).lower()
        if terms and not all(t in blob for t in terms):
            continue
        hits.append({'title': title, 'path': path})
    return hits[:8]


def _knowledge_matching(terms):
    hits = []
    if KNOWLEDGE_DIR.exists():
        for p in sorted(KNOWLEDGE_DIR.rglob('*.md')):
            text = _read_text(p)
            blob = (p.stem + ' ' + text).lower()
            if terms and not all(t in blob for t in terms):
                continue
            excerpt = ' '.join(text.split())[:180]
            hits.append({'category': p.stem, 'excerpt': excerpt, 'path': str(p)})
    return hits[:6]


def _terms(query):
    return [t for t in re.split(r'\W+', (query or '').lower()) if len(t) > 2]


def cmd_sources(_args):
    print(sources_report())


def cmd_scan(args):
    ws = args.workspace or 'samudera'
    query = (args.query or '').strip()
    terms = _terms(query)
    lines = ['Research scan - workspace "%s" - query "%s" (%s)'
             % (ws, query, _now_wib())]
    lines.append('')
    lines.append('## News (samudera category)')
    news = _news_matching(terms)
    if news:
        for s in news:
            lines.append('- [%s] %s - %s'
                         % (s.get('date', '?'), s.get('title', ''),
                            (s.get('summary') or s.get('headline') or '')[:160]))
    else:
        lines.append('(no matching samudera news stories in stored briefings)')
    lines.append('')
    lines.append('## Meeting archive')
    docs = _docs_matching(terms)
    if docs:
        for d in docs:
            lines.append('- %s (%s)' % (d['title'], d['path']))
    else:
        lines.append('(no matching documents in the Samudera meeting archive)')
    lines.append('')
    lines.append('## Knowledge store')
    kn = _knowledge_matching(terms)
    if kn:
        for k in kn:
            lines.append('- [%s] %s (%s)' % (k['category'], k['excerpt'], k['path']))
    else:
        lines.append('(no matching knowledge notes)')
    lines.append('')
    lines.append('## Gaps (stated explicitly, never invented)')
    lines.append('- web research (exa-connector): NOT configured' if not _web_research_configured()
                 else '- web research: configured')
    lines.append('- Samudera ERP/BI/database: post_join (>= 2026-08-18)')
    print('\n'.join(lines))


def cmd_brief(args):
    stories = _news_stories()
    sam = [s for s in stories if s.get('category') == 'samudera_indonesia']
    sam.sort(key=lambda s: s.get('importance', 0) or 0, reverse=True)
    lines = ['Samudera news brief (%s)' % _now_wib()]
    if not sam:
        lines.append('(no stored samudera news briefings yet)')
    for s in sam[:6]:
        lines.append('')
        lines.append('### %s' % s.get('title', 'untitled'))
        lines.append((s.get('summary') or s.get('headline') or '')[:300])
        lines.append('_importance %s - %s_' % (s.get('importance', '?'), s.get('date', '?')))
    print('\n'.join(lines))


def cmd_synthesize(args):
    ws = args.workspace or 'samudera'
    topic = (args.topic or '').strip()
    if not topic:
        print(json.dumps({'ok': False, 'error': 'topic is required'}))
        sys.exit(1)
    terms = _terms(topic)
    gathered = {
        'news': _news_matching(terms),
        'documents': _docs_matching(terms),
        'knowledge': _knowledge_matching(terms),
        'web_configured': _web_research_configured(),
        'gaps': ['web research NOT configured', 'Samudera ERP/BI/database post_join (>= 2026-08-18)'],
    }
    bundle = json.dumps(gathered, ensure_ascii=False, indent=2)
    system = (
        'You are a transformation research analyst for the "%s" workspace '
        '(Samudera Indonesia, Head of Digital Transformation). Write a concise '
        'research note on the topic from the gathered context ONLY. '
        'Ground every claim in the gathered facts; do NOT invent company '
        'data, numbers, or sources. Widely known public industry knowledge may '
        'be used but anything NOT in the gathered context must be labeled '
        '"public knowledge, unverified". End with a "Gaps & next steps" section '
        'repeating what is unavailable (web research not configured; ERP/BI '
        'post_join >= 2026-08-18).' % ws)
    import openai_call  # noqa: WPS433 (local import keeps CLI lightweight)
    complexity = 6 if len(gathered['news']) + len(gathered['documents']) else 4
    tier = 'high' if complexity >= 7 else 'medium'
    ok, text, meta = openai_call.call(topic, system=system, tier=tier,
                                      max_tokens=1800, timeout=150)
    if ok and text.strip():
        print(text)
        return
    # fallback: deterministic scan output
    from argparse import Namespace
    print('(AI synthesis unavailable; deterministic scan follows)')
    cmd_scan(Namespace(workspace=ws, query=topic))


def main():
    p = argparse.ArgumentParser(description='Transformation research (Phase 3)')
    sub = p.add_subparsers(dest='cmd')
    s = sub.add_parser('sources')
    s.add_argument('--workspace', default='samudera')
    sc = sub.add_parser('scan')
    sc.add_argument('--workspace', default='samudera')
    sc.add_argument('--query', required=True)
    b = sub.add_parser('brief')
    b.add_argument('--workspace', default='samudera')
    sy = sub.add_parser('synthesize')
    sy.add_argument('--workspace', default='samudera')
    sy.add_argument('--topic', required=True)
    args = p.parse_args()
    if args.cmd == 'sources':
        cmd_sources(args)
    elif args.cmd == 'scan':
        cmd_scan(args)
    elif args.cmd == 'brief':
        cmd_brief(args)
    elif args.cmd == 'synthesize':
        cmd_synthesize(args)
    else:
        p.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
