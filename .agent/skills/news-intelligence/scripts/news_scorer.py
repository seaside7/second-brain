#!/usr/bin/env python3
"""
news_scorer.py — Score news articles via DeepSeek for the news-intelligence skill.

Sends candidate articles to DeepSeek for multi-dimensional scoring.
Returns a ranked and filtered list with full metadata.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SKILL_DIR.parent.parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "news_intelligence.json"

SCRIPTS_DIR = REPO_ROOT / ".agent" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
from deepseek_call import call as deepseek_call

WIB = timezone(timedelta(hours=7))

SCORING_PROMPT = """You are a news analyst scoring articles for a concise daily briefing. The recipient works in AI engineering and digital transformation, and is the incoming Head of Digital Transformation at Samudera Indonesia (shipping/logistics).

Scoring dimensions (1-10 scale, 10=highest):
- importance: How significant is this story for the industry, market, or world? (weight 0.35)
- relevance: How directly does this affect the recipient's work or interests? (weight 0.25)
- credibility: How trustworthy and authoritative is the source? (weight 0.20)
- traction: How widely is this being covered across multiple sources? (weight 0.10)
- urgency: How time-sensitive is this? Must the recipient know today? (weight 0.10)

Instructions:
1. Score EVERY article on ALL five dimensions.
2. Weighted score = importance*0.35 + relevance*0.25 + credibility*0.2 + traction*0.1 + urgency*0.1
3. Minimum weighted-score threshold: 55/100. Below that, the story should NOT be selected.
4. Select at most 3 stories TOTAL. Quality over quantity is critical. If only 1 story passes threshold, return just 1.
5. NEVER fabricate information. If the source doesn't say something, don't add it.
6. Clearly distinguish facts in the source from inference/interpretation.
7. For any selected story, provide ALL fields: headline, summary, why_it_matters, relevance_to_me, source, url, importance (1-10), confidence (1-10).
8. The "relevance_to_me" field must explain specifically why this story matters to someone in AI engineering AND/or digital transformation at a shipping/logistics company. If there's no clear relevance, the story should not be selected.
9. Deduplicate: if multiple entries cover the same event, only score the best one.

Return ONLY a JSON object. No markdown, no explanations outside the JSON.
Format:
{
  "stories": [
    {
      "headline": "...",
      "summary": "...",
      "why_it_matters": "...",
      "relevance_to_me": "...",
      "source": "...",
      "url": "...",
      "importance": N,
      "confidence": N
    }
  ],
  "scores": {"all": [{"title": "...", "composite": N.N, "verdict": "selected|below_threshold|duplicate", ...}]},
  "selected_count": N,
  "total_candidates": N
}
"""


def load_config():
    if not CONFIG_PATH.exists():
        print(f"[ERROR] Config not found: {CONFIG_PATH}", file=sys.stderr)
        return None
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def build_scoring_payload(articles, category):
    lines = []
    for i, a in enumerate(articles):
        lines.append(f"[{i}] Title: {a.get('title', '')}")
        lines.append(f"    Summary: {a.get('summary', '')}")
        lines.append(f"    Source: {a.get('source', '')}")
        lines.append(f"    URL: {a.get('url', '')}")
        lines.append(f"    Published: {a.get('published', 'unknown')}")
        lines.append("")

    article_list = "\n".join(lines)

    prompt = f"{SCORING_PROMPT}\n\nCategory: {category}\n\nARTICLES TO SCORE:\n{article_list}\n\nReturn JSON:"
    return prompt


def score_candidates(articles, category):
    config = load_config()
    weights = config.get("scoring", {}).get("weights", {})
    threshold = config.get("scoring", {}).get("min_priority_threshold", 55)

    if not articles:
        return {"stories": [], "scores": {"all": []}, "selected_count": 0, "total_candidates": 0}

    print(f"[INFO] Scoring {len(articles)} candidate articles for category '{category}'", file=sys.stderr)

    prompt = build_scoring_payload(articles, category)
    ok, text, meta = deepseek_call(prompt, max_tokens=4096, temperature=0.2, timeout=90)

    if not ok:
        print(f"[ERROR] DeepSeek scoring failed: {meta.get('reason', 'unknown')}", file=sys.stderr)
        return {"stories": [], "scores": {"all": []}, "selected_count": 0,
                "total_candidates": len(articles), "error": meta.get("reason")}

    try:
        text = text.strip()
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        result = json.loads(text.strip())
    except json.JSONDecodeError as e:
        print(f"[ERROR] Failed to parse DeepSeek response as JSON: {e}", file=sys.stderr)
        print(f"[DEBUG] Raw response (first 500 chars): {text[:500]}", file=sys.stderr)
        return {"stories": [], "scores": {"all": []}, "selected_count": 0,
                "total_candidates": len(articles), "error": "json_parse_failed"}

    result["total_candidates"] = len(articles)
    if "selected_count" not in result:
        result["selected_count"] = len(result.get("stories", []))
    if "scores" not in result:
        result["scores"] = {"all": []}

    for story in result.get("stories", []):
        story.setdefault("importance", 0)
        story.setdefault("confidence", 0)
        story["category"] = category

    result["stories"] = result["stories"][:3]
    result["selected_count"] = len(result["stories"])

    print(f"[INFO] DeepSeek selected {result['selected_count']} stories "
          f"from {len(articles)} candidates (tokens: {meta.get('total_tokens', 0)})", file=sys.stderr)
    return result


def score_single(title, summary, category="ai"):
    articles = [{"title": title, "summary": summary, "url": "", "source": "", "published": None}]
    return score_candidates(articles, category)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="News Scorer via DeepSeek")
    parser.add_argument("--title", help="Story headline")
    parser.add_argument("--summary", help="Story summary")
    parser.add_argument("--category", default="ai", choices=["ai", "samudera_indonesia"])
    parser.add_argument("--articles-file", dest="articles_file",
                        help="Path to JSON file of candidate articles")
    args = parser.parse_args()

    if args.articles_file:
        with open(args.articles_file, "r", encoding="utf-8") as f:
            articles = json.load(f)
        result = score_candidates(articles, args.category)
    elif args.title:
        result = score_single(args.title, args.summary or "", args.category)
    else:
        articles_raw = sys.stdin.read().strip()
        if articles_raw:
            articles = json.loads(articles_raw)
            result = score_candidates(articles, args.category)
        else:
            print("[ERROR] Provide --title/--summary, --articles-file, or pipe JSON to stdin", file=sys.stderr)
            sys.exit(1)

    print(json.dumps(result, indent=2, ensure_ascii=False))

    if result.get("error"):
        sys.exit(1)
