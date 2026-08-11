# News Intelligence Skill

Curated daily briefings: top 3 AI + Samudera Indonesia stories, scored and summarized.

## Overview

Fetches news from RSS feeds and NewsAPI, deduplicates, scores each candidate via DeepSeek, and selects the top 3 most important stories for Telegram delivery.

## Capabilities

- **brief** — generate a briefing (morning or midday)
- **score** — score candidate stories via DeepSeek
- **test** — dry-run: fetch, score, select, but don't send

## Usage

```bash
# Morning briefing
python .agent/skills/news-intelligence/scripts/news_briefing.py brief --mode morning

# Midday briefing
python .agent/skills/news-intelligence/scripts/news_briefing.py brief --mode midday

# Dry run (no Telegram send)
python .agent/skills/news-intelligence/scripts/news_briefing.py brief --mode morning --dry-run

# Score a single story
python .agent/skills/news-intelligence/scripts/news_briefing.py score --title "..." --summary "..."
```

## State

- Config: `config/news_intelligence.json`
- Briefings: `journal/news_briefings/YYYY-MM-DD_morning.md`
- Log: `journal/state/news_briefing_log.json`

## Integration

- **model_router**: classifies and scores stories via DeepSeek
- **orchestrator**: can be invoked as a workflow step
- **Telegram**: to be integrated later for delivery