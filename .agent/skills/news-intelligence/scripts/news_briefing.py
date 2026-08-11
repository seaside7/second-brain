#!/usr/bin/env python3
"""
news_briefing.py — Main orchestrator for the news-intelligence skill.

Pipeline:
1. Fetch RSS feeds for ai + samudera_indonesia categories
2. Score candidates via DeepSeek
3. Select top 3 stories (max) per briefing
4. Format for Telegram delivery
5. Send via Telegram (with approval gate) or dry-run
6. Store briefing to journal/news_briefings/

Usage:
    python news_briefing.py brief --mode morning [--dry-run]
    python news_briefing.py brief --mode midday [--dry-run]
    python news_briefing.py score --title "..." --summary "..."
    python news_briefing.py test --mode morning
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_DIR / "scripts"
REPO_ROOT = SKILL_DIR.parent.parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "news_intelligence.json"
STATE_PATH = REPO_ROOT / "journal" / "state" / "news_briefing_log.json"
BRIEFINGS_DIR = REPO_ROOT / "journal" / "news_briefings"

sys.path.insert(0, str(SCRIPTS_DIR))
from news_fetcher import fetch_articles, load_state, save_state, load_config
from news_scorer import score_candidates, score_single
from telegram_sender import send_message

WIB = timezone(timedelta(hours=7))


def get_midday_since(state):
    today_str = datetime.now(WIB).strftime("%Y-%m-%d")
    for entry in reversed(state.get("briefings", [])):
        if entry.get("date") == today_str and entry.get("mode") == "morning":
            return entry.get("fetched_until", None)
    return None


def format_briefing_telegram(stories, mode, config):
    now = datetime.now(WIB)
    date_str = now.strftime("%a, %d %b %Y")
    time_str = now.strftime("%H:%M WIB")
    mode_label = "Morning" if mode == "morning" else "Midday"

    lines = [
        f"<b>AI Second Brain - {mode_label} Briefing</b>",
        f"{date_str} | {time_str}",
        "",
    ]

    if not stories:
        lines.append("No significant updates for this briefing.")
        return "\n".join(lines)

    for i, story in enumerate(stories, 1):
        headline = story.get("headline", story.get("title", ""))
        summary = story.get("summary", "")
        why = story.get("why_it_matters", "")
        relevance = story.get("relevance_to_me", "")
        source = story.get("source", "")
        url = story.get("url", "")
        importance = story.get("importance", 0)
        confidence = story.get("confidence", 0)

        lines.append(f"<b>{i}. {headline}</b>")
        if summary:
            lines.append(summary)
        if why:
            lines.append(f"<i>Why it matters:</i> {why}")
        if relevance:
            lines.append(f"<i>Relevance:</i> {relevance}")
        src_line = f"Source: {source}"
        if url:
            src_line += f" - {url}"
        lines.append(src_line)
        lines.append(f"Importance: {importance}/10 | Confidence: {confidence}/10")
        lines.append("")

    lines.append("---")
    lines.append("Curated by AI Second Brain")
    return "\n".join(lines)


def save_briefing(mode, stories, config):
    now = datetime.now(WIB)
    date_str = now.strftime("%Y-%m-%d")
    filename = f"{date_str}_{mode}.md"
    BRIEFINGS_DIR.mkdir(parents=True, exist_ok=True)
    filepath = BRIEFINGS_DIR / filename

    lines = [
        f"# {mode.title()} Briefing - {now.strftime('%A, %d %B %Y')}",
        f"Generated: {now.strftime('%H:%M WIB')}",
        "",
    ]

    if not stories:
        lines.append("_No significant updates for this briefing._")
    else:
        for i, story in enumerate(stories, 1):
            lines.append(f"## {i}. {story.get('headline', story.get('title', ''))}")
            lines.append("")
            lines.append(f"**Summary:** {story.get('summary', '')}")
            lines.append("")
            lines.append(f"**Why it matters:** {story.get('why_it_matters', '')}")
            lines.append("")
            lines.append(f"**Relevance:** {story.get('relevance_to_me', '')}")
            lines.append("")
            lines.append(f"**Source:** [{story.get('source', '')}]({story.get('url', '')})")
            lines.append("")
            lines.append(f"**Importance:** {story.get('importance', 0)}/10 | "
                         f"**Confidence:** {story.get('confidence', 0)}/10")
            lines.append("")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    json_path = BRIEFINGS_DIR / f"{date_str}_{mode}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "mode": mode,
            "date": date_str,
            "time": now.strftime("%H:%M WIB"),
            "generated_wib": now.isoformat(timespec="seconds"),
            "stories": stories,
            "stories_count": len(stories),
        }, f, indent=2, ensure_ascii=False)

    print(f"[INFO] Briefing saved to {filepath}", file=sys.stderr)
    return str(filepath)


def filter_articles(articles, config, max_count=30):
    keyword_sets = {}
    for cat in ["ai", "samudera_indonesia"]:
        keywords = config.get("categories", {}).get(cat, {}).get("keywords", [])
        keyword_sets[cat] = [kw.lower() for kw in keywords]

    scored = []
    for a in articles:
        cat = a.get("category", "ai")
        kw_list = keyword_sets.get(cat, [])
        text = (a.get("title", "") + " " + a.get("summary", "")).lower()
        hits = sum(1 for kw in kw_list if kw.lower() in text)
        published = a.get("published", "")
        scored.append((hits, published, a))

    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)

    filtered = [a for _, _, a in scored[:max_count]]
    print(f"[INFO] Pre-filtered to {len(filtered)} most relevant articles (from {len(articles)})", file=sys.stderr)
    return filtered


def run_briefing(mode, dry_run=False, send=False):
    config = load_config()
    if not config:
        print("[ERROR] Failed to load config", file=sys.stderr)
        sys.exit(1)

    state = load_state()
    now = datetime.now(WIB)

    since = None
    if mode == "midday":
        since_str = get_midday_since(state)
        if since_str:
            since = since_str
        else:
            morning_time = now.replace(hour=8, minute=30, second=0, microsecond=0)
            if now < morning_time:
                morning_time = morning_time - timedelta(hours=24)
            since = morning_time.isoformat()

    if not since:
        since_time = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        since = since_time.isoformat()

    print(f"[INFO] Generating {mode} briefing (since={since}, dry_run={dry_run})", file=sys.stderr)

    category_articles = {}
    for category in ["ai", "samudera_indonesia"]:
        articles = fetch_articles(config, category, since=since, state=state)
        category_articles[category] = articles

    all_articles = category_articles["ai"] + category_articles["samudera_indonesia"]
    print(f"[INFO] Total unique articles fetched: {len(all_articles)}", file=sys.stderr)

    if not all_articles:
        print("[INFO] No new articles. Preparing empty briefing.", file=sys.stderr)
        stories = []
    else:
        stories = []
        ai_articles = category_articles.get("ai", [])
        sam_articles = category_articles.get("samudera_indonesia", [])

        ai_scored = score_candidates(
            filter_articles(ai_articles, config, max_count=20), "ai"
        ) if ai_articles else {"stories": []}
        sam_scored = score_candidates(
            filter_articles(sam_articles, config, max_count=20), "samudera_indonesia"
        ) if sam_articles else {"stories": []}

        ai_stories = ai_scored.get("stories", [])
        sam_stories = sam_scored.get("stories", [])

        if ai_stories and sam_stories:
            stories = [ai_stories[0], sam_stories[0]]
            remaining = [s for s in (ai_stories[1:] + sam_stories[1:]) if s.get("importance", 0) >= 5]
            remaining.sort(key=lambda s: s.get("importance", 0), reverse=True)
            stories.extend(remaining[:1])
        else:
            combined = ai_stories + sam_stories
            combined.sort(key=lambda s: s.get("importance", 0), reverse=True)
            stories = combined[:3]

    print(f"[INFO] Selected {len(stories)} stories for {mode} briefing", file=sys.stderr)

    telegram_text = format_briefing_telegram(stories, mode, config)

    print("")
    print("=" * 60)
    print(telegram_text)
    print("=" * 60)
    print("")

    briefing_path = save_briefing(mode, stories, config)

    state["briefings"].append({
        "date": now.strftime("%Y-%m-%d"),
        "mode": mode,
        "time": now.isoformat(),
        "stories_count": len(stories),
        "fetched_until": now.isoformat(),
        "path": briefing_path,
    })
    save_state(state)

    if send and not dry_run:
        ok, resp = send_message(telegram_text, dry_run=False)
        if ok:
            print("[OK] Telegram message sent", file=sys.stderr)
        else:
            print(f"[ERROR] Telegram send failed: {resp}", file=sys.stderr)
            sys.exit(1)
    elif dry_run:
        print("[DRY-RUN] Telegram message would be sent here", file=sys.stderr)
        ok, resp = send_message(telegram_text, dry_run=True)
        if not ok:
            print(f"[WARN] Telegram dry-run check: {resp}", file=sys.stderr)
    else:
        print("[INFO] Telegram send skipped (use --send to deliver)", file=sys.stderr)

    return {"stories": stories, "briefing_path": briefing_path, "telegram_text": telegram_text}


def main():
    parser = argparse.ArgumentParser(description="News Intelligence Briefing")
    sub = parser.add_subparsers(dest="command")

    brief_p = sub.add_parser("brief", help="Generate a briefing")
    brief_p.add_argument("--mode", required=True, choices=["morning", "midday"])
    brief_p.add_argument("--dry-run", action="store_true",
                         help="Fetch, score, and format but do not send")
    brief_p.add_argument("--send", action="store_true",
                         help="Send the briefing via Telegram (requires --approved equivalent)")

    score_p = sub.add_parser("score", help="Score a single story")
    score_p.add_argument("--title", required=True)
    score_p.add_argument("--summary", default="")
    score_p.add_argument("--category", default="ai", choices=["ai", "samudera_indonesia"])

    test_p = sub.add_parser("test", help="Dry-run: full pipeline without sending")
    test_p.add_argument("--mode", required=True, choices=["morning", "midday"])

    args = parser.parse_args()

    if args.command == "brief":
        result = run_briefing(args.mode, dry_run=args.dry_run, send=args.send)
        print(json.dumps({"status": "ok", "count": len(result["stories"]),
                          "path": result["briefing_path"]}))
    elif args.command == "score":
        result = score_single(args.title, args.summary, args.category)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif args.command == "test":
        result = run_briefing(args.mode, dry_run=True, send=False)
        print(json.dumps({"status": "ok", "count": len(result["stories"]),
                          "path": result["briefing_path"]}))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
