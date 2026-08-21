#!/usr/bin/env python3
"""executive_orchestrator.py - the Executive Orchestrator (Samudera workspace).

The integration layer of the executive system. Classifies an executive request
into an intent, gathers from the MINIMUM set of workspace specialists, and
synthesizes a response - escalating to a stronger model only for genuinely
complex synthesis.

Pipeline:
  1. classify  - cheap DeepSeek call -> {category, complexity, importance};
                 heuristic fallback when no key / call fails.
  2. gather    - deterministic + category-specific: only the specialists the
                 category needs (executive-pm digest, approval-queue, meeting
                 index + transcripts, news briefings, knowledge store,
                 credentials_status for data/BI gaps).
  3. synthesize - simple categories format their gathered data directly (no LLM
                 call); complex ones build a context bundle and call OpenAI
                 (tier 'medium'), escalating to 'high' when complexity >= 7 or
                 importance >= 8.

Workspace scoping: 'samudera' reads ONLY .agent/workspaces/samudera/state/,
its meeting index, its transcripts folder, and its news category. No
cross-workspace data, ever. Pre-join (before 2026-08-18) there is no corporate
data: the 'data' category reports exactly what is missing.

Usage:
  python3 .agent/skills/executive-orchestrator/scripts/executive_orchestrator.py \
      run --workspace samudera --prompt "What should I focus on today?"
  python3 ... run --workspace samudera --prompt "..." --json
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent
sys.path.insert(0, str(BASE_DIR / '.agent' / 'scripts'))
import deepseek_call  # noqa: E402
import openai_call   # noqa: E402

WIB = timezone(timedelta(hours=7))
WS_DIR = BASE_DIR / '.agent' / 'workspaces'
SAMUDERA_STATE = WS_DIR / 'samudera' / 'state'
SAMUDERA_MEETINGS_DIR = BASE_DIR / 'Clients' / 'Samudera' / 'meetings'
SAMUDERA_MEETING_INDEX = BASE_DIR / 'journal' / 'state' / 'samudera_meetings_index.json'
NEWS_BRIEFINGS_DIR = BASE_DIR / 'journal' / 'news_briefings'
CREDENTIALS_STATUS = WS_DIR / 'samudera' / 'credentials_status.json'

EPM_CLI = BASE_DIR / '.agent' / 'skills' / 'executive-pm' / 'scripts' / 'executive_pm.py'
AQ_CLI = BASE_DIR / '.agent' / 'skills' / 'approval-queue' / 'scripts' / 'approval_queue.py'
KS_CLI = BASE_DIR / '.agent' / 'skills' / 'knowledge-store' / 'scripts' / 'knowledge_store.py'
TR_CLI = BASE_DIR / '.agent' / 'skills' / 'transformation-research' / 'scripts' / 'transformation_research.py'
DA_CLI = BASE_DIR / '.agent' / 'skills' / 'data-agent' / 'scripts' / 'data_agent.py'

CATEGORIES = ('status', 'approvals', 'briefing', 'documents', 'research',
              'knowledge', 'data', 'synthesize')

CLASSIFY_SYSTEM = (
    'You are the intent router for an executive AI (Samudera Indonesia, Head of '
    'Digital Transformation). Classify the user request into EXACTLY one category:\n'
    '- status: executive status/digest (overdue, due today, blocked, waiting, decisions, commitments)\n'
    '- approvals: pending approval-queue items / actions awaiting a decision\n'
    '- briefing: daily brief (meetings + news + focus items)\n'
    '- documents: find/read/summarize documents, meeting notes, transcripts, MOMs\n'
    '- research: industry/company/market/competitor research\n'
    '- knowledge: what has been learned/stored about the person, role, or company\n'
    '- data: metrics, KPIs, analytics, BI/numbers\n'
    '- synthesize: complex analysis, business case, strategy, tradeoffs, planning\n'
    'Reply with JSON only: {"category": "<one of the above>", '
    '"complexity": <1-10>, "importance": <1-10>}'
)


def _now_wib():
    return datetime.now(WIB).isoformat(timespec='seconds')


def _state_file(ws, name):
    if ws == 'samudera':
        return SAMUDERA_STATE / name
    return BASE_DIR / 'journal' / 'state' / name


def _read_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return None


def _sh(cmd, timeout=30):
    try:
        r = subprocess.run([sys.executable, *cmd], cwd=str(BASE_DIR),
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout or '').strip()
    except Exception as e:
        return False, str(e)


def classify(prompt):
    """Category + complexity + importance. DeepSeek classify, heuristic fallback."""
    try:
        ok, text, _ = deepseek_call.call(
            CLASSIFY_SYSTEM + '\n\nUser request: ' + prompt,
            max_tokens=120, temperature=0, timeout=30)
        if ok:
            m = re.search(r'\{.*\}', text, re.S)
            if m:
                d = json.loads(m.group(0))
                cat = d.get('category')
                if cat in CATEGORIES:
                    return cat, int(d.get('complexity', 3)), int(d.get('importance', 3))
    except Exception:
        pass

    # heuristic fallback (no key / parse failure): keyword scoring
    low = prompt.lower()
    if any(k in low for k in ('overdue', 'due today', 'blocked', 'waiting', 'decision',
                              'commitment', 'focus', 'status', 'digest', 'open ticket')):
        return 'status', 2, 3
    if any(k in low for k in ('approval', 'approve', 'approvals', 'pending action',
                              'awaiting', 'reject')):
        return 'approvals', 2, 4
    if any(k in low for k in ('brief', 'briefing', 'today', 'morning', 'meetings today')):
        return 'briefing', 3, 4
    if any(k in low for k in ('research', 'market', 'industry', 'competitor', 'trend')):
        return 'research', 6, 5
    if any(k in low for k in ('kpi', 'metrics', 'data', 'number', 'dashboard', 'bi',
                              'analytics', 'measure')):
        return 'data', 5, 5
    if any(k in low for k in ('document', 'meeting note', 'mom', 'transcript',
                              'minutes', 'record')):
        return 'documents', 4, 4
    if any(k in low for k in ('remember', 'learned', 'know about', 'who is')):
        return 'knowledge', 3, 3
    return 'synthesize', 6, 5


# ── gather: minimum specialists per category ─────────────────────────────

def _gather_digest(ws):
    ok, out = _sh([str(EPM_CLI), 'digest', '--workspace', ws])
    return out if ok else f'(digest unavailable: {out})'


def _gather_approvals(ws):
    ok, out = _sh([str(AQ_CLI), 'list', '--workspace', ws, '--json'])
    if not ok:
        return f'(approvals unavailable: {out})'
    try:
        return json.loads(out)
    except Exception:
        return out


def _gather_news(ws):
    stories = []
    for offset in range(7):
        d = (datetime.now(WIB) - timedelta(days=offset)).strftime('%Y-%m-%d')
        for mode in ('morning', 'midday'):
            data = _read_json(NEWS_BRIEFINGS_DIR / f'{d}_{mode}.json')
            if data:
                stories.extend(data.get('stories', []) or [])
        if stories:
            break
    if ws == 'samudera':
        stories = [s for s in stories if s.get('category') == 'samudera_indonesia']
    stories.sort(key=lambda s: s.get('importance', 0) or 0, reverse=True)
    return stories[:5]


def _gather_documents(ws, query):
    """Search the workspace's meeting index + transcripts folder for query terms."""
    idx = _read_json(SAMUDERA_MEETING_INDEX) if ws == 'samudera' else None
    terms = [t for t in re.split(r'\W+', (query or '').lower()) if len(t) > 2]
    docs = []
    if isinstance(idx, dict):
        src = idx.get('documents', idx.get('docs', []))
        if isinstance(src, dict):
            docs.extend(src.values())
        elif isinstance(src, list):
            docs.extend(src)
    # fall back to scanning the transcripts folder for samudera
    if ws == 'samudera' and SAMUDERA_MEETINGS_DIR.exists():
        for p in sorted(SAMUDERA_MEETINGS_DIR.rglob('*')):
            if p.is_file() and p.suffix.lower() in ('.md', '.txt'):
                docs.append({'name': p.stem, 'path': str(p)})
    seen = set()
    hits = []
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
        score = sum(1 for t in terms if t in blob)
        if terms and score == 0:
            continue
        hits.append({'title': title, 'path': path, 'score': score,
                     'date': d.get('date') or d.get('synced') or ''})
    hits.sort(key=lambda h: (-h['score'], h['date'] or ''))
    return hits[:8]


def _gather_knowledge(ws, query):
    if query:
        ok, out = _sh([str(KS_CLI), 'search', '--query', query, '--workspace', ws])
    else:
        ok, out = _sh([str(KS_CLI), 'search', '--query', ws, '--workspace', ws])
    return out if ok else f'(knowledge unavailable: {out})'


def _gather_credential_status(ws):
    if ws != 'samudera':
        return 'N/A (non-samudera workspace)'
    data = _read_json(CREDENTIALS_STATUS)
    if not data:
        return 'credentials_status.json not found'
    lines = ['Data/credential availability (pre-join constraint, join date %s):'
             % data.get('join_date', '2026-08-18')]
    for key, info in (data.get('status') or {}).items():
        st = info.get('status')
        mark = '[x]' if st == 'configured_working' else '[!]' if st == 'post_join' else '[?]'
        lines.append('%s %s: %s' % (mark, key, st))
    unknown = data.get('unknown_data_sources') or []
    if unknown:
        lines.append('Unknown/unavailable until after join: ' + ', '.join(unknown))
    return '\n'.join(lines)


MR_CLI = BASE_DIR / '.agent' / 'skills' / 'memory-recall' / 'scripts' / 'memory_recall.py'
DI_CLI = BASE_DIR / '.agent' / 'skills' / 'drive-indexer' / 'scripts' / 'drive_index.py'


def _gather_memory(ws, query):
    """Unified memory recall: knowledge (FAISS) + Drive index + state files."""
    if not MR_CLI.is_file():
        return f'(memory-recall skill not found: {MR_CLI})'
    ok, out = _sh([str(MR_CLI), 'recall', '--workspace', ws, '--query', query, '--top', '8'])
    return out if ok else f'(memory recall unavailable: {out})'


def _gather_drive_status(ws):
    """Drive index status summary."""
    idx_path = BASE_DIR / '.agent' / 'workspaces' / ws / 'state' / 'drive_index.json'
    data = _read_json(idx_path)
    if not data:
        return 'No Drive index built. Run: drive_index.py scan --workspace %s' % ws
    stats = data.get('stats', {})
    lines = ['Drive index (last scan: %s)' % data.get('indexed_wib', '?')]
    lines.append('  %d files across %d projects' % (
        stats.get('total_files', 0), stats.get('total_projects', 0)))
    for name, count in stats.get('by_project', {}).items():
        lines.append('    %s: %d files' % (name, count))
    return '\n'.join(lines)


def _gather(category, ws, prompt):
    if category == 'status':
        return {'digest': _gather_digest(ws)}
    if category == 'approvals':
        return {'approvals': _gather_approvals(ws)}
    if category == 'briefing':
        return {'digest': _gather_digest(ws), 'news': _gather_news(ws)}
    if category == 'documents':
        return {'documents': _gather_documents(ws, prompt)}
    if category == 'research':
        ok, out = _sh([str(TR_CLI), 'scan', '--workspace', ws, '--query', prompt])
        if ok:
            return {'research_scan': out}
        return {'news': _gather_news(ws), 'knowledge': _gather_knowledge(ws, prompt)}
    if category == 'knowledge':
        # Use memory recall (FAISS semantic + Drive + state) if available
        memory = _gather_memory(ws, prompt) if ws == 'samudera' else None
        knowledge = _gather_knowledge(ws, prompt)
        result = {'knowledge': knowledge}
        if memory:
            result['memory_recall'] = memory
        return result
    if category == 'data':
        ok, out = _sh([str(DA_CLI), 'query', '--workspace', ws, '--question', prompt])
        if ok:
            return {'data_agent': out}
        return {'credential_status': _gather_credential_status(ws),
                'digest_counts': _gather_digest(ws)}
    # synthesize: bring the broad picture + memory recall for samudera
    result = {'digest': _gather_digest(ws),
              'approvals': _gather_approvals(ws),
              'documents': _gather_documents(ws, ''),
              'news': _gather_news(ws)}
    if ws == 'samudera' and prompt:
        result['memory_recall'] = _gather_memory(ws, prompt)
        result['drive_status'] = _gather_drive_status(ws)
    return result


# ── synthesize ───────────────────────────────────────────────────────────

def _fmt_bundle(cat, gathered):
    parts = []
    for k, v in gathered.items():
        if isinstance(v, (dict, list)):
            parts.append('## %s\n%s' % (k, json.dumps(v, ensure_ascii=False, indent=2)))
        else:
            parts.append('## %s\n%s' % (k, v))
    return '\n\n'.join(parts)


def synthesize(category, ws, gathered, prompt, complexity, importance):
    if category in ('status', 'approvals', 'documents', 'knowledge', 'data'):
        # deterministic: the gathered specialists ARE the answer
        if category == 'approvals' and isinstance(gathered.get('approvals'), dict):
            items = gathered['approvals'].get('items', [])
            if not items:
                return 'No approval-queue items for workspace "%s".' % ws
            return ('Approval queue (%s):\n' % ws + '\n'.join(
                '- %s [%s] %s -> %s: %s' % (i['id'], i['status'], i.get('action'),
                                            i.get('target'), (i.get('detail') or '')[:120])
                for i in items))
        if category == 'data':
            # data-agent already produced the graceful answer (verbatim)
            da = gathered.get('data_agent')
            if da:
                return da
            return _gather_credential_status(ws)
        return _fmt_bundle(category, gathered)

    # briefing / research / synthesize: LLM synthesis over the gathered bundle
    tier = 'high' if (complexity >= 7 or importance >= 8) else 'medium'
    system = (
        'You are the executive assistant for the "%s" workspace (Head of Digital '
        'Transformation, Samudera Indonesia). Synthesize an answer from the '
        'gathered context below. Ground everything in that context; do not '
        'invent Samudera corporate data that is not present (pre-join, before '
        '2026-08-18, corporate access does not exist). For research, widely '
        'known public industry knowledge may be used but anything NOT in the '
        'gathered context must be labeled "public knowledge, unverified". '
        'Keep it concise and decision-oriented. Category: %s.' % (ws, category))
    ok, text, meta = openai_call.call(prompt, system=system, tier=tier,
                                       max_tokens=2600, timeout=150)
    if not ok or not text.strip():
        # reasoning models can spend the whole budget thinking - retry with
        # a larger budget and an explicit "answer directly" directive
        ok, text, _ = openai_call.call(
            prompt + '\n\nAnswer directly and fully now - do not output '
                     'reasoning, only the final answer.',
            system=system, tier=tier, max_tokens=3400, timeout=180)
    if ok:
        return text
    # OpenAI unavailable -> deterministic fallback
    return ('(AI synthesis unavailable; here is the gathered context)\n\n'
            + _fmt_bundle(category, gathered))


# ── CLI ──────────────────────────────────────────────────────────────────

def cmd_run(args):
    prompt = (args.prompt or '').strip()
    ws = (args.workspace or '').strip() or 'samudera'
    if not prompt:
        print(json.dumps({'ok': False, 'error': 'prompt is required'}))
        sys.exit(1)
    cat, complexity, importance = classify(prompt)
    gathered = _gather(cat, ws, prompt)
    response = synthesize(cat, ws, gathered, prompt, complexity, importance)
    if args.json:
        print(json.dumps({
            'ok': True, 'workspace': ws, 'category': cat,
            'complexity': complexity, 'importance': importance,
            'generated_wib': _now_wib(), 'response': response,
        }, ensure_ascii=False, indent=2))
    else:
        print(response)


def main():
    p = argparse.ArgumentParser(description='Executive Orchestrator')
    sub = p.add_subparsers(dest='cmd')
    r = sub.add_parser('run')
    r.add_argument('--workspace', default='samudera')
    r.add_argument('--prompt', required=True)
    r.add_argument('--json', action='store_true')
    args = p.parse_args()
    if args.cmd == 'run':
        cmd_run(args)
    else:
        p.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
