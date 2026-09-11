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

import importlib
import json
import os
import sys
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


# ══════════════════════════════════════════════════════════════════════════════
# Module model registry (Settings → AI Models)
# ────────────────────────────────────────────────────────────────────────────────
# The dashboard's Settings page exposes one per-module model selector. THIS file
# is the single resolution service: every module (chat, news, memory, finance,
# coding, ...) resolves its model through resolve_module(), never its own copy of
# model-selection logic.
#
# Resolution order (first match wins):
#   1. module override     -> .agent/workspaces/<ws>/state/model_settings.json[modules][id]
#   2. workspace default   -> same file[workspace_defaults][capability]
#   3. capability default  -> config/model_routing.json[capability_defaults][cap]
#   4. system default      -> MODULES[id].defaults below (today's behavior)
#   5. env default         -> DEEPSEEK_API_KEY ? deepseek-chat : OPENAI_MODEL_LUNA ...
#
# Secrets never appear here: availability is a boolean; keys stay in .env.
# ────────────────────────────────────────────────────────────────────────────────

CATALOG_PATH = Path(__file__).parent.parent.parent / "config" / "model_catalog.json"
MODEL_AUDIT_LOG = Path(__file__).parent.parent.parent / "journal" / "state" / "model_audit.jsonl"
KNOWN_WORKSPACES = ("personal", "samudera", "catalyze", "shared")

# Capability strings (must match config/model_catalog.json capabilities).
CAP_TEXT = "text_generation"
CAP_EMBED = "embedding"
CAP_IMAGE = "image"
CAP_AUDIO = "audio_transcription"
CAP_CODING = "coding"

# agy-bridge / claude "backends" allowed as the last emergency leg. The agy task
# name doubles as the model selector ('harvest' <-> claude haiku).
AGY_TASKS = ("harvest", "draft", "research", "critic")


def settings_path(workspace="personal"):
    return Path(__file__).parent.parent.parent / ".agent" / "workspaces" / (
        workspace if workspace in KNOWN_WORKSPACES else "personal") / "state" / "model_settings.json"


def load_settings(workspace="personal"):
    """Per-workspace model overrides; {} when missing or corrupt (fail-safe)."""
    p = settings_path(workspace)
    try:
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def save_settings(workspace, data):
    """Atomic write of the settings file (tmp + replace), safe on corrupt parent."""
    p = settings_path(workspace)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, p)


def audit_model(workspace, action, module, prev, new, actor="dashboard"):
    """Append one non-secret audit line (best-effort, never raises)."""
    try:
        MODEL_AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(MODEL_AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": now_iso(), "actor": actor, "workspace": workspace,
                "action": action, "module": module, "prev": prev, "new": new,
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def load_catalog():
    """Non-secret model metadata catalog; {} on missing/corrupt."""
    try:
        if CATALOG_PATH.exists():
            with open(CATALOG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def provider_available(provider):
    """Ability to CALL this provider on this machine (credential presence only)."""
    if provider == "deepseek":
        return bool(os.environ.get("DEEPSEEK_API_KEY"))
    if provider in ("openai",):
        return bool(os.environ.get("OPENAI_API_KEY"))
    if provider == "gemini":
        return bool(os.environ.get("GEMINI_API_KEY"))
    if provider == "gemini-image":
        tok = Path(__file__).parent.parent / "skills" / "gemini-image" / "token.env"
        return bool(os.environ.get("GEMINI_API_KEY") or tok.exists())
    if provider == "claude":
        try:
            if SCRIPT_DIR not in sys.path:
                sys.path.insert(0, SCRIPT_DIR)
            import ai_call
            return ai_call.claude_bin() is not None
        except Exception:
            return False
    if provider == "ai_call":
        try:
            if SCRIPT_DIR not in sys.path:
                sys.path.insert(0, SCRIPT_DIR)
            import ai_call
            return ai_call.agy_available() or ai_call.claude_bin() is not None
        except Exception:
            return False
    if provider == "agy":
        try:
            if SCRIPT_DIR not in sys.path:
                sys.path.insert(0, SCRIPT_DIR)
            import ai_call
            return ai_call.agy_available()
        except Exception:
            return False
    return False


# Capability -> provider that owns the model id. Used to validate that a chosen
# model id is compatible with a module's capability AND its provider.
def _model_capabilities(model_id):
    cat = load_catalog()
    entry = (cat.get("models") or {}).get(model_id) or {}
    return entry.get("capabilities") or []


def _model_provider(model_id):
    cat = load_catalog()
    entry = (cat.get("models") or {}).get(model_id) or {}
    return entry.get("provider")


# ── Module registry ────────────────────────────────────────────────────────────
# Provider values: 'deepseek' | 'openai' | 'ai_call' (agy/claude via ai_call).
# emergency: optional extra last-leg backend (ai_call) so migrated server.py
# chat/deepdive/news-chat call sites keep their today behavior via execute().
MODULES = {
    # id: {label, purpose, workspace, capability, defaults,
    #      fallback: (provider, model) or None, emergency: bool}
    "personal_chat": {
        "label": "Personal Chat",
        "purpose": "Chatbox replies + slash commands (/invest, /focus, ...)",
        "workspace": "personal",
        "capability": CAP_TEXT,
        "defaults": {"provider": "deepseek", "model": "deepseek-chat"},
        "fallback": ("openai", "gpt-5.6-terra"),
        "emergency": True,
    },
    "samudera_chat": {
        "label": "Samudera Chat",
        "purpose": "Chatbox replies + slash commands (/orchestrate, /brief, ...)",
        "workspace": "samudera",
        "capability": CAP_TEXT,
        "defaults": {"provider": "deepseek", "model": "deepseek-chat"},
        "fallback": ("openai", "gpt-5.6-terra"),
        "emergency": True,
    },
    "stock_deepdive": {
        "label": "Stock Deep Dive",
        "purpose": "/deepdive - full 18-section stock report narrative",
        "workspace": "personal",
        "capability": CAP_TEXT,
        "defaults": {"provider": "deepseek", "model": "deepseek-chat"},
        "fallback": ("openai", "gpt-5.6-terra"),
        "emergency": True,
    },
    "news_chat": {
        "label": "News Chat",
        "purpose": "Ask AI about a news story (below My take)",
        "workspace": "shared",
        "capability": CAP_TEXT,
        "defaults": {"provider": "deepseek", "model": "deepseek-chat"},
        "fallback": ("openai", "gpt-5.6-terra"),
        "emergency": True,
    },
    "news_scoring": {
        "label": "News Scoring",
        "purpose": "Score candidate articles for the daily briefing",
        "workspace": "shared",
        "capability": CAP_TEXT,
        "defaults": {"provider": "deepseek", "model": "deepseek-chat"},
        "fallback": ("openai", "gpt-5.6-luna"),
        "emergency": False,
    },
    "news_writer": {
        "label": "News Writer",
        "purpose": "Intel-feed editorial drafting (global economy editorial)",
        "workspace": "shared",
        "capability": CAP_TEXT,
        "defaults": {"provider": "openai", "model": "gpt-5.6-luna"},
        "fallback": ("deepseek", "deepseek-chat"),
        "emergency": False,
    },
    "memory_classify": {
        "label": "Memory Classification",
        "purpose": "Classify + store memory notes",
        "workspace": "shared",
        "capability": CAP_TEXT,
        "defaults": {"provider": "deepseek", "model": "deepseek-chat"},
        "fallback": ("openai", "gpt-5.6-luna"),
        "emergency": False,
    },
    "memory_summary": {
        "label": "Memory Summarization",
        "purpose": "Memory recall summarization + knowledge synthesis",
        "workspace": "shared",
        "capability": CAP_TEXT,
        "defaults": {"provider": "deepseek", "model": "deepseek-chat"},
        "fallback": ("openai", "gpt-5.6-luna"),
        "emergency": False,
    },
    "finance_analysis": {
        "label": "Finance Analysis",
        "purpose": "Personal finance analysis + insights",
        "workspace": "personal",
        "capability": CAP_TEXT,
        "defaults": {"provider": "deepseek", "model": "deepseek-chat"},
        "fallback": ("openai", "gpt-5.6-luna"),
        "emergency": False,
    },
    "inbox_digest": {
        "label": "Inbox Digest",
        "purpose": "Inbox sweep -> digest draft + review",
        "workspace": "catalyze",
        "capability": CAP_TEXT,
        "defaults": {"provider": "agy", "model": "draft"},
        "fallback": ("openai", "gpt-5.6-luna"),
        "emergency": False,
    },
    "premeeting_enrich": {
        "label": "Pre-meeting Enrichment",
        "purpose": "Pre-meeting card enrichment",
        "workspace": "samudera",
        "capability": CAP_TEXT,
        "defaults": {"provider": "agy", "model": "draft"},
        "fallback": ("claude", "sonnet"),
        "emergency": False,
    },
    "commitment_extract": {
        "label": "Commitment Extraction",
        "purpose": "Commitment ledger sweep",
        "workspace": "shared",
        "capability": CAP_TEXT,
        "defaults": {"provider": "agy", "model": "harvest"},
        "fallback": ("claude", "haiku"),
        "emergency": False,
    },
    "mention_classify": {
        "label": "Mention Classification",
        "purpose": "Slack mention triage",
        "workspace": "shared",
        "capability": CAP_TEXT,
        "defaults": {"provider": "agy", "model": "harvest"},
        "fallback": ("claude", "haiku"),
        "emergency": False,
    },
    "reply_queue": {
        "label": "Reply Queue",
        "purpose": "Reply-queue draft generation",
        "workspace": "shared",
        "capability": CAP_TEXT,
        "defaults": {"provider": "agy", "model": "harvest"},
        "fallback": ("claude", "haiku"),
        "emergency": False,
    },
    "mom_draft": {
        "label": "Meeting Minutes (MOM) Draft",
        "purpose": "Meeting summary drafting",
        "workspace": "shared",
        "capability": CAP_TEXT,
        "defaults": {"provider": "agy", "model": "draft"},
        "fallback": ("openai", "gpt-5.6-luna"),
        "emergency": False,
    },
    "build_insights": {
        "label": "Meeting Insights",
        "purpose": "Insights from meetings",
        "workspace": "shared",
        "capability": CAP_TEXT,
        "defaults": {"provider": "agy", "model": "draft"},
        "fallback": ("claude", "sonnet"),
        "emergency": False,
    },
    "command_triage": {
        "label": "Command Triage",
        "purpose": "Command queue triage + execute",
        "workspace": "shared",
        "capability": CAP_TEXT,
        "defaults": {"provider": "agy", "model": "harvest"},
        "fallback": ("claude", "haiku"),
        "emergency": False,
    },
    "executive_orchestrate": {
        "label": "Executive Orchestrator",
        "purpose": "Classify + synthesize orchestrator output",
        "workspace": "samudera",
        "capability": CAP_TEXT,
        "defaults": {"provider": "deepseek", "model": "deepseek-chat"},
        "fallback": ("openai", "gpt-5.6-luna"),
        "emergency": False,
    },
    "action_execute": {
        "label": "Action Executor",
        "purpose": "Action execution prompts",
        "workspace": "shared",
        "capability": CAP_TEXT,
        "defaults": {"provider": "deepseek", "model": "deepseek-chat"},
        "fallback": None,
        "emergency": False,
    },
    "transformation_research": {
        "label": "Transformation Research",
        "purpose": "Samudera research skill",
        "workspace": "samudera",
        "capability": CAP_TEXT,
        "defaults": {"provider": "openai", "model": "gpt-5.6-terra"},
        "fallback": None,
        "emergency": False,
    },
    "transformation_strategy": {
        "label": "Transformation Strategy",
        "purpose": "Samudera strategy skill",
        "workspace": "samudera",
        "capability": CAP_TEXT,
        "defaults": {"provider": "openai", "model": "gpt-5.6-sol"},
        "fallback": None,
        "emergency": False,
    },
    "doc_sync": {
        "label": "Document Sync",
        "purpose": "Meeting index sync (doc_engine)",
        "workspace": "shared",
        "capability": CAP_TEXT,
        "defaults": {"provider": "deepseek", "model": "deepseek-chat"},
        "fallback": None,
        "emergency": False,
    },
    "action_extract": {
        "label": "Action-Item Extraction",
        "purpose": "Fathom transcript action-item scan",
        "workspace": "shared",
        "capability": CAP_TEXT,
        "defaults": {"provider": "agy", "model": "harvest"},
        "fallback": ("claude", "haiku"),
        "emergency": False,
    },
    # ai-task detached workers (per-kind; model strings live in _ai_task_spec)
    "ai_task_ping": {
        "label": "ai-task Ping", "purpose": "Smoke test worker",
        "workspace": "shared", "capability": CAP_TEXT,
        "defaults": {"provider": "claude", "model": "haiku"},
        "fallback": None, "emergency": False,
    },
    "ai_task_commitment": {
        "label": "ai-task Commitment", "purpose": "Commitment write-up worker",
        "workspace": "shared", "capability": CAP_TEXT,
        "defaults": {"provider": "claude", "model": "sonnet"},
        "fallback": None, "emergency": False,
    },
    "ai_task_fix": {
        "label": "ai-task Fix-job", "purpose": "Fix scheduled-job failures",
        "workspace": "shared", "capability": CAP_TEXT,
        "defaults": {"provider": "claude", "model": "sonnet"},
        "fallback": None, "emergency": False,
    },
    "ai_task_inbox": {
        "label": "ai-task Inbox", "purpose": "Inbox research + draft worker",
        "workspace": "catalyze", "capability": CAP_TEXT,
        "defaults": {"provider": "claude", "model": "opus"},
        "fallback": None, "emergency": False,
    },
    "ai_task_inbox_digest": {
        "label": "ai-task Inbox Digest", "purpose": "Inbox digest review worker",
        "workspace": "catalyze", "capability": CAP_TEXT,
        "defaults": {"provider": "claude", "model": "haiku"},
        "fallback": None, "emergency": False,
    },
    "ai_task_premeeting": {
        "label": "ai-task Pre-meeting", "purpose": "Pre-meeting enrichment worker",
        "workspace": "samudera", "capability": CAP_TEXT,
        "defaults": {"provider": "claude", "model": "haiku"},
        "fallback": None, "emergency": False,
    },
    "transaction_categorize": {
        "label": "Transaction Categorize",
        "purpose": "AI categorisation of ambiguous personal transactions",
        "workspace": "personal",
        "capability": CAP_TEXT,
        "defaults": {"provider": "deepseek", "model": "deepseek-chat"},
        "fallback": ("openai", "gpt-4o-mini"),
        "emergency": False,
    },
    "knowledge_embeddings": {
        "label": "Knowledge Embeddings",
        "purpose": "FAISS embedding index for memory recall",
        "workspace": "shared",
        "capability": CAP_EMBED,
        "defaults": {"provider": "openai", "model": "text-embedding-3-small"},
        "fallback": None,
        "emergency": False,
    },
    "gemini_image": {
        "label": "Gemini Image",
        "purpose": "Image generation (gemini-image skill)",
        "workspace": "shared",
        "capability": CAP_IMAGE,
        "defaults": {"provider": "gemini-image", "model": "gemini-3-pro-image"},
        "fallback": None,
        "emergency": False,
    },
    "meeting_transcription": {
        "label": "Meeting Transcription",
        "purpose": "Recording -> text (meeting-recorder)",
        "workspace": "shared",
        "capability": CAP_AUDIO,
        "defaults": {"provider": "openai", "model": "whisper-1"},
        "fallback": ("gemini", "gemini-2.5-flash"),
        "emergency": False,
    },
    "meetbot_transcription": {
        "label": "Meetbot Transcription",
        "purpose": "Bot transcript transcription (whisper.cpp server)",
        "workspace": "shared",
        "capability": CAP_AUDIO,
        "defaults": {"provider": "openai", "model": "whisper-1"},
        "fallback": None,
        "emergency": False,
    },
    "coding_agent": {
        "label": "Coding Agent",
        "purpose": "Plan + build coding jobs (opencode-managed)",
        "workspace": "shared",
        "capability": CAP_CODING,
        "defaults": {"provider": "opencode", "model": ""},
        "fallback": None,
        "emergency": False,
    },
}


def module_ids():
    return list(MODULES.keys())


def module_spec(module_id):
    return MODULES.get(module_id)


def capability_defaults():
    """config/model_routing.json[capability_defaults] or {}."""
    try:
        cfg = load_config()
        return cfg.get("capability_defaults") or {}
    except Exception:
        return {}


def _env_default(capability):
    """Layer 5: today's env-driven behavior when nothing has been configured."""
    if capability == CAP_TEXT:
        if os.environ.get("DEEPSEEK_API_KEY"):
            return "deepseek", "deepseek-chat"
        if os.environ.get("OPENAI_API_KEY"):
            return "openai", os.environ.get("OPENAI_MODEL_LUNA", "gpt-5.6-luna")
        return None, None
    if capability == CAP_EMBED:
        return ("openai", "text-embedding-3-small") if os.environ.get("OPENAI_API_KEY") else (None, None)
    if capability == CAP_IMAGE:
        return ("gemini-image", "gemini-3-pro-image")
    if capability == CAP_AUDIO:
        return ("openai", "whisper-1")
    if capability == CAP_CODING:
        return ("opencode", "")
    return None, None


def _fallback_for(provider, model, mod, cap_defaults):
    """Pick a compatible fallback pair for a chosen primary."""
    # explicit config-level fallback for text generation from model_routing chains
    if provider == "deepseek" and model == "deepseek-chat":
        chains = (load_config().get("routing") or {}).get("fallback_chains") or {}
        chain = chains.get("deepseek-chat") or []
        if chain:
            mp = _model_provider(chain[0])
            if mp:
                return mp, chain[0]
    # module's built-in fallback
    if mod and mod.get("fallback"):
        return mod["fallback"][0], mod["fallback"][1]
    # capability default's fallback
    cdef = cap_defaults.get(mod.get("capability") if mod else CAP_TEXT) or {}
    if cdef.get("fallbackProvider") and cdef.get("fallbackModel"):
        return cdef["fallbackProvider"], cdef["fallbackModel"]
    return None, None


def resolve_module(module_id, workspace="personal", _ctx=None):
    """Resolve the effective model for one module.

    Resolution order: module override -> workspace default -> capability default
    -> system (MODULES) default -> env default. Returns a dict with the effective
    provider/model, the source of truth, capability compatibility, availability,
    fallback and emergency-leg info. Never raises for an unknown module id
    (returns a minimal record with provider 'unknown')."""
    mod = MODULES.get(module_id)
    if not mod:
        return {"module": module_id, "label": module_id, "capability": CAP_TEXT,
                "provider": "unknown", "model": None, "source": "none",
                "available": False, "enabled": False, "error": "unknown_module"}
    cap = mod["capability"]
    ws = workspace if workspace in KNOWN_WORKSPACES else "personal"
    settings = load_settings(ws)
    cap_defaults = capability_defaults()
    cdef = cap_defaults.get(cap) or {}

    # 1. module override
    mo = (settings.get("modules") or {}).get(module_id)
    if isinstance(mo, dict) and mo.get("provider") and mo.get("model"):
        provider, model = mo["provider"], mo["model"]
        fb = _fallback_for(provider, model, None, cap_defaults)
        if mo.get("fallbackProvider") and mo.get("fallbackModel"):
            fb = (mo["fallbackProvider"], mo["fallbackModel"])
        return _resolved(mod, ws, provider, model, fb,
                         source="module", enabled=bool(mo.get("enabled", True)),
                         module_id=module_id)

    # 2. workspace default for this capability
    wd = (settings.get("workspace_defaults") or {}).get(cap)
    if isinstance(wd, dict) and wd.get("provider") and wd.get("model"):
        fb = _fallback_for(wd["provider"], wd["model"], mod, cap_defaults)
        if wd.get("fallbackProvider") and wd.get("fallbackModel"):
            fb = (wd["fallbackProvider"], wd["fallbackModel"])
        return _resolved(mod, ws, wd["provider"], wd["model"], fb,
                         source="workspace", enabled=bool(wd.get("enabled", True)),
                         module_id=module_id)

    # 3. capability default (config/model_routing.json) — the generic baseline
    #    for a capability. Only applies to modules WITHOUT a purpose-specific
    #    built-in default below (per the approved plan: modules that define
    #    their own tier keep their purpose model).
    if (not mod.get("defaults") or not mod.get("defaults", {}).get("provider")) \
            and cdef.get("provider") and cdef.get("model"):
        fb = _fallback_for(cdef["provider"], cdef["model"], None, cap_defaults)
        return _resolved(mod, ws, cdef["provider"], cdef["model"], fb,
                         source="capability", enabled=True, module_id=module_id)

    # 4. system default (this module's built-in behavior)
    d = mod.get("defaults") or {}
    if d.get("provider"):
        fb = mod.get("fallback")
        return _resolved(mod, ws, d["provider"], d.get("model"), fb,
                         source="system", enabled=True, module_id=module_id)

    # 5. env default
    provider, model = _env_default(cap)
    if provider:
        return _resolved(mod, ws, provider, model, mod.get("fallback"),
                         source="env", enabled=True, module_id=module_id)

    return _resolved(mod, ws, None, None, None, source="env", enabled=True,
                     module_id=module_id)


def _resolved(mod, ws, provider, model, fallback, source, enabled, module_id=None):
    cap = mod["capability"]
    available = provider_available(provider) if provider and provider not in (
        "ai_call", "claude", "agy", "gemini-image", "opencode") else (
        provider_available(provider))
    compatible = (not model) or (cap in _model_capabilities(model)) or _is_backend_for_cap(provider, cap)
    return {
        "module": mod.get("label") or "",
        "module_id": module_id,
        "purpose": mod.get("purpose"),
        "workspace": mod.get("workspace"),
        "capability": cap,
        "provider": provider,
        "model": model,
        "source": source,
        "enabled": bool(enabled),
        "available": available,
        "compatible": compatible,
        "fallback": {"provider": fallback[0] if fallback else None,
                     "model": fallback[1] if fallback else None} if fallback else None,
        "emergency": bool(mod.get("emergency")),
        "capability_description": _capability_description(cap),
    }


def _is_backend_for_cap(provider, cap):
    if cap == CAP_TEXT and provider in ("agy", "claude", "ai_call"):
        return True
    if cap == CAP_AUDIO and provider in ("gemini",):
        return True
    return False


def _capability_description(cap):
    cat = load_catalog()
    return ((cat.get("capabilities") or {}).get(cap) or {}).get("description", cap)


def _module_id_for(label):
    for mid, m in MODULES.items():
        if (m.get("label") or "").lower() == (label or "").lower():
            return mid
    return None


def catalog_for_capability(cap, ws="personal"):
    """Enabled, compatible, non-secret model options for one capability."""
    cat = load_catalog()
    out = []
    spec = (cat.get("capabilities") or {}).get(cap) or {}
    for mid in spec.get("models") or []:
        entry = (cat.get("models") or {}).get(mid) or {}
        if not entry.get("enabled", True):
            continue
        out.append({
            "model": mid,
            "provider": entry.get("provider"),
            "displayName": entry.get("displayName", mid),
            "available": provider_available(entry.get("provider") or ""),
        })
    # extra ai_call / agy / claude backends for text generation
    if cap == CAP_TEXT:
        for key in ("agy", "claude"):
            out.append({
                "model": key,
                "provider": key,
                "displayName": (cat.get("agy_backends") or {}).get(
                    key, {}).get("displayName", key),
                "available": provider_available(key),
            })
    return out


def list_modules(ws="personal"):
    """Full module list with effective settings, for GET /api/models."""
    rows = []
    for mid in MODULES:
        r = resolve_module(mid, ws)
        rows.append({k: r[k] for k in (
            "module", "module_id", "purpose", "workspace", "capability",
            "provider", "model", "source", "enabled", "available", "compatible",
            "fallback", "emergency", "capability_description")})
    rows.sort(key=lambda r: (r["capability"], r["module"]))
    return rows


# ── settings mutations (POST /api/models/*) ───────────────────────────────────

ROLE_PROVIDERS = ("deepseek", "openai", "agy", "claude")


def set_module(workspace, module_id, provider, model, fallback_provider=None,
               fallback_model=None, enabled=True):
    """Validate + save one module override. Returns (ok, detail)."""
    mod = MODULES.get(module_id)
    if not mod:
        return False, "unknown module: %s" % module_id
    if provider not in ROLE_PROVIDERS and provider != mod.get("defaults", {}).get("provider"):
        return False, "unsupported provider: %s" % provider
    if provider in ("deepseek", "openai") and not _model_provider(model) == provider:
        return False, "model %r is not a %s model" % (model, provider)
    if provider in ("deepseek", "openai"):
        if mod["capability"] not in _model_capabilities(model):
            return False, ("model %r does not support capability %s"
                           % (model, mod["capability"]))
    if enabled not in (True, False):
        return False, "enabled must be boolean"
    prev = resolve_module(module_id, workspace)
    if fallback_provider and fallback_model and fallback_provider not in ROLE_PROVIDERS:
        return False, "unsupported fallback provider: %s" % fallback_provider
    settings = load_settings(workspace)
    settings.setdefault("modules", {})
    settings["modules"][module_id] = {
        "provider": provider, "model": model,
        "fallbackProvider": fallback_provider or None,
        "fallbackModel": fallback_model or None,
        "enabled": bool(enabled),
        "updated_at": now_iso(),
    }
    settings["schema_version"] = settings.get("schema_version") or 1
    settings["updated_by"] = "dashboard"
    save_settings(workspace, settings)
    new = resolve_module(module_id, workspace)
    audit_model(workspace, "set", module_id,
                {"provider": prev.get("provider"), "model": prev.get("model")},
                {"provider": new.get("provider"), "model": new.get("model")})
    return True, new


def set_workspace_default(workspace, capability, provider, model,
                          fallback_provider=None, fallback_model=None):
    """Workspace-level default for a capability (layer 2)."""
    cat = load_catalog()
    if capability not in (cat.get("capabilities") or {}):
        return False, "unknown capability: %s" % capability
    settings = load_settings(workspace)
    settings.setdefault("workspace_defaults", {})
    settings["workspace_defaults"][capability] = {
        "provider": provider, "model": model,
        "fallbackProvider": fallback_provider or None,
        "fallbackModel": fallback_model or None,
    }
    settings["schema_version"] = settings.get("schema_version") or 1
    save_settings(workspace, settings)
    audit_model(workspace, "set_workspace_default", capability,
                None, {"provider": provider, "model": model})
    return True, None


def reset_module(workspace, module_id=None):
    """Remove overrides. module_id None => reset everything for workspace."""
    settings = load_settings(workspace)
    if module_id:
        if module_id not in MODULES:
            return {"ok": False, "error": "unknown module: %s" % module_id}
        (settings.get("modules") or {}).pop(module_id, None)
        audit_model(workspace, "reset", module_id, None, None)
    else:
        settings = {"schema_version": 1, "workspace_defaults": {},
                    "modules": {}, "updated_by": "dashboard", "updated_at": now_iso()}
        audit_model(workspace, "reset_all", "*", None, None)
    save_settings(workspace, settings)
    return {"ok": True}


def bulk_apply(workspace, provider, model, module_ids=None):
    """Validate a bulk change target list for text_generation modules.
    module_ids None = all text_generation modules in MODULES."""
    if provider not in ("deepseek", "openai"):
        return {"ok": False, "affected": [],
                "error": "bulk apply supports deepseek/openai providers only"}
    if not _model_provider(model) == provider:
        return {"ok": False, "affected": [],
                "error": "model %r is not a %s model" % (model, provider)}
    affected = [mid for mid in (module_ids or [])
                if MODULES.get(mid, {}).get("capability") == CAP_TEXT] \
        if module_ids else \
        [mid for mid, m in MODULES.items() if m.get("capability") == CAP_TEXT]
    return {"ok": True, "affected": affected}


def apply_bulk(workspace, provider, model, module_ids):
    """Write the bulk change (caller shows confirmation first)."""
    settings = load_settings(workspace)
    settings.setdefault("modules", {})
    for mid in module_ids:
        settings["modules"][mid] = {
            "provider": provider, "model": model,
            "fallbackProvider": None, "fallbackModel": None,
            "enabled": True, "updated_at": now_iso(),
        }
    settings["schema_version"] = settings.get("schema_version") or 1
    save_settings(workspace, settings)
    audit_model(workspace, "bulk_apply", ",".join(module_ids),
                None, {"provider": provider, "model": model})
    return {"ok": True, "applied": len(module_ids)}


# ── execution (single entry point used by migrated modules) ───────────────────

def _import_provider(name):
    global _provider_cache
    try:
        if name not in _provider_cache:
            _provider_cache[name] = importlib.import_module(name)
    except Exception:
        _provider_cache[name] = None
    return _provider_cache[name]


_provider_cache = {}


def execute(module_id, workspace="personal", prompt="", system=None,
            max_tokens=1024, temperature=0.3, timeout=120):
    """One call through the registry, honoring the module's resolved model +
    fallback chain. Returns (ok, text, meta) where meta includes the effective
    provider/model/source/fallback info (same shape modules already consume).
    This is THE single execution entry point for text_generation modules."""
    r = resolve_module(module_id, workspace)
    if not r.get("provider") or r.get("provider") == "unknown":
        return False, "", {"reason": "no backend available", "source": r.get("source"),
                          "provider": None, "model": None}
    if r.get("capability") != CAP_TEXT:
        return False, "", {"reason": "module %s is not text_generation" % module_id,
                          "provider": r.get("provider"), "model": r.get("model")}
    if not r.get("enabled"):
        return False, "", {"reason": "module disabled in settings",
                          "provider": r.get("provider"), "model": r.get("model")}

    try_chain = [(r["provider"], r["model"], "primary")]
    if r.get("fallback") and r["fallback"].get("provider") and r["fallback"].get("model"):
        try_chain.append((r["fallback"]["provider"], r["fallback"]["model"], "fallback"))
    if r.get("emergency"):
        try_chain.append(("ai_call", "sonnet", "emergency"))

    fallback_events = []
    last_reason = None
    for provider, model, leg in try_chain:
        ok, text, meta = _provider_call(provider, model, prompt, system,
                                        max_tokens, temperature, timeout)
        if ok:
            meta["module"] = module_id
            meta["source"] = r["source"]
            meta["provider"] = provider
            meta["model"] = model
            meta["fallback_events"] = fallback_events
            log_call(r["workspace"], module_id,
                     "%s/%s" % (provider, model), "resolved=%s" % r["source"],
                     True, meta.get("tokens"), fallback_events or None,
                     None, meta.get("duration_ms"))
            return ok, text, meta
        fallback_events.append({"from_model": "%s/%s" % (provider, model),
                                "to_model": "next| none",
                                "reason": (meta.get("reason") or "call failed")[:200],
                                "leg": leg})
        last_reason = meta.get("reason") or "call failed"
    log_call(r["workspace"], module_id,
             "%s/%s" % (try_chain[0] if try_chain else ("?", "?")),
             "resolved=%s" % r["source"], False, None,
             fallback_events or None, last_reason[:300], None)
    return False, "", {"reason": last_reason, "provider": r["provider"],
                       "model": r["model"], "source": r["source"],
                       "backend": "none", "fallback_events": fallback_events}


def _provider_call(provider, model, prompt, system, max_tokens, temperature, timeout):
    """Call ONE provider leg. Returns (ok, text, meta). Never raises."""
    m = {"reason": "unknown"}
    if provider == "deepseek":
        ds = _import_provider("deepseek_call")
        if ds is None:
            return False, "", {"reason": "deepseek_call unavailable"}
        joined = (system + "\n\n" + prompt) if system else prompt
        return ds.call(joined, model=model, max_tokens=max_tokens,
                       temperature=temperature, timeout=timeout)
    if provider == "openai":
        oa = _import_provider("openai_call")
        if oa is None:
            return False, "", {"reason": "openai_call unavailable"}
        return oa.call(prompt, model=model, system=system,
                       max_tokens=max_tokens, temperature=temperature,
                       timeout=timeout)
    if provider in ("agy", "claude", "ai_call"):
        ai = _import_provider("ai_call")
        if ai is None:
            return False, "", {"reason": "ai_call unavailable"}
        task = "draft"
        if model and model in AGY_TASKS:
            task = model
        claude_model = model if (provider == "claude" and model in (
            "haiku", "sonnet", "opus")) else None
        ok, text, meta = ai.run(prompt, task=task, model=claude_model,
                                timeout=timeout)
        meta.setdefault("model", model)
        return ok, text, meta
    return False, "", {"reason": "unhandled provider %s" % provider}


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
    p.add_argument("--module", default=None, help="resolve a module with resolve_module")
    args = p.parse_args()

    if args.module:
        print(json.dumps(resolve_module(args.module, args.workspace or "personal"),
                         indent=2, default=str))
        sys.exit(0)

    if args.task_type == "status":
        print(json.dumps(status(), indent=2))
        sys.exit(0)

    r = route_task(args.task_type, complexity=args.complexity,
                   importance=args.importance, context_size=args.context_size,
                   requires_deep_reasoning=args.deep_reasoning,
                   workspace=args.workspace)
    print(json.dumps(r, indent=2))
