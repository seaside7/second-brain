#!/usr/bin/env python3
"""memory_classifier.py — Classify natural-language notes into memory types.

Rule-based fast path + DeepSeek LLM fallback.

Usage:
  python3 memory_classifier.py classify --text "PSP = Pelabuhan Samudera Palaran"
  python3 memory_classifier.py classify --text "CRM will release on 30 October"
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
if sys.stderr.encoding != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8')

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR / '.agent' / 'scripts'))

try:
    from reminder_engine import parse_due as _parse_due_datetime
except ImportError:            # engine missing -> reminders just carry no date
    _parse_due_datetime = None

# ── Memory types ──────────────────────────────────────────────────────────
MEMORY_TYPES = (
    'definition', 'fact', 'project_knowledge', 'decision',
    'observation', 'strategy', 'task', 'milestone', 'reminder',
)

TYPE_TO_KNOWLEDGE_CATEGORY = {
    'definition': 'glossary',
    'fact': 'business',
    'project_knowledge': 'projects',
    'decision': 'decisions',
    'observation': 'business',
    'strategy': 'business',
}

# ── Rule-based patterns ───────────────────────────────────────────────────

DEFINITION_PATTERNS = [
    r'^[A-Z][A-Za-z0-9_-]+\s*=\s*.+',           # PSP = Pelabuhan...
    r'^[A-Z][A-Za-z0-9_-]+\s+means\s+.+',       # TSJ means ...
    r'^[A-Z][A-Za-z0-9_-]+\s+is\s+(?:a |an |the )?.+',  # PSP is a port...
    r'^(?:abbreviation|acronym|meaning)\s*:',     # abbreviation: ...
]

MILESTONE_PATTERNS = [
    r'(?:will|shall|should|to be)\s+release[d]?\s+on',
    r'(?:release|launch|go.?live|deadline|target)\s+(?:date|on|is)\s',
    r'(?:due|by|before|until)\s+\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)',
    r'\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{4}',
    r'(?:phase|sprint|milestone)\s+\d.*(?:date|by|before)',
]

TASK_PATTERNS = [
    r'(?:remind|reminder)\s+(?:me\s+)?(?:to\s+)?',
    r"(?:don'?t\s+forget)",
    r'(?:make\s+sure)',
    r'(?:need\s+to|have\s+to|must|should)\s+',
    r'(?:schedule|book|arrange|set\s+up)\s+(?:a\s+)?(?:meeting|call|call\s+with)',
    r'(?:check|verify|confirm|review)\s+(?:before|with|that)',
]

DECISION_PATTERNS = [
    r'(?:we\s+)?decided\s+to',
    r'(?:decision|decided|agreed)\s*:',
    r'(?:agreed\s+to)\s+',
    r'(?:the\s+decision\s+is)\s+',
]

STRATEGY_PATTERNS = [
    r'(?:i\s+think|my\s+(?:view|opinion|thinking|take))\s',
    r'(?:strategy|strategic|approach)\s+(?:is|should|would)',
    r'(?:the\s+plan\s+is|we\s+(?:should|could|will))\s+(?:focus|prioritize|invest)',
    r'(?:biggest\s+(?:problem|challenge|issue|opportunity))\s',
]

OBSERVATION_PATTERNS = [
    r'(?:i\s+)?(?:noticed|observed|found|learned|discovered)\s+',
    r'(?:it\s+(?:seems?|appears?|looks?))\s+(?:like|that|as)',
    r'(?:pattern|trend|correlation)\s+(?:in|between|among)',
    r'(?:interesting|notable|surprising)\s',
]

PROJECT_KNOWLEDGE_PATTERNS = [
    r'(?:phase|sprint|version|release)\s+\d',
    r'(?:will\s+not\s+use|not\s+(?:in|using))\s+\w+',
    r'(?:integration|interface|connect)\s+(?:with|to|between)',
    r'(?:CRM|ERP|TOS|ETOS)\s+(?:phase|module|feature)',
]

FACT_PATTERNS = [
    r'(?:the\s+)?(?:company|team|department)\s+(?:has|have|is|are)\s+',
    r'(?:located|based|headquartered)\s+(?:in|at)',
    r'(?:founded|established|incorporated)\s+in\s+\d{4}',
]

REMINDER_PATTERNS = [
    r'(?:remind|reminder)\s+(?:me\s+)?(?:to\s+)?',
    r"(?:don'?t\s+forget\s+to)",
]


def _rule_classify(text):
    """Rule-based classification. Returns (type, confidence) or (None, 0)."""
    t = text.strip()

    for p in DEFINITION_PATTERNS:
        if re.search(p, t, re.I):
            return 'definition', 0.9

    for p in REMINDER_PATTERNS:
        if re.search(p, t, re.I):
            return 'reminder', 0.8

    for p in MILESTONE_PATTERNS:
        if re.search(p, t, re.I):
            return 'milestone', 0.85

    for p in TASK_PATTERNS:
        if re.search(p, t, re.I):
            return 'task', 0.8

    for p in DECISION_PATTERNS:
        if re.search(p, t, re.I):
            return 'decision', 0.8

    for p in STRATEGY_PATTERNS:
        if re.search(p, t, re.I):
            return 'strategy', 0.75

    for p in OBSERVATION_PATTERNS:
        if re.search(p, t, re.I):
            return 'observation', 0.7

    for p in PROJECT_KNOWLEDGE_PATTERNS:
        if re.search(p, t, re.I):
            return 'project_knowledge', 0.75

    for p in FACT_PATTERNS:
        if re.search(p, t, re.I):
            return 'fact', 0.7

    return None, 0


def _extract_date(text):
    """Try to extract a date from text. Returns YYYY-MM-DD or None."""
    t = text.lower()

    # "30 October 2026" / "October 30, 2026"
    m = re.search(r'(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+(\d{4})', t)
    if m:
        day, mon, year = m.group(1), m.group(2)[:3], m.group(3)
        months = {'jan': '01', 'feb': '02', 'mar': '03', 'apr': '04',
                  'may': '05', 'jun': '06', 'jul': '07', 'aug': '08',
                  'sep': '09', 'oct': '10', 'nov': '11', 'dec': '12'}
        return f'{year}-{months[mon]}-{day.zfill(2)}'

    m = re.search(r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+(\d{1,2}),?\s+(\d{4})', t)
    if m:
        mon, day, year = m.group(1)[:3], m.group(2), m.group(3)
        months = {'jan': '01', 'feb': '02', 'mar': '03', 'apr': '04',
                  'may': '05', 'jun': '06', 'jul': '07', 'aug': '08',
                  'sep': '09', 'oct': '10', 'nov': '11', 'dec': '12'}
        return f'{year}-{months[mon]}-{day.zfill(2)}'

    # "next week", "tomorrow" — relative dates
    # Leave for LLM to handle

    return None


def _extract_entities(text):
    """Extract capitalized abbreviations / proper nouns."""
    # Match ALL_CAPS or TitleCase words
    abbrevs = re.findall(r'\b([A-Z][A-Za-z0-9_-]{1,15})\b', text)
    # Filter common words
    stop = {'The', 'This', 'That', 'What', 'When', 'Where', 'How', 'Why',
            'It', 'I', 'We', 'They', 'And', 'Or', 'But', 'For', 'Not',
            'Will', 'Can', 'Should', 'Would', 'Could', 'Phase', 'Release',
            'System', 'Application', 'Data', 'User', 'Team', 'Company',
            'Project', 'Plan', 'Meeting', 'Schedule', 'Check', 'Make',
            'Sure', 'Don', 'Forget'}
    return list(dict.fromkeys(e for e in abbrevs if e not in stop))


def _extract_project(text):
    """Try to extract a project name from text."""
    projects = [
        'CRM', 'Salesforce', 'ETOS', 'TSJ', 'PNP', 'PSP',
        'Samudera Cockpit', 'Data Platform', 'Data Hub',
    ]
    t_upper = text.upper()
    for p in projects:
        if p.upper() in t_upper:
            return p
    return None


def _llm_classify(text):
    """DeepSeek LLM classification fallback."""
    try:
        import importlib
        mod = importlib.import_module('ai_call')
        deepseek_call = getattr(mod, 'deepseek_call', None)
        openai_call = getattr(mod, 'openai_call', None)
    except ImportError:
        deepseek_call = None
        openai_call = None

    system = """You are a memory classifier. Given a user note, classify it and extract structured data.

Return ONLY valid JSON (no markdown, no explanation):
{
  "type": "definition|fact|project_knowledge|decision|observation|strategy|task|milestone|reminder",
  "title": "short title (max 80 chars)",
  "content": "cleaned-up content for storage",
  "entities": ["list of entities mentioned"],
  "project": "project name or null",
  "date": "YYYY-MM-DD or null",
  "confidence": "high|medium|low",
  "summary": "1-2 sentence summary"
}

Types:
- definition: abbreviations, acronyms, terminology (e.g. "PSP = Pelabuhan Samudera Palaran")
- fact: factual information about companies, people, systems
- project_knowledge: info about specific project phases, integrations, decisions
- decision: a decision that was made or agreed upon
- observation: things noticed, patterns, insights
- strategy: strategic thinking, plans, priorities
- task: action items, things to do
- milestone: date-based project milestones
- reminders: things to remember or be reminded about

If the note is a definition (X = Y), extract entity and value.
If the note mentions a date, extract it as YYYY-MM-DD.
If uncertain, use confidence: "medium"."""

    prompt = f'Classify this note:\n\n"{text}"'

    ok, raw, meta = False, '', {}
    if deepseek_call is not None:
        ok, raw, meta = deepseek_call.call(
            system + '\n\n' + prompt, max_tokens=500, temperature=0.1, timeout=30)
    if not ok and openai_call is not None:
        ok, raw, meta = openai_call.call(prompt, system=system, tier='low',
                                          max_tokens=500, timeout=30)

    if not ok:
        return None

    # Parse JSON from response
    try:
        # Try to extract JSON from response
        raw = raw.strip()
        if raw.startswith('```'):
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            raw = re.sub(r'\s*```$', '', raw)
        result = json.loads(raw)
        if result.get('type') in MEMORY_TYPES:
            return result
    except (json.JSONDecodeError, KeyError):
        pass

    return None


def _extract_due(text, mem_type):
    """Full datetime (ISO YYYY-MM-DDTHH:MM) for reminder-type notes.
    Falls back to the plain date extractor's YYYY-MM-DD result."""
    if _parse_due_datetime is not None:
        try:
            iso = _parse_due_datetime(text)
            if iso:
                return iso
        except Exception:
            pass
    return _extract_date(text)


def classify(text):
    """Classify a note. Returns dict with type, title, content, entities, etc."""
    text = (text or '').strip()
    if not text:
        return {'type': 'fact', 'title': 'Empty note', 'content': text,
                'entities': [], 'project': None, 'date': None,
                'confidence': 'low', 'summary': ''}

    # Rule-based fast path
    rule_type, rule_conf = _rule_classify(text)
    due_date = _extract_due(text, rule_type) if rule_type == 'reminder' else None

    if rule_type and rule_conf >= 0.8:
        # High-confidence rule match — use rules, no LLM needed
        result = {
            'type': rule_type,
            'title': _auto_title(text, rule_type),
            'content': text,
            'entities': _extract_entities(text),
            'project': _extract_project(text),
            'date': _extract_date(text),
            'confidence': 'high',
            'summary': text[:200],
        }
        if due_date:
            result['due_date'] = due_date
        if rule_type == 'definition':
            result['category'] = 'glossary'
        elif rule_type in TYPE_TO_KNOWLEDGE_CATEGORY:
            result['category'] = TYPE_TO_KNOWLEDGE_CATEGORY[rule_type]
        return result

    # LLM fallback for ambiguous notes
    llm_result = _llm_classify(text)
    if llm_result:
        llm_result.setdefault('entities', _extract_entities(text) or llm_result.get('entities', []))
        llm_result.setdefault('project', _extract_project(text) or llm_result.get('project'))
        llm_result.setdefault('date', _extract_date(text) or llm_result.get('date'))
        if due_date:
            llm_result['due_date'] = due_date
        if llm_result.get('type') in TYPE_TO_KNOWLEDGE_CATEGORY:
            llm_result['category'] = TYPE_TO_KNOWLEDGE_CATEGORY[llm_result['type']]
        return llm_result

    # Low-confidence fallback
    return {
        'type': rule_type or 'fact',
        'title': _auto_title(text, rule_type or 'fact'),
        'content': text,
        'entities': _extract_entities(text),
        'project': _extract_project(text),
        'date': _extract_date(text),
        'confidence': 'medium',
        'summary': text[:200],
        'category': TYPE_TO_KNOWLEDGE_CATEGORY.get(rule_type or 'fact', 'misc'),
    }


def _auto_title(text, mem_type):
    """Generate a short title from the note."""
    if mem_type == 'definition':
        # "PSP = Pelabuhan Samudera Palaran" → "PSP"
        m = re.match(r'^([A-Za-z0-9_-]+)\s*=', text)
        if m:
            return m.group(1).strip()
    # Use first line, truncated
    first = text.strip().split('\n')[0][:80]
    return first


def cmd_classify(args):
    result = classify(args.text)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    p = argparse.ArgumentParser(description='Memory Classifier')
    sub = p.add_subparsers(dest='cmd')
    cl = sub.add_parser('classify')
    cl.add_argument('--text', required=True)
    args = p.parse_args()
    if args.cmd == 'classify':
        cmd_classify(args)
    else:
        p.print_help()


if __name__ == '__main__':
    main()
