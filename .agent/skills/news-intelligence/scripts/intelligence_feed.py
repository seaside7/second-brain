#!/usr/bin/env python3
"""intelligence_feed.py - Daily Intelligence Feed generator.

Fetches news from RSS feeds for 3 categories, uses LLM to generate
Bloomberg-quality briefings with structured format.

Categories:
  1. Global Economic Update (US, China, Europe, Asia, commodities, geopolitics)
  2. AI & Technology Update (AI agents, models, enterprise AI, tools)
  3. Crypto Update (BTC/ETH, ETF flows, regulation, macro connections)

Usage:
  python3 intelligence_feed.py generate [--dry-run]
  python3 intelligence_feed.py latest
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
if sys.stderr.encoding != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8')

SKILL_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SKILL_DIR.parent.parent.parent
FEEDS_DIR = REPO_ROOT / 'journal' / 'news_briefings'
FEEDS_DIR.mkdir(parents=True, exist_ok=True)
STATE_PATH = REPO_ROOT / 'journal' / 'state' / 'intelligence_feed_log.json'
STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

WIB = timezone(timedelta(hours=7))

RSS_FEEDS = {
    'global_economy': [
        'https://feeds.reuters.com/reuters/businessNews',
        'https://feeds.reuters.com/reuters/topNews',
        'https://www.cnbc.com/id/100003114/device/rss/rss.html',
        'https://www.cnbc.com/id/10001147/device/rss/rss.html',
        'https://www.aljazeera.com/xml/rss/all.xml',
    ],
    'ai_tech': [
        'https://techcrunch.com/category/artificial-intelligence/feed/',
        'https://www.theverge.com/rss/ai-artificial-intelligence/index.xml',
        'https://arstechnica.com/feed/',
        'https://www.wired.com/feed/rss',
    ],
    'crypto': [
        'https://cointelegraph.com/rss',
        'https://www.coindesk.com/arc/outboundfeeds/rss/',
        'https://cryptonews.com/news/feed/',
    ],
}

CATEGORY_META = {
    'global_economy': {
        'icon': '\U0001f30e',
        'label': 'Global Economic Update',
        'format': 'NEWS -> WHY IT MATTERS -> IMPACT ON INDONESIA -> WHAT TO WATCH NEXT -> MY TAKE',
    },
    'ai_tech': {
        'icon': '\U0001f916',
        'label': 'AI & Technology Update',
        'format': 'NEWS -> WHY IT MATTERS -> HOW IT CAN SUPPORT MY WORK -> WHAT TO WATCH NEXT -> MY TAKE',
    },
    'crypto': {
        'icon': '\u20bf',
        'label': 'Crypto Update',
        'format': 'MARKET -> NEWS -> WHY IT MATTERS -> WHAT TO WATCH NEXT -> MY TAKE',
    },
}


def _load_json(path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None


def _save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, str(path))


def _fetch_rss(url, timeout=15):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; PSB/1.0)'})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode('utf-8', errors='replace')
        items = []
        for match in re.finditer(r'<item>(.*?)</item>', raw, re.S):
            item_raw = match.group(1)
            title = re.search(r'<title[^>]*>(.*?)</title>', item_raw, re.S)
            link = re.search(r'<link[^>]*>(.*?)</link>', item_raw, re.S)
            desc = re.search(r'<description[^>]*>(.*?)</description>', item_raw, re.S)
            pub = re.search(r'<pubDate[^>]*>(.*?)</pubDate>', item_raw, re.S)
            if title:
                items.append({
                    'title': re.sub(r'<[^>]+>', '', title.group(1)).strip(),
                    'url': (link.group(1).strip() if link else ''),
                    'summary': re.sub(r'<[^>]+>', '', desc.group(1)).strip()[:500] if desc else '',
                    'published': pub.group(1).strip() if pub else '',
                })
        return items
    except Exception as e:
        print('  [WARN] RSS fetch failed: %s: %s' % (url, e), file=sys.stderr)
        return []


def _fetch_category(category, max_items=20):
    feeds = RSS_FEEDS.get(category, [])
    all_items = []
    seen = set()
    for url in feeds:
        items = _fetch_rss(url)
        for item in items:
            key = item['title'].lower()[:60]
            if key not in seen:
                seen.add(key)
                all_items.append(item)
        time.sleep(0.3)
    all_items.sort(key=lambda x: x.get('published', ''), reverse=True)
    return all_items[:max_items]


def _openai_key():
    env = REPO_ROOT / '.env'
    if env.exists():
        with open(env, 'r', encoding='utf-8') as fh:
            for line in fh:
                if line.strip().startswith('OPENAI_API_KEY='):
                    return line.strip().split('=', 1)[1].strip()
    return os.environ.get('OPENAI_API_KEY')


def _llm_call(prompt, system='', max_tokens=3000):
    api_key = _openai_key()
    if not api_key:
        return None
    messages = []
    if system:
        messages.append({'role': 'system', 'content': system})
    messages.append({'role': 'user', 'content': prompt})
    payload = json.dumps({
        'model': 'gpt-4o-mini',
        'messages': messages,
        'max_tokens': max_tokens,
        'temperature': 0.3,
    }).encode('utf-8')
    req = urllib.request.Request(
        'https://api.openai.com/v1/chat/completions',
        data=payload,
        headers={
            'Content-Type': 'application/json',
            'Authorization': 'Bearer %s' % api_key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        return data['choices'][0]['message']['content']
    except Exception as e:
        print('  [ERROR] LLM call failed: %s' % e, file=sys.stderr)
        return None


SYSTEM_PROMPTS = {
    'global_economy': (
        'You are a senior financial analyst writing a daily economic briefing for '
        'a Head of Digital Transformation at an Indonesian shipping/logistics company. '
        'Write in simple, clear English with occasional Indonesian context.\n\n'
        ' RULES:\n'
        '- Quality like Bloomberg/Reuters but simple language\n'
        '- Only include materially important news\n'
        '- Include specific numbers/data when useful\n'
        '- Explain the "so what?" behind every story\n'
        '- Consider impact on Indonesia specifically\n'
        '- If nothing important, say "Nothing major today"\n'
        '- Do NOT use sensational headlines\n'
        '- Distinguish FACT from ANALYSIS\n'
        '- Target read time: 2-3 minutes\n\n'
        'FORMAT (for each story):\n'
        '**[Headline]**\n'
        'NEWS: What happened (1-2 sentences)\n'
        'WHY IT MATTERS: The significance\n'
        'IMPACT ON INDONESIA: How it affects Indonesia/business\n'
        'WHAT TO WATCH NEXT: Key developments to monitor\n'
        'MY TAKE: Brief analyst perspective\n\n'
        'Return ONLY valid JSON array. Each item:\n'
        '{"headline": "...", "news": "...", "why_it_matters": "...", '
        '"impact_on_indonesia": "...", "what_to_watch": "...", "my_take": "...", '
        '"importance": 1-10, "source": "...", "url": "..."}\n\n'
        'Write 3-5 stories maximum. If nothing major, return empty array [].'
    ),
    'ai_tech': (
        'You are a senior tech analyst writing a daily AI/tech briefing for '
        'a Head of Digital Transformation at an Indonesian company. '
        'Focus on practical, actionable intelligence.\n\n'
        'RULES:\n'
        '- Only include news that could affect work or business\n'
        '- For each story, give a clear verdict: TRY NOW / MONITOR / IGNORE\n'
        '- Focus on: AI agents, new models, enterprise AI, coding AI, tools\n'
        '- Consider: How can this support digital transformation?\n'
        '- If nothing important, say "Nothing major today"\n'
        '- Target read time: 2-3 minutes\n\n'
        'FORMAT (for each story):\n'
        '**[Headline]**\n'
        'NEWS: What happened (1-2 sentences)\n'
        'WHY IT MATTERS: The significance for business/tech\n'
        'HOW IT CAN SUPPORT MY WORK: Practical application\n'
        'WHAT TO WATCH NEXT: Key developments to monitor\n'
        'MY TAKE: Verdict (TRY NOW / MONITOR / IGNORE) + brief reasoning\n\n'
        'Return ONLY valid JSON array. Each item:\n'
        '{"headline": "...", "news": "...", "why_it_matters": "...", '
        '"how_it_supports_work": "...", "what_to_watch": "...", "my_take": "...", '
        '"verdict": "TRY NOW|MONITOR|IGNORE", "importance": 1-10, '
        '"source": "...", "url": "..."}\n\n'
        'Write 3-5 stories maximum. If nothing major, return empty array [].'
    ),
    'crypto': (
        'You are a crypto market analyst writing a daily briefing for '
        'a professional investor. Focus on Bitcoin and major crypto developments.\n\n'
        'RULES:\n'
        '- Include BTC/ETH price movement and market sentiment\n'
        '- Focus on ETF/institutional flows, regulation, major events\n'
        '- Connect macro (Fed/liquidity) to crypto\n'
        '- If nothing important, say "Nothing major today"\n'
        '- Do NOT use crypto hype language\n'
        '- Target read time: 2 minutes\n\n'
        'FORMAT (for each story):\n'
        '**[Headline]**\n'
        'MARKET: Price/sentiment data\n'
        'NEWS: What happened (1-2 sentences)\n'
        'WHY IT MATTERS: The significance\n'
        'WHAT TO WATCH NEXT: Key developments to monitor\n'
        'MY TAKE: Brief analyst perspective\n\n'
        'Return ONLY valid JSON array. Each item:\n'
        '{"headline": "...", "market_data": "...", "news": "...", '
        '"why_it_matters": "...", "what_to_watch": "...", "my_take": "...", '
        '"importance": 1-10, "source": "...", "url": "..."}\n\n'
        'Write 3-5 stories maximum. If nothing major, return empty array [].'
    ),
}


def _generate_category(category, items):
    meta = CATEGORY_META[category]
    system = SYSTEM_PROMPTS[category]

    headlines = []
    for i, item in enumerate(items[:15], 1):
        h = '%d. %s' % (i, item['title'])
        if item.get('summary'):
            h += '\n   Summary: %s' % item['summary'][:200]
        if item.get('url'):
            h += '\n   URL: %s' % item['url']
        headlines.append(h)

    prompt = ('Here are today\'s news headlines for the %s category:\n\n'
              '%s\n\n'
              'Generate the briefing in the specified JSON format. '
              'Pick only the 3-5 most important stories. '
              'Quality over quantity.') % (meta['label'], '\n'.join(headlines))

    raw = _llm_call(prompt, system=system)
    if not raw:
        return []

    try:
        raw = raw.strip()
        if raw.startswith('```'):
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            raw = re.sub(r'\s*```$', '', raw)
        stories = json.loads(raw)
        if isinstance(stories, list):
            return stories
    except (json.JSONDecodeError, TypeError):
        pass

    return []


def generate():
    today = datetime.now(WIB).strftime('%Y-%m-%d')
    now_wib = datetime.now(WIB).isoformat(timespec='seconds')

    print('[%s] Intelligence Feed - Generating daily briefing...' % now_wib)
    result = {
        'date': today,
        'generated_wib': now_wib,
        'categories': {},
    }

    for cat in ('global_economy', 'ai_tech', 'crypto'):
        meta = CATEGORY_META[cat]
        print('  Fetching %s...' % meta['label'])
        items = _fetch_category(cat)
        print('    Got %d items' % len(items))

        if items:
            print('  Generating %s briefing...' % meta['label'])
            stories = _generate_category(cat, items)
            print('    %d stories selected' % len(stories))
        else:
            stories = []

        result['categories'][cat] = {
            'label': meta['label'],
            'icon': meta['icon'],
            'stories': stories,
            'stories_count': len(stories),
            'fetched_items': len(items),
        }

    # Save to file
    filename = '%s_intelligence.json' % today
    out_path = FEEDS_DIR / filename
    _save_json(out_path, result)
    print('\nSaved: %s' % out_path)

    # Update state log
    state = _load_json(STATE_PATH) or {'feeds': []}
    state['feeds'].append({
        'date': today,
        'generated_wib': now_wib,
        'file': filename,
        'categories': {k: v['stories_count'] for k, v in result['categories'].items()},
    })
    state['feeds'] = state['feeds'][-30:]
    _save_json(STATE_PATH, state)

    return result


def latest():
    state = _load_json(STATE_PATH) or {'feeds': []}
    if not state['feeds']:
        return None
    latest_entry = state['feeds'][-1]
    path = FEEDS_DIR / latest_entry['file']
    return _load_json(path)


def cmd_generate(args):
    if args.dry_run:
        print('[DRY RUN] Would fetch RSS feeds and generate briefing')
        for cat, meta in CATEGORY_META.items():
            items = _fetch_category(cat)
            print('  %s: %d items fetched' % (meta['label'], len(items)))
        return
    result = generate()
    total = sum(c['stories_count'] for c in result['categories'].values())
    print('\nTotal stories: %d' % total)
    for cat, data in result['categories'].items():
        meta = CATEGORY_META[cat]
        print('  %s %s: %d stories' % (meta['icon'], meta['label'], data['stories_count']))


def cmd_latest(args):
    data = latest()
    if not data:
        print('No intelligence feed found')
        return
    print(json.dumps(data, ensure_ascii=False, indent=2))


def main():
    p = argparse.ArgumentParser(description='Daily Intelligence Feed')
    sub = p.add_subparsers(dest='cmd')
    gen = sub.add_parser('generate')
    gen.add_argument('--dry-run', action='store_true')
    sub.add_parser('latest')
    args = p.parse_args()
    if args.cmd == 'generate':
        cmd_generate(args)
    elif args.cmd == 'latest':
        cmd_latest(args)
    else:
        p.print_help()


if __name__ == '__main__':
    main()
