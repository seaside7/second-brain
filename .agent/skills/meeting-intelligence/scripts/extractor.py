"""
Knowledge Extractor — Processes MeetingRecords into normalized tasks and knowledge.

Responsibilities:
- Extract action items → Universal Task Schema → tasks.json
- Extract decisions → meeting_decisions.json
- Extract follow-ups → meeting_followups.json
- Extract ideas → meeting_ideas.json
- Deduplicate using deterministic keys

Does NOT prioritize. That's the Inbox Engine's job.
"""
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, '..', '..', '..', '..'))

sys.path.insert(0, os.path.join(REPO_ROOT, '.agent', 'scripts'))
sys.path.insert(0, os.path.join(REPO_ROOT, '.agent', 'workspaces'))

import task_store

WIB = timezone(timedelta(hours=7))

DECISIONS_PATH = os.path.join(REPO_ROOT, 'journal', 'state', 'meeting_decisions.json')
FOLLOWUPS_PATH = os.path.join(REPO_ROOT, 'journal', 'state', 'meeting_followups.json')
IDEAS_PATH = os.path.join(REPO_ROOT, 'journal', 'state', 'meeting_ideas.json')

# Owner identity for assignee detection
OWNER_NAMES = {'said', 'said iskandar'}


def _dedupe_key(source, source_id, text):
    """Generate deterministic dedupe key."""
    content_hash = hashlib.sha256(text.lower().strip().encode('utf-8')).hexdigest()[:12]
    return f"meeting:{source}:{source_id}:{content_hash}"


def _load_state(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {'items': []}


def _save_state(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _is_owner(name):
    """Check if a name refers to the owner (Said)."""
    if not name:
        return False
    return name.lower().strip() in OWNER_NAMES


def _infer_project_from_title(title, workspace):
    """Try to infer project name from meeting title."""
    title_lower = title.lower()
    # Common project keywords for Catalyze
    project_keywords = {
        'abnj': 'ABNJ',
        'seamap': 'ERIA - SEAMAP',
        'sea-map': 'ERIA - SEAMAP',
        'sea map': 'ERIA - SEAMAP',
        'katingan': 'Katingan',
        'wwf': 'WWF Data Platform',
        'data platform': 'WWF Data Platform',
        'data hub': 'WWF Data Hub',
        'datahub': 'WWF Data Hub',
        'viriya': 'Viriya',
        'eqi': 'EQI',
        'ykan': 'YKAN - KHG EXPLORER + MRV (Phase 1))',
    }
    for keyword, project in project_keywords.items():
        if keyword in title_lower:
            return project
    return ''


def extract_from_meeting(meeting_record):
    """Extract all knowledge from a MeetingRecord.

    Processes:
    1. Structured action items (from Fathom) → tasks.json (confidence 0.95)
    2. Decisions, follow-ups, ideas from summary → respective state files

    Returns:
        dict with counts: {tasks_created, tasks_skipped, decisions, followups, ideas}
    """
    record = meeting_record if isinstance(meeting_record, dict) else meeting_record.to_dict()
    stats = {'tasks_created': 0, 'tasks_skipped': 0, 'decisions': 0, 'followups': 0, 'ideas': 0}

    workspace = record.get('workspace', '')
    source = record.get('source', 'fathom')
    source_id = record.get('source_id', '')
    meeting_id = record.get('id', '')
    meeting_title = record.get('title', '')
    meeting_date = record.get('date', '')
    source_url = record.get('source_url', '')
    project = _infer_project_from_title(meeting_title, workspace)

    # --- 1. Extract action items into Universal Task Store ---
    for item in record.get('action_items', []):
        description = item.get('description', '').strip()
        if not description:
            continue

        assignee_name = item.get('assignee_name', '')
        assignee_email = item.get('assignee_email', '')
        playback_url = item.get('playback_url', '')
        timestamp = item.get('timestamp', '')

        # Build source ref (link back to the exact moment)
        ref = playback_url or source_url
        if not ref and source_url and timestamp:
            ref = f"{source_url}#t={timestamp}"

        # Determine assignee display
        assignee = assignee_name or ''

        # Determine requester (the person who gave the action, not the assignee)
        # For meeting action items, the requester is contextual - leave blank if unclear
        requester = ''

        # Try to extract due date from description
        due_date = None
        due_patterns = [
            r'by\s+(\d{4}-\d{2}-\d{2})',
            r'before\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)',
            r'by\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)',
            r'due\s+(\d{4}-\d{2}-\d{2})',
        ]
        for pattern in due_patterns:
            match = re.search(pattern, description, re.IGNORECASE)
            if match:
                val = match.group(1)
                if re.match(r'\d{4}-\d{2}-\d{2}', val):
                    due_date = val
                break

        task = task_store.append_task({
            'workspace': workspace,
            'source': 'meeting',
            'source_ref': ref,
            'source_id': source_id,
            'project': project,
            'title': description[:120],
            'description': description,
            'priority': 'unknown',  # Meeting engine does NOT prioritize
            'requester': requester,
            'assignee': assignee,
            'due_date': due_date,
            'meeting_id': meeting_id,
            'confidence': 0.95,  # Structured Fathom action items = high confidence
            'metadata': {
                'meeting_title': meeting_title,
                'meeting_date': meeting_date,
                'timestamp': timestamp,
            },
        })

        if task:
            stats['tasks_created'] += 1
        else:
            stats['tasks_skipped'] += 1

    # --- 2. Extract decisions from summary (simple pattern matching for v1) ---
    summary = record.get('summary', '')
    if summary:
        decisions_state = _load_state(DECISIONS_PATH)
        existing_keys = {d.get('dedupe_key') for d in decisions_state.get('items', [])}

        # Look for decision-like sentences
        decision_patterns = [
            r'(?:agreed|decided|confirmed|approved|resolved)\s+(?:to\s+)?(.+?)(?:\.|$)',
            r'decision:\s*(.+?)(?:\.|$)',
        ]
        for pattern in decision_patterns:
            for match in re.finditer(pattern, summary, re.IGNORECASE):
                text = match.group(1).strip()
                if len(text) < 10:
                    continue
                key = _dedupe_key(source, source_id, text)
                if key in existing_keys:
                    continue
                decisions_state['items'].append({
                    'dedupe_key': key,
                    'workspace': workspace,
                    'meeting_id': meeting_id,
                    'meeting_title': meeting_title,
                    'meeting_date': meeting_date,
                    'text': text,
                    'source_url': source_url,
                    'created_at': datetime.now(WIB).isoformat(timespec='seconds'),
                })
                existing_keys.add(key)
                stats['decisions'] += 1

        _save_state(DECISIONS_PATH, decisions_state)

    return stats
