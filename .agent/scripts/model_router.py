#!/usr/bin/env python3
"""
Model Router - routes tasks to appropriate model tier.
Part of Second Brain: Claude/DeepSeek/OpenAI/Local routing layer.

Two entry points:

  route(category, context)  - legacy tier router (local|deepseek|claude),
                              used by orchestrator.py + news-intelligence.
  route_task(task_type, complexity, importance, context_size,
             requires_deep_reasoning, workspace)
                            - intelligent task router. Decides provider
                              (deepseek default, openai escalation), reasoning
                              level, concrete model id, and the ordered
                              fallback chain. Config-driven via
                              config/model_routing.json. Logs every routing
                              decision and every executed call outcome to
                              journal/state/model_routing_log.jsonl.

Policy (see config/model_routing.json -> routing):
  - DeepSeek is the DEFAULT for simple questions, summarization, reading docs,
    extraction, classification, rewriting, news briefings, simple finance,
    task queries, retrieval+answer, routine agent interactions.
  - OpenAI is the escalation layer: complex/multi-step reasoning, strategy,
    difficult finance, cross-source synthesis, high-stakes planning, and any
    task flagged requires_deep_reasoning / high complexity / high importance.
  - OpenAI tiers: gpt-5.6-luna (cheap) < gpt-5.6-terra (medium) < gpt-5.6-sol (high).
  - Fallback on technical failure walks the chain; invalid structured responses
    retry once then escalate. Never escalate merely because the answer is long.
"""

import json
from pathlib import Path
from datetime import datetime, timedelta

CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "model_routing.json"
USAGE_LOG = Path(__file__).parent.parent / "state" / "model_usage.json"


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_usage():
    if not USAGE_LOG.exists():
        return {"monthly_spend": 0, "month_reset": "", "calls": []}
    with open(USAGE_LOG, encoding="utf-8") as f:
        return json.load(f)


def save_usage(data):
    USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(USAGE_LOG, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def reset_monthly_if_needed(usage):
    this_month = datetime.now().strftime("%Y-%m")
    if usage.get("month_reset") != this_month:
        usage["monthly_spend"] = 0
        usage["month_reset"] = this_month
    return usage


# ---- model id helpers ----

def provider_for_model(model):
    """Provider name for a concrete model id ('deepseek-chat' -> deepseek,
    'gpt-5.6-*' -> openai, 'local' -> local)."""
    m = (model or "").lower()
    if m == "local":
        return "local"
    if m.startswith("deepseek"):
        return "deepseek"
    if m.startswith("gpt"):
        return "openai"
    if m.startswith("claude"):
        return "claude"
    return "unknown"


def tier_for_model(model):
    """OpenAI reasoning tier for a gpt model id, else None."""
    m = (model or "").lower()
    if "luna" in m:
        return "low"
    if "terra" in m:
        return "medium"
    if "sol" in m:
        return "high"
    return None


# ---- legacy router (route / status) - unchanged contract ----

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

    cost_estimates = {
        "local": 0.0,
        "deepseek": 0.005,
        "claude": 0.05,
    }

    target = rules.get(task_category, default)
    estimated_cost = cost_estimates.get(target, 0.005)
    if target != "local" and (usage["monthly_spend"] + estimated_cost) > cost_limit:
        target = "local"
        estimated_cost = 0.0

    models = config.get("models", {})
    if target == "claude" and "claude" not in models:
        target = "deepseek"
        estimated_cost = cost_estimates["deepseek"]

    usage["monthly_spend"] += estimated_cost
    usage["calls"].append({
        "timestamp": now_iso(),
        "category": task_category,
        "context": context[:200],
        "model": target,
        "cost": estimated_cost,
    })
    if len(usage["calls"]) > 500:
        usage["calls"] = usage["calls"][-500:]
    save_usage(usage)

    return {
        "model": target,
        "reason": (f"Rule match: {task_category} -> {target}"
                   if task_category in rules else f"Default: {default}"),
        "estimated_cost": estimated_cost,
        "monthly_spend": usage["monthly_spend"],
        "limit": cost_limit,
    }


def status():
    """Get current routing status."""
    config = load_config()
    usage = load_usage()
    usage = reset_monthly_if_needed(usage)

    routing = config.get("routing", {})
    return {
        "default": config.get("default"),
        "rules_count": len(config.get("rules", {})),
        "monthly_spend": usage["monthly_spend"],
        "limit": config.get("cost_limit_monthly_usd"),
        "remaining": config.get("cost_limit_monthly_usd", 0) - usage["monthly_spend"],
        "total_calls_this_month": len([c for c in usage.get("calls", [])
            if c["timestamp"].startswith(usage.get("month_reset", ""))]),
        "available_models": list(config.get("models", {}).keys()),
        "task_types": list((routing.get("task_types") or {}).keys()),
        "openai_tiers": list((routing.get("openai_tiers") or {}).keys()),
        "providers": list(config.get("providers", {}).keys()),
    }


# ---- centralized logging ----

def _log_path():
    try:
        cfg = load_config()
        rel = (cfg.get("logging") or {}).get("log_path",
                                             "journal/state/model_routing_log.jsonl")
        return Path(__file__).parent.parent.parent / rel
    except Exception:
        return Path(__file__).parent.parent.parent / "journal/state/model_routing_log.jsonl"


def _append_log(entry):
    """Append one JSON line to the routing log (best-effort, never raises)."""
    try:
        cfg = load_config()
        logging_cfg = cfg.get("logging") or {}
        if not logging_cfg.get("enabled", True):
            return
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        entry["ts"] = now_iso()
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        max_entries = int(logging_cfg.get("max_entries", 2000))
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) > max_entries:
                path.write_text("\n".join(lines[-max_entries:]) + "\n", encoding="utf-8")
        except Exception:
            pass
    except Exception:
        pass


def log_routing(result, workspace=None, complexity=0, importance=0,
                context_size=0, requires_deep_reasoning=False):
    """Log a routing decision."""
    _append_log({
        "event": "routing_decision",
        "workspace": workspace,
        "task_type": result.get("task_type"),
        "complexity": complexity,
        "importance": importance,
        "context_size": context_size,
        "requires_deep_reasoning": requires_deep_reasoning,
        "provider": result.get("provider"),
        "model": result.get("model"),
        "reasoning_level": result.get("reasoning_level"),
        "reason": result.get("reason"),
        "estimated_cost_usd": result.get("estimated_cost_usd"),
        "fallback": result.get("fallback", []),
    })


def log_call(workspace, task_type, selected_model, reason, success,
             tokens=None, fallback_events=None, error=None, duration_ms=None):
    """
    Log an executed model call outcome (callers use this).
    tokens: dict with input_tokens/output_tokens/total_tokens (or None).
    fallback_events: list of {from_model, to_model, reason}.
    """
    _append_log({
        "event": "model_call",
        "workspace": workspace,
        "task_type": task_type,
        "model": selected_model,
        "provider": provider_for_model(selected_model),
        "reason": reason,
        "success": bool(success),
        "tokens": tokens or {},
        "fallback_events": fallback_events or [],
        "error": error,
        "duration_ms": duration_ms,
    })


# ---- intelligent router (route_task) ----

def _provider_cfg(config, provider):
    return (config.get("providers") or {}).get(provider) or {}


def _tier_model(config, tier):
    tiers = (config.get("routing") or {}).get("openai_tiers") or {}
    return tiers.get(tier) or "gpt-5.6-luna"


def _estimate_cost(config, provider, model, context_chars):
    """
    Rough per-call cost estimate in USD from provider/tier prices.
    Input tokens estimated at ~4 chars/token from context_size; output ~600.
    """
    cfg = _provider_cfg(config, provider)
    in_price = cfg.get("price_usd_per_1k_input", 0.0004)
    out_price = cfg.get("price_usd_per_1k_output", 0.0016)
    if provider == "openai":
        tier_cfg = (cfg.get("tiers") or {}).get(tier_for_model(model) or "low") or {}
        in_price = tier_cfg.get("price_usd_per_1k_input", in_price)
        out_price = tier_cfg.get("price_usd_per_1k_output", out_price)
    in_tokens = int(context_chars or 0) / 4
    out_tokens = 600
    return round((in_tokens * in_price + out_tokens * out_price) / 1000.0, 6)


def _resolve_task_base(config, task_type):
    routing = config.get("routing") or {}
    types = routing.get("task_types") or {}
    default_type = routing.get("default_task_type", "simple_question")
    return (types.get(task_type) or types.get(default_type)
            or {"provider": "deepseek", "reasoning_level": "low"})


def _escalate(config, tier):
    """Build an {provider, model, reasoning_level} target for an OpenAI tier.
    tier is 'openai-low' | 'openai-medium' | 'openai-high'."""
    model = _tier_model(config, tier.replace("openai-", ""))
    return {"provider": "openai", "model": model,
            "reasoning_level": tier_for_model(model) or "low"}


def route_task(task_type="simple_question", complexity=0, importance=0,
               context_size=0, requires_deep_reasoning=False, workspace=None):
    """
    Intelligent task routing.

    Args:
        task_type: key in config routing.task_types.
        complexity: 0-10 (>=7 escalates to openai-medium, >=9 to openai-high).
        importance: 0-10 (>=8 escalates to openai-medium, >=9 to openai-high).
        context_size: chars of context the task will carry (cost estimate only).
        requires_deep_reasoning: forces an OpenAI tier.
        workspace: logged only.

    Returns:
        {provider, model, reasoning_level, task_type, reason,
         estimated_cost_usd, fallback: [{provider, model, reasoning_level, reason}]}
    """
    config = load_config()
    routing = config.get("routing") or {}
    esc = routing.get("escalation") or {}
    base = _resolve_task_base(config, task_type)

    target = dict(base)
    if "model" not in target:
        if target.get("provider") == "openai":
            target["model"] = _tier_model(
                config, routing.get("default_openai_tier", "low"))
        else:
            target["model"] = (_provider_cfg(config, "deepseek") or {}).get(
                "default_model", "deepseek-chat")

    reasons = []

    def _apply(new_target, why):
        nonlocal target
        if new_target["model"] != target["model"]:
            target = dict(new_target)
            reasons.append(why)

    if requires_deep_reasoning:
        tier = "openai-high" if importance >= 9 else "openai-medium"
        _apply(_escalate(config, tier), f"requires_deep_reasoning=True -> {tier}")

    if importance >= int(esc.get("importance_threshold", 8)):
        tier = "openai-high" if importance >= 9 else "openai-medium"
        _apply(_escalate(config, tier), f"importance={importance} -> {tier}")

    if complexity >= int(esc.get("complexity_threshold", 7)):
        tier = "openai-high" if complexity >= 9 else "openai-medium"
        _apply(_escalate(config, tier), f"complexity={complexity} -> {tier}")

    estimated = _estimate_cost(config, target["provider"], target["model"],
                               context_size)
    chain = (routing.get("fallback_chains") or {}).get(target["model"]) or []
    fallback = [{
        "provider": provider_for_model(m),
        "model": m,
        "reasoning_level": tier_for_model(m) or "low",
        "reason": "fallback chain step",
    } for m in chain]

    if not reasons:
        reasons.append(f"rule match: {task_type} -> {target['provider']}/{target['model']}")

    result = {
        "provider": target["provider"],
        "model": target["model"],
        "reasoning_level": target.get("reasoning_level", "low"),
        "task_type": task_type,
        "reason": "; ".join(reasons),
        "estimated_cost_usd": estimated,
        "fallback": fallback,
    }

    log_routing(result, workspace=workspace, complexity=complexity,
                importance=importance, context_size=context_size,
                requires_deep_reasoning=requires_deep_reasoning)
    return result


# CLI smoke test
if __name__ == "__main__":
    import argparse
    import sys

    p = argparse.ArgumentParser(description="Model router CLI")
    p.add_argument("task_type", nargs="?", default="simple_question")
    p.add_argument("--complexity", type=int, default=0)
    p.add_argument("--importance", type=int, default=0)
    p.add_argument("--context-size", type=int, default=0, dest="context_size")
    p.add_argument("--deep-reasoning", action="store_true", dest="deep_reasoning")
    p.add_argument("--workspace", default=None)
    args = p.parse_args()

    if args.task_type == "status":
        print(json.dumps(status(), indent=2))
        sys.exit(0)

    r = route_task(args.task_type, complexity=args.complexity,
                   importance=args.importance, context_size=args.context_size,
                   requires_deep_reasoning=args.deep_reasoning,
                   workspace=args.workspace)
    print(json.dumps(r, indent=2))
