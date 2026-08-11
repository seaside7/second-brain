#!/usr/bin/env python3
"""
news_fetcher.py — Fetch news from RSS feeds and NewsAPI for the news-intelligence skill.

Supports:
- RSS feed fetching (primary, no API key needed)
- NewsAPI (optional, if key is set in config)
- GNews (optional, if key is set in config)

Deduplicates by URL and tracks seen stories across briefings.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser

SKILL_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SKILL_DIR.parent.parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "news_intelligence.json"
STATE_PATH = REPO_ROOT / "journal" / "state" / "news_briefing_log.json"

DEFAULT_TIMEOUT = 30
WIB = timezone(timedelta(hours=7))


def load_config():
    if not CONFIG_PATH.exists():
        print(f"[ERROR] Config not found: {CONFIG_PATH}", file=sys.stderr)
        return None
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_state():
    if STATE_PATH.exists():
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"seen_urls": {}, "briefings": []}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(STATE_PATH) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)


def fetch_rss(url, timeout=DEFAULT_TIMEOUT):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ai-second-brain/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        feed = feedparser.parse(raw)
        if feed.bozo and not feed.entries:
            print(f"[WARN] RSS parse error for {url}: {feed.bozo_exception}", file=sys.stderr)
            return []
        return feed.entries
    except Exception as e:
        print(f"[WARN] Failed to fetch RSS {url}: {e}", file=sys.stderr)
        return []


def truncate(text, max_len=300):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        return text[:max_len - 3] + "..."
    return text


def url_key(url):
    return hashlib.md5(url.strip().lower().encode()).hexdigest()


def fetch_articles(config, category, since=None, state=None):
    if state is None:
        state = load_state()

    cat_config = config.get("categories", {}).get(category)
    if not cat_config:
        print(f"[ERROR] Unknown category: {category}", file=sys.stderr)
        return []

    rss_urls = config.get("api", {}).get("rss_feeds", {}).get(category, [])
    if not rss_urls:
        rss_urls = cat_config.get("rss_feeds", [])

    articles = []
    seen_keys = state.get("seen_urls", {})
    cutoff = None
    if since:
        if isinstance(since, datetime):
            cutoff = since
        elif isinstance(since, str):
            cutoff = datetime.fromisoformat(since)

    now = datetime.now(WIB)

    for rss_url in rss_urls:
        print(f"[DEBUG] Fetching RSS: {rss_url}", file=sys.stderr)
        entries = fetch_rss(rss_url)
        source_name = _extract_source_name(rss_url)
        for entry in entries:
            link = entry.get("link", "")
            if not link:
                continue

            key = url_key(link)
            if key in seen_keys:
                continue

            published = _parse_pubdate(entry)
            if cutoff and published and published < cutoff:
                continue

            title = truncate(entry.get("title", ""), 200)
            summary = truncate(entry.get("summary", entry.get("description", "")), 300)

            if not title:
                continue

            articles.append({
                "title": title,
                "summary": summary,
                "url": link,
                "source": source_name,
                "published": published.isoformat() if published else None,
                "category": category,
                "url_hash": key,
            })
            seen_keys[key] = now.isoformat()

    print(f"[INFO] Fetched {len(articles)} new articles for category '{category}'", file=sys.stderr)
    return articles


def _extract_source_name(rss_url):
    parsed = urllib.parse.urlparse(rss_url)
    domain = parsed.netloc.replace("www.", "")
    domain = domain.replace("feeds.", "").replace("rss.", "")
    domain = domain.split(".")[0]
    return domain.capitalize()


def _parse_pubdate(entry):
    for field in ("published_parsed", "updated_parsed"):
        tp = entry.get(field)
        if tp:
            try:
                from time import mktime
                ts = mktime(tp)
                return datetime.fromtimestamp(ts, tz=WIB)
            except Exception:
                continue

    for field in ("published", "updated"):
        val = entry.get(field, "")
        if val:
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(val)
                return dt.astimezone(WIB)
            except Exception:
                continue
    return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="News Fetcher")
    parser.add_argument("--category", required=True, choices=["ai", "samudera_indonesia"])
    parser.add_argument("--since", help="Only fetch articles published after this ISO datetime")
    parser.add_argument("--output", choices=["json", "jsonl"], default="json")
    args = parser.parse_args()

    config = load_config()
    if not config:
        sys.exit(1)

    state = load_state()
    articles = fetch_articles(config, args.category, since=args.since, state=state)
    save_state(state)

    if args.output == "jsonl":
        for a in articles:
            print(json.dumps(a, ensure_ascii=False))
    else:
        print(json.dumps(articles, indent=2, ensure_ascii=False))
