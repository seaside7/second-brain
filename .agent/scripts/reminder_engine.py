#!/usr/bin/env python3
"""reminder_engine.py — Natural-language reminders with date parsing.

Turns notes like "remind me i have meeting tomorrow at 3 for savvy" into
structured reminders with real due dates. Store lives in the PERSONAL
workspace only (reminders never cross into the office-safe view).

Usage:
  python3 reminder_engine.py add --text "meeting tomorrow at 3 for savvy"
  python3 reminder_engine.py add --text "pay Vandi" --due 2026-08-25T14:00
  python3 reminder_engine.py list [--scope today|upcoming|overdue|done|all]
  python3 reminder_engine.py close --id r-xxx
  python3 reminder_engine.py reopen --id r-xxx
  python3 reminder_engine.py delete --id r-xxx

parse_due() understands (WIB, UTC+7):
  today/tonight · tomorrow · next monday..sun · bare weekday ·
  in N minutes/hours/days · 25 aug [2026] · aug 25 · dd/mm · dd-mm ·
  times: at 3 / 15:00 / 3pm / 9 am / noon / midnight / pagi·siang·sore·malam
Bare hour <= 7 (no am/pm) is read as PM — nobody sets 3am reminders.
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
if sys.stderr.encoding != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8')

BASE_DIR = Path(__file__).resolve().parent.parent.parent
STATE_PATH = BASE_DIR / '.agent' / 'workspaces' / 'personal' / 'state' / 'reminders.json'

WIB = timezone(timedelta(hours=7))

WEEKDAYS = {'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
            'friday': 4, 'saturday': 5, 'sunday': 6,
            'senin': 0, 'selasa': 1, 'rabu': 2, 'kamis': 3, 'jumat': 4,
            'sabtu': 5, 'minggu': 6}
MONTHS = {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
          'jul': 7, 'aug': 8, 'agu': 8, 'sep': 9, 'oct': 10, 'okt': 10,
          'nov': 11, 'dec': 12, 'des': 12}

# Indonesian time-of-day words -> default hour (24h)
INDO_TIME_WORDS = {'pagi': 8, 'siang': 12, 'sore': 16, 'malam': 19}


def parse_due(text, now=None):
    """Extract a due datetime from natural text. Returns ISO string
    (YYYY-MM-DDTHH:MM) or None when no date/time is present."""
    now = now or datetime.now(WIB)
    t = text.lower()
    t = re.sub(r'remind(?:er)?\s+(?:me\s+)?(?:to\s+|about\s+|that\s+|for\s+)?', '', t)

    day = None
    has_explicit_day = False

    # ── explicit dates first (they win over relative words) ──
    m = re.search(r'\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b', t)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        yr = int(m.group(3)) if m.group(3) else now.year
        if yr < 100:
            yr += 2000
        try:
            day = day_at(now, yr, mo, d)
            has_explicit_day = True
        except ValueError:
            pass

    if not has_explicit_day:
        # "25 august" / "august 25" (+ optional year)
        m = re.search(r'\b(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|agu|sep|oct|okt|nov|dec|des)[a-z]*\.?(?:\s+(\d{4}))?\b', t)
        if not m:
            m = re.search(r'\b(jan|feb|mar|apr|may|jun|jul|aug|agu|sep|oct|okt|nov|dec|des)[a-z]*\.?\s+(\d{1,2})(?:,?\s+(\d{4}))?\b', t)
        if m:
            g = m.groups()
            if g[0].isdigit():
                d, mon = int(g[0]), MONTHS[g[1][:3]]
                yr = int(g[2]) if len(g) > 2 and g[2] else now.year
            else:
                mon, d = MONTHS[g[0][:3]], int(g[1])
                yr = g[2] if len(g) > 2 and g[2] else now.year
            try:
                day = day_at(now, yr, mon, d)
                has_explicit_day = True
            except ValueError:
                pass

    if not has_explicit_day:
        # "in N minutes/hours/days"
        m = re.search(r'\bin\s+(\d+)\s*(minute|min|menit|hour|jam|day|hari)s?\b', t)
        if m:
            n, unit = int(m.group(1)), m.group(2)
            if unit.startswith(('min', 'menit')):
                due = now + timedelta(minutes=n)
            elif unit in ('hour', 'jam'):
                due = now + timedelta(hours=n)
            else:
                due = now + timedelta(days=n)
            return fmt(due)

    if not has_explicit_day:
        if re.search(r'\btonight\b|\bmalam ini\b', t):
            day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            has_explicit_day = True
        elif re.search(r'\btomorrow\b|\bbesok\b', t):
            day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            has_explicit_day = True
        elif re.search(r'\btoday\b|\bhari ini\b', t):
            day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            has_explicit_day = True

    if not has_explicit_day:
        # "next monday" / "next senin" — strictly after this week
        m = re.search(r'\bnext\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday|senin|selasa|rabu|kamis|jumat|sabtu|minggu)\b', t)
        if m:
            wd = WEEKDAYS[m.group(1)]
            delta = (wd - now.weekday()) % 7
            if delta == 0:
                delta = 7
            day = (now + timedelta(days=delta)).replace(hour=0, minute=0, second=0, microsecond=0)
            has_explicit_day = True

    if not has_explicit_day:
        # bare weekday — upcoming occurrence (today counts)
        m = re.search(r'\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|senin|selasa|rabu|kamis|jumat|sabtu|minggu)\b', t)
        if m:
            wd = WEEKDAYS[m.group(1)]
            delta = (wd - now.weekday()) % 7
            day = (now + timedelta(days=delta)).replace(hour=0, minute=0, second=0, microsecond=0)
            has_explicit_day = True

    # ── time of day ── (each pattern has its own named groups — never share)
    hour = minute = None
    ap = ''
    m = re.search(r'\bat\s+(?P<h>\d{1,2})(?:[:.](?P<min>\d{2}))?\s*(?P<ap>am|pm)?\b', t)
    if m:
        hour, ap = int(m.group('h')), (m.group('ap') or '')
        minute = int(m.group('min') or 0)
    if hour is None:
        m = re.search(r'\b(?P<h>\d{1,2})[:.](?P<min>\d{2})\s*(?P<ap>am|pm)?\b', t)
        if m:
            hour, ap = int(m.group('h')), (m.group('ap') or '')
            minute = int(m.group('min'))
    if hour is None:
        m = re.search(r'\b(?P<h>\d{1,2})\s*(?P<ap>am|pm)\b', t)
        if m:
            hour, ap, minute = int(m.group('h')), m.group('ap'), 0
    if hour is None:
        # "tonight 8" / "malam jam 9" — bare hour right after an evening word
        m = re.search(r'\b(?:tonight|tonite|malam)(?:\s+jam)?[^\d]{0,12}(?P<h>\d{1,2})\b', t)
        if m:
            hour, minute = int(m.group('h')), 0

    evening_ctx = bool(re.search(r'\btonight\b|\btonite\b|\bmalam\b|\bsore\b', t))
    if hour is not None:
        if ap == 'pm' and hour < 12:
            hour += 12
        elif ap == 'am' and hour == 12:
            hour = 0
        elif not ap:
            if evening_ctx and hour <= 11:
                hour += 12          # "tonight 8" -> 20:00
            elif hour <= 7:
                hour += 12          # "at 3" means afternoon, not dawn
    else:
        for word, h in INDO_TIME_WORDS.items():
            if re.search(r'\b%s\b' % word, t):
                hour, minute = h, 0
                break
        else:
            if re.search(r'\bnoon\b|\btengah hari\b', t):
                hour, minute = 12, 0

    base = day if has_explicit_day else now.replace(second=0, microsecond=0)
    if hour is not None:
        base = base.replace(hour=min(hour, 23), minute=min(minute or 0, 59))

    return fmt(base)


def day_at(now, year, month, day):
    return datetime(year, month, day, tzinfo=WIB)


def fmt(dt):
    return dt.strftime('%Y-%m-%dT%H:%M')


# ── store ──────────────────────────────────────────────────────────────────

def _load():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {'reminders': []}


def _save(data):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(STATE_PATH) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, str(STATE_PATH))


def add(text, due=None, source=''):
    data = _load()
    rid = 'r-' + format(int(time.time() * 1000), 'x')
    entry = {
        'id': rid,
        'text': (text or '').strip(),
        'due': due or parse_due(text),
        'created_wib': datetime.now(WIB).isoformat(timespec='seconds'),
        'source': source or 'manual',
        'done': False,
        'done_wib': None,
    }
    data['reminders'].append(entry)
    _save(data)
    return entry


def list_reminders(scope='all'):
    # stored dues are naive WIB wall-clock strings; compare against naive now
    now = datetime.now(WIB).replace(tzinfo=None)
    items = sorted(_load()['reminders'], key=lambda r: (r.get('due') or '9999'))
    out = []
    for r in items:
        due_dt = None
        if r.get('due'):
            try:
                due_dt = datetime.fromisoformat(r['due'])
            except ValueError:
                pass
        bucket = 'upcoming'
        if r.get('done'):
            bucket = 'done'
        elif due_dt and due_dt < now:
            bucket = 'overdue'
        elif due_dt and due_dt.date() == now.date():
            bucket = 'today'
        r = dict(r)
        r['bucket'] = bucket
        if scope in ('all', bucket) or (scope == 'today' and bucket == 'overdue'):
            out.append(r)
    if scope == 'today':
        out.sort(key=lambda r: r.get('due') or '9999')
    return out


def set_done(rid, done):
    data = _load()
    for r in data['reminders']:
        if r['id'] == rid:
            r['done'] = bool(done)
            r['done_wib'] = datetime.now(WIB).isoformat(timespec='seconds') if done else None
            _save(data)
            return r
    raise KeyError(rid)


def delete(rid):
    data = _load()
    before = len(data['reminders'])
    data['reminders'] = [r for r in data['reminders'] if r['id'] != rid]
    if len(data['reminders']) == before:
        raise KeyError(rid)
    _save(data)
    return {'deleted': rid}


# ── CLI ────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description='Reminder engine')
    sub = p.add_subparsers(dest='cmd')

    a = sub.add_parser('add')
    a.add_argument('--text', required=True)
    a.add_argument('--due', default=None)
    a.add_argument('--source', default='manual')

    l = sub.add_parser('list')
    l.add_argument('--scope', default='all',
                   choices=['today', 'upcoming', 'overdue', 'done', 'all'])

    for name in ('close', 'reopen', 'delete'):
        s = sub.add_parser(name)
        s.add_argument('--id', required=True)

    args = p.parse_args()
    if args.cmd == 'add':
        result = add(args.text, due=args.due, source=args.source)
    elif args.cmd == 'list':
        result = {'scope': args.scope, 'reminders': list_reminders(args.scope)}
    elif args.cmd == 'close':
        result = set_done(args.id, True)
    elif args.cmd == 'reopen':
        result = set_done(args.id, False)
    elif args.cmd == 'delete':
        result = delete(args.id)
    else:
        p.print_help()
        return
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
