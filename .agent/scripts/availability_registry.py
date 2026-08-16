#!/usr/bin/env python3
"""availability_registry.py - canonical data-availability registry for the
Samudera executive layer (Phase 3 infrastructure).

Single source of truth for "what data can we actually use today". Read by the
data-agent (Data/BI) and transformation-research (research) skills, and by the
executive orchestrator when it needs to know what is and is not available.

NOTHING IS ASSUMED. A data source is "available" ONLY when one of these holds:
  1. credentials_status.json marks it `configured_working`, or
  2. an actual file exists in the owner-provided data drop folder
     (.agent/workspaces/samudera/data/), i.e. the owner really did drop a
     Samudera export there after joining.

Everything else is reported as UNAVAILABLE, with the reason, the expected
availability date, and what the owner must provide to make it available. This
module never returns numbers it has not seen in a real file.

API:
  load(workspace='samudera')        -> availability dict (see below)
  summary(workspace='samudera')     -> human-readable availability report
  metric_domain(question)           -> domain string or None (heuristic)
  resolve(question, workspace=...)  -> {domain, source, status, files, reason}
"""
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

WIB = timezone(timedelta(hours=7))
BASE_DIR = Path(__file__).resolve().parent.parent.parent
WS_DIR = BASE_DIR / '.agent' / 'workspaces'

DEFAULT_WORKSPACE = 'samudera'
JOIN_DATE = '2026-08-18'
JOIN_DATE_MEANING = ('First day at Samudera Indonesia. Before this date NO '
                     'Samudera corporate data is assumed available.')

# ── data domains a metric/research question can map to ─────────────────────
# keyword -> (domain, canonical source key in credentials_status.json)
DOMAIN_KEYWORDS = {
    'fleet':        (['fleet', 'vessel', 'vessels', 'ship', 'ships', 'armada', 'kapal'],
                     'fleet', 'samudera_db_read'),
    'ops':          (['operations', 'ops', 'terminal', 'terminals', 'logistics',
                      'cargo', 'port', 'turnaround', 'handling', 'b2b', 'b2c',
                      'container', 'teu'],
                     'ops', 'samudera_db_read'),
    'finance':      (['finance', 'financial', 'budget', 'revenue', 'profit',
                      'cost', 'margin', 'capex', 'opex', 'cash', 'p&l', 'p/l'],
                     'finance', 'samudera_erp'),
    'hr':           (['hr', 'people', 'employee', 'employees', 'headcount',
                      'staff', 'sdm', 'talent'],
                     'hr', 'samudera_db_read'),
    'procurement':  (['procurement', 'vendor', 'vendors', 'supplier', 'suppliers',
                      'purchase', 'po number'],
                     'procurement', 'samudera_erp'),
    'customer':     (['customer', 'customers', 'client', 'clients', 'nps', 'crm'],
                     'customer', 'samudera_bi'),
    'kpi':          (['kpi', 'metric', 'metrics', 'performance', 'target', 'targets',
                      'scorecard', 'okr', 'measure', 'measures', 'indicator'],
                     'kpi', 'samudera_bi'),
    'bi':           (['bi', 'dashboard', 'dashboards', 'analytics', 'metabase',
                      'power bi', 'tableau'],
                     'bi', 'samudera_bi'),
    'it':           (['it systems', 'it system', 'application', 'applications',
                      'sap', 'erp', 'legacy', 'software'],
                     'it', 'samudera_erp'),
}

# domains that are known to be missing corporate data pre-join, from the
# `unknown_data_sources` list in credentials_status.json
KNOWN_UNAVAILABLE = {
    'fleet':       'fleet/ops/shipping data (vessel tracking, terminals)',
    'ops':         'fleet/ops/shipping data (vessel tracking, terminals)',
    'finance':     'budget and finance data (transformation program budget)',
    'hr':          'HR/people data',
    'procurement': 'procurement/vendor data',
    'customer':    'business KPIs and current-state metrics',
    'kpi':         'business KPIs and current-state metrics',
    'bi':          'BI/analytics platform access',
    'it':          'IT/application estate data',
}


def now_wib():
    return datetime.now(WIB).isoformat(timespec='seconds')


def _read_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return None


def data_drop_dir(workspace=DEFAULT_WORKSPACE):
    return WS_DIR / workspace / 'data'


def credentials_path(workspace=DEFAULT_WORKSPACE):
    return WS_DIR / workspace / 'credentials_status.json'


def _registry(workspace=DEFAULT_WORKSPACE):
    return _read_json(credentials_path(workspace)) or {}


def _scan_data_files(workspace=DEFAULT_WORKSPACE):
    """Real files the owner has dropped into the workspace data folder."""
    ddir = data_drop_dir(workspace)
    files = []
    if ddir.exists():
        for p in sorted(ddir.rglob('*')):
            if p.is_file() and p.name.lower() != 'readme.md':
                try:
                    st = p.stat()
                    files.append({
                        'path': str(p),
                        'name': p.name,
                        'size': st.st_size,
                        'modified': datetime.fromtimestamp(st.st_mtime, WIB)
                        .isoformat(timespec='seconds'),
                    })
                except Exception:
                    continue
    return files


def load(workspace=DEFAULT_WORKSPACE):
    """Build the availability dict for a workspace.

    Returns:
      {
        workspace, join_date, join_date_meaning,
        sources: {source_key: {status, platform, setup_date, read_only, ...}},
        available_now: [source_keys usable today],
        post_join:     [source_keys awaiting owner provisioning],
        data_drop_files: [real files found in the data drop folder],
        unknown_data_sources: [domains with no source at all],
      }
    """
    reg = _registry(workspace)
    status = reg.get('status') or {}
    sources = {}
    available_now = []
    post_join = []
    for key, info in status.items():
        st = info.get('status')
        entry = {
            'status': st,
            'platform': info.get('platform', 'n/a'),
            'read_only': info.get('read_only', True),
            'setup_date': info.get('setup_date', ''),
        }
        sources[key] = entry
        if st == 'configured_working':
            available_now.append(key)
        elif st == 'post_join':
            post_join.append(key)
    return {
        'workspace': workspace,
        'join_date': reg.get('join_date', JOIN_DATE),
        'join_date_meaning': reg.get('join_date_meaning', JOIN_DATE_MEANING),
        'rule': reg.get('rule', ''),
        'sources': sources,
        'available_now': available_now,
        'post_join': post_join,
        'data_drop_files': _scan_data_files(workspace),
        'unknown_data_sources': reg.get('unknown_data_sources', []),
        'generated_wib': now_wib(),
    }


def metric_domain(question):
    """Heuristically map a data question to a domain key, or None."""
    low = (question or '').lower()
    for key, (kws, _, _) in DOMAIN_KEYWORDS.items():
        if any(k in low for k in kws):
            return key
    return None


def source_for_domain(domain):
    for key, (_, dom, source) in DOMAIN_KEYWORDS.items():
        if dom == domain:
            return source
    return None


def resolve(question, workspace=DEFAULT_WORKSPACE):
    """Resolve a data question to its availability. Never guesses numbers.

    Returns:
      {question, domain, source, status, files, reason, expected}
    where `status` is 'available', 'unavailable_post_join', or 'unknown'.
    """
    domain = metric_domain(question)
    source = source_for_domain(domain) if domain else None
    av = load(workspace)
    files = av['data_drop_files']

    if domain is None:
        return {
            'question': question, 'domain': None, 'source': None,
            'status': 'unknown',
            'files': [],
            'reason': ('Could not determine which Samudera data domain this '
                       'question needs (fleet/ops, finance, HR, procurement, '
                       'KPIs, BI, ...).'),
            'expected': None,
        }

    # 1. owner already dropped a matching export -> genuinely available
    matching = [f for f in files if _file_matches_domain(f['name'], domain)]
    if matching:
        return {
            'question': question, 'domain': domain,
            'source': source or 'data_drop', 'status': 'available',
            'files': matching,
            'reason': ('Owner-provided export file(s) found in the data drop '
                       'folder.'),
            'expected': None,
        }

    # 2. a configured_working source covers it (only document/news sources
    #    exist pre-join; no metric domains are configured)
    if source and source in av['available_now']:
        return {
            'question': question, 'domain': domain, 'source': source,
            'status': 'available', 'files': [],
            'reason': ('Source %s is marked configured_working in '
                       'credentials_status.json.' % source),
            'expected': None,
        }

    # 3. post-join source not yet provisioned -> graceful unavailable
    if source:
        reason = ('Source %s is marked post_join (platform: %s) - not '
                  'provisioned yet. Samudera corporate data is not assumed '
                  'before %s.'
                  % (source, av['sources'].get(source, {}).get('platform', 'n/a'),
                     av['join_date']))
    else:
        reason = ('No corporate source is provisioned for domain "%s".'
                  % domain)
    return {
        'question': question, 'domain': domain, 'source': source,
        'status': 'unavailable_post_join',
        'files': [],
        'reason': reason,
        'expected': av['join_date'],
    }


def _file_matches_domain(name, domain):
    low = name.lower()
    for key, (kws, dom, _) in DOMAIN_KEYWORDS.items():
        if dom != domain:
            continue
        if any(k in low for k in kws):
            return True
    # fall back to the domain name itself appearing in the filename
    return domain in low


def summary(workspace=DEFAULT_WORKSPACE):
    """Human-readable availability report for the CLI/chat."""
    av = load(workspace)
    lines = []
    lines.append('Data availability for workspace "%s" (generated %s)'
                 % (workspace, av['generated_wib']))
    lines.append('Join date: %s - %s' % (av['join_date'], av['join_date_meaning']))
    lines.append('')
    lines.append('Available now:')
    if av['available_now']:
        for k in av['available_now']:
            lines.append('  [x] %s (configured_working)' % k)
    else:
        lines.append('  (none)')
    lines.append('')
    lines.append('Post-join (awaiting owner provisioning):')
    for k in av['post_join']:
        info = av['sources'][k]
        lines.append('  [!] %s (platform: %s; setup: %s)'
                     % (k, info['platform'], info['setup_date'] or av['join_date']))
    files = av['data_drop_files']
    lines.append('')
    lines.append('Owner-provided data files (data drop folder):')
    if files:
        for f in files:
            lines.append('  [x] %s (%d bytes, modified %s)'
                         % (f['path'], f['size'], f['modified']))
    else:
        lines.append('  (none yet - drop CSVs/exports in %s after joining)'
                     % data_drop_dir(workspace))
    lines.append('')
    lines.append('Data with NO source until provided:')
    for u in av['unknown_data_sources']:
        lines.append('  [?] %s' % u)
    lines.append('')
    lines.append('Rule: %s' % av['rule'])
    return '\n'.join(lines)
