#!/usr/bin/env python3
"""transformation_strategy.py - Transformation Strategy specialist (Phase 3).

Strategic framing layer for the Samudera Digital Transformation role. Reuses
transformation-research for ALL evidence gathering (news briefings, meeting
archive, knowledge store) - never duplicates scanning/briefing.

Commands:
  python3 transformation_strategy.py framework
      Deterministic: print decision framework + delegation matrix + evidence taxonomy.
  python3 transformation_strategy.py delegate --need "<keyword...>"
      Deterministic: map a need phrase to the specialist to engage.
  python3 transformation_strategy.py sources --workspace samudera
  python3 transformation_strategy.py scan --workspace samudera --query "..."
  python3 transformation_strategy.py brief --workspace samudera
      Delegated to transformation-research (subprocess).
  python3 transformation_strategy.py synthesize --workspace samudera --topic "..."
      Strategy synthesis grounded in delegated evidence, framed by the decision
      framework. OpenAI tier medium, escalating to high when complexity >= 7.
      Falls back to deterministic framework output when synthesis is unavailable.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
if sys.stderr.encoding != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8')

BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent
sys.path.insert(0, str(BASE_DIR / '.agent' / 'scripts'))

RESEARCH_CLI = (BASE_DIR / '.agent' / 'skills' / 'transformation-research'
                / 'scripts' / 'transformation_research.py')

JOIN_DATE = '2026-08-18'

DELEGATION = [
    ('tasks|commitments|deadlines|owners|due|overdue|action items',
     '📋 Executive PM', 'task state, due/overdue, commitments, owners'),
    ('process|mapping|redesign|harmoniz|standardiz|workflow',
     '🔄 Process Excellence', 'current-state flows, target process design, operating-model KPIs'),
    ('kpi|metric|validat|quantitative|data availability|numbers',
     '📊 Data/BI', 'data availability, numbers, validation (read-only)'),
    ('erp|system|api|integration|architecture|landscape|data flow',
     '🔗 Enterprise Integration', 'systems landscape, integration contracts, post-join sequencing'),
    ('policy|standard|control|governance|documentation',
     '🏛️ Governance & Standards', 'policy framework, controls, documentation standards'),
    ('cost|roi|tco|payback|scenario|sensitivity|financial',
     '💰 Business Case', 'financial modeling and sensitivity'),
    ('risk|control|security|compliance|regulatory|audit',
     '🛡️ Risk / Audit / Security', 'risk register, controls, security and regulatory review'),
    ('recommend|decision|tradeoff|judgment|final',
     '👔 Executive Advisor', 'judgment, tradeoffs, recommendation framing'),
    ('proposal|decision memo|presentation|talking points|exec update',
     '📝 Executive Communication', 'executive-facing deliverables'),
    ('research|trend|benchmark|external|evidence|market|technology',
     '🔎 transformation-research', 'evidence gathering, source and gap reporting'),
]

FRAMEWORK = [
    ('1', 'Problem', 'the issue, stated concretely'),
    ('2', 'Current State', 'grounded in internal evidence only; say explicitly when no internal evidence exists'),
    ('3', 'Root Cause', 'why the problem exists today (state as inference when not proven)'),
    ('4', 'Strategic Objective', 'which long-term / holding-company objective it serves'),
    ('5', 'Transformation Opportunity', 'the change that addresses the problem'),
    ('6', 'Options', 'at least two real alternatives with trade-offs'),
    ('7', 'Recommended Direction', 'the chosen option and the rationale'),
    ('8', 'Required Capabilities', 'people, skills, systems, data needed'),
    ('9', 'Dependencies', 'what must be true first (people, systems, data, timing)'),
    ('10', 'Roadmap', 'phased sequence with rough owners (align owners via Executive PM)'),
    ('11', 'KPI', 'measurable outcomes aligned to business objectives (validate via Data/BI)'),
    ('12', 'Cost / Benefit', 'order-of-magnitude; hand precision to Business Case'),
    ('13', 'Risk', 'top risks and mitigations (validate via Risk / Audit / Security)'),
    ('14', 'Executive Decision', 'the ask: what decision is needed, from whom, by when'),
]

EVIDENCE_TYPES = [
    ('facts', 'direct observed/verified statements'),
    ('internal Samudera evidence', 'meeting archive, knowledge store, or data drop (cite the source)'),
    ('external verified research', 'from a named external source gathered via transformation-research'),
    ('inference', 'derived from evidence; show the reasoning chain'),
    ('recommendation', "this agent's judgment, clearly labeled as such"),
    ('assumptions', 'stated explicitly, never silent'),
    ('missing information', 'what is unknown; the exact data/person/team that should provide it'),
]


def _run_research(args):
    """Reuse transformation-research - the single evidence engine."""
    cmd = [sys.executable, str(RESEARCH_CLI)] + args
    proc = subprocess.run(cmd, capture_output=True, text=True)
    out = (proc.stdout or '').strip() or (proc.stderr or '').strip()
    return out, proc.returncode


def cmd_framework(_args):
    lines = ['Transformation Strategy - decision framework (workspace samudera)',
             'JOIN_DATE: Samudera corporate data assumed only on/after %s' % JOIN_DATE,
             '']
    lines.append('## Decision framework')
    for num, title, what in FRAMEWORK:
        lines.append('%s. %s - %s' % (num, title, what))
    lines.append('')
    lines.append('## Delegation matrix')
    for terms, specialist, owns in DELEGATION:
        lines.append('- %-34s %-24s %s' % (specialist, '', owns))
    lines.append('')
    lines.append('## Evidence taxonomy')
    for label, meaning in EVIDENCE_TYPES:
        lines.append('- %s: %s' % (label, meaning))
    print('\n'.join(lines))


def cmd_delegate(args):
    need = (args.need or '').lower()
    if not need:
        print('delegate --need "<what you need>"')
        sys.exit(1)
    for terms, specialist, owns in DELEGATION:
        if any(t in need for t in terms.split('|')):
            print('engage: %s' % specialist)
            print('owns:   %s' % owns)
            return
    print('no specialist matched - ask a clarifying question, or engage 🎯 Orchestrator to route.')


def cmd_sources(args):
    out, _ = _run_research(['sources', '--workspace', args.workspace or 'samudera'])
    print(out)


def cmd_scan(args):
    out, _ = _run_research(['scan', '--workspace', args.workspace or 'samudera',
                            '--query', args.query])
    print(out)


def cmd_brief(args):
    out, _ = _run_research(['brief', '--workspace', args.workspace or 'samudera'])
    print(out)


def cmd_synthesize(args):
    ws = args.workspace or 'samudera'
    topic = (args.topic or '').strip()
    if not topic:
        print('synthesize --topic "<strategy topic>"')
        sys.exit(1)
    evidence, _ = _run_research(['scan', '--workspace', ws, '--query', topic])
    complexity = 6 if evidence and 'no matching' not in evidence else 4
    tier = 'high' if complexity >= 7 else 'medium'
    system = (
        'You are the Transformation Strategy specialist for the "%s" workspace '
        '(Samudera Indonesia, Head of Digital Transformation). Frame the topic '
        'using the 14-step decision framework (Problem, Current State, Root Cause, '
        'Strategic Objective, Transformation Opportunity, Options, Recommended '
        'Direction, Required Capabilities, Dependencies, Roadmap, KPI, '
        'Cost/Benefit, Risk, Executive Decision). Ground every claim ONLY in the '
        'provided evidence. Distinguish facts / internal Samudera evidence / '
        'external verified research / inference / recommendation / assumptions / '
        'missing information, tagging claims accordingly. Samudera corporate data '
        'is only assumed on/after %s. If required internal information is missing, '
        'state explicitly what is missing and which data/person/team should '
        'provide it. If the request would be ambiguous, list the clarifying '
        'questions instead of inventing assumptions. Never fabricate.' % (ws, JOIN_DATE))
    bundle = json.dumps({'topic': topic, 'evidence': evidence,
                         'decision_framework': [f[1] for f in FRAMEWORK],
                         'delegation': [(s, o) for _, s, o in DELEGATION]},
                        ensure_ascii=False, indent=2)
    try:
        import openai_call  # noqa: WPS433 (local import keeps CLI lightweight)
        ok, text, _meta = openai_call.call(bundle, system=system, tier=tier,
                                           max_tokens=2400, timeout=180)
        if ok and text and text.strip():
            print(text)
            return
    except Exception:
        pass
    print('(AI synthesis unavailable; deterministic framework follows)')
    cmd_framework(args)


def main():
    p = argparse.ArgumentParser(description='Transformation Strategy (Phase 3)')
    sub = p.add_subparsers(dest='cmd')
    sub.add_parser('framework')
    d = sub.add_parser('delegate')
    d.add_argument('--need', required=True)
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
    if args.cmd == 'framework':
        cmd_framework(args)
    elif args.cmd == 'delegate':
        cmd_delegate(args)
    elif args.cmd == 'sources':
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
