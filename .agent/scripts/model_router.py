#!/usr/bin/env python3
"""
Model Router — routes tasks to appropriate model tier.
Part of Second Brain: Claude/DeepSeek/Local routing layer.
"""

import json
import sys
from pathlib import Path
from datetime import datetime, timedelta

CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "model_routing.json"
USAGE_LOG = Path(__file__).parent.parent / "state" / "model_usage.json"


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_usage():
    if not USAGE_LOG.exists():
        return {"monthly_spend": 0, "month_reset": "", "calls": []}
    with open(USAGE_LOG) as f:
        return json.load(f)


def save_usage(data):
    USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(USAGE_LOG, "w") as f:
        json.dump(data, f, indent=2)


def reset_monthly_if_needed(usage):
    this_month = datetime.now().strftime("%Y-%m")
    if usage.get("month_reset") != this_month:
        usage["monthly_spend"] = 0
        usage["month_reset"] = this_month
    return usage


def route(task_category: str, context: str = "") -> dict:
    """
    Route a task to the appropriate model.
    Returns {"model": "deepseek|local|claude", "reason": "...", "estimated_cost": 0.0}
    """
    config = load_config()
    usage = load_usage()
    usage = reset_monthly_if_needed(usage)

    rules = config.get("rules", {})
    default = config.get("default", "deepseek")
    cost_limit = config.get("cost_limit_monthly_usd", 50)

    # Simple cost estimates per call
    cost_estimates = {
        "local": 0.0,
        "deepseek": 0.005,  # ~$0.005 per medium call
        "claude": 0.05,      # ~$0.05 per medium call
    }

    # 1. Determine target model from rules
    target = rules.get(task_category, default)

    # 2. Check cost limit
    estimated_cost = cost_estimates.get(target, 0.005)
    if target != "local" and (usage["monthly_spend"] + estimated_cost) > cost_limit:
        target = "local"
        estimated_cost = 0.0

    # 3. If Claude but not configured, fall back to DeepSeek
    models = config.get("models", {})
    if target == "claude" and "claude" not in models:
        target = "deepseek"
        estimated_cost = cost_estimates["deepseek"]

    # 4. Log the call
    usage["monthly_spend"] += estimated_cost
    usage["calls"].append({
        "timestamp": datetime.now().isoformat(),
        "category": task_category,
        "context": context[:200],
        "model": target,
        "cost": estimated_cost,
    })
    # Keep only last 500 calls
    if len(usage["calls"]) > 500:
        usage["calls"] = usage["calls"][-500:]
    save_usage(usage)

    return {
        "model": target,
        "reason": f"Rule match: {task_category} → {target}" if task_category in rules else f"Default: {default}",
        "estimated_cost": estimated_cost,
        "monthly_spend": usage["monthly_spend"],
        "limit": cost_limit,
    }


def status():
    """Get current routing status."""
    config = load_config()
    usage = load_usage()
    usage = reset_monthly_if_needed(usage)

    return {
        "default": config.get("default"),
        "rules_count": len(config.get("rules", {})),
        "monthly_spend": usage["monthly_spend"],
        "limit": config.get("cost_limit_monthly_usd"),
        "remaining": config.get("cost_limit_monthly_usd", 0) - usage["monthly_spend"],
        "total_calls_this_month": len([c for c in usage.get("calls", [])
            if c["timestamp"].startswith(usage.get("month_reset", ""))]),
        "available_models": list(config.get("models", {}).keys()),
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps(status(), indent=2))
    elif sys.argv[1] == "route":
        category = sys.argv[2] if len(sys.argv) > 2 else "simple_lookup"
        context = sys.argv[3] if len(sys.argv) > 3 else ""
        print(json.dumps(route(category, context), indent=2))
    elif sys.argv[1] == "status":
        print(json.dumps(status(), indent=2))
    else:
        print(f"Unknown command: {sys.argv[1]}")
        print("Usage: model_router.py [route <category> [context] | status]")