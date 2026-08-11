#!/usr/bin/env python3
"""
openai_call.py — Call OpenAI API directly.

The escalation layer of the Second Brain model router. Used when the model
router sends a task to the OpenAI provider (moderate/complex/strategic
reasoning). Mirrors the deepseek_call.py contract: returns (ok, text, meta).

Model IDs are tiered and env-overridable:
  OPENAI_MODEL_LUNA   (default gpt-5.6-luna)   cheap / cost-sensitive
  OPENAI_MODEL_TERRA  (default gpt-5.6-terra)  medium reasoning
  OPENAI_MODEL_SOL    (default gpt-5.6-sol)    high / strategic

Usage:
    python openai_call.py --prompt "Classify this task"
    python openai_call.py --prompt "..." --model gpt-5.6-terra
    python openai_call.py --prompt-file input.txt --tier medium
"""
import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(str(REPO_ROOT / ".env"))

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.environ.get(
    "OPENAI_BASE_URL", "https://api.openai.com/v1/chat/completions")
DEFAULT_MODEL = os.environ.get("OPENAI_MODEL_LUNA", "gpt-5.6-luna")
TIER_MODELS = {
    "low": os.environ.get("OPENAI_MODEL_LUNA", "gpt-5.6-luna"),
    "medium": os.environ.get("OPENAI_MODEL_TERRA", "gpt-5.6-terra"),
    "high": os.environ.get("OPENAI_MODEL_SOL", "gpt-5.6-sol"),
}
DEFAULT_TIMEOUT = 60


def resolve_model(model=None, tier=None):
    """Resolve a concrete model id from an explicit model, a tier name, or the default."""
    if model:
        return model
    if tier and tier in TIER_MODELS:
        return TIER_MODELS[tier]
    return DEFAULT_MODEL


def call(prompt, model=None, tier=None, max_tokens=1024, temperature=0.3,
         timeout=DEFAULT_TIMEOUT, system=None):
    """Call OpenAI API. Returns (ok, text, meta)."""
    if not OPENAI_API_KEY:
        return False, "", {"reason": "OPENAI_API_KEY not set in .env"}

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    model_id = resolve_model(model, tier)
    is_reasoning = model_id.startswith("gpt-5") or model_id.startswith("o")
    body = {
        "model": model_id,
        "messages": messages,
    }
    if is_reasoning:
        body["max_completion_tokens"] = max_tokens
    else:
        body["max_tokens"] = max_tokens
        body["temperature"] = temperature
    body = json.dumps(body).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENAI_API_KEY}",
    }

    req = urllib.request.Request(OPENAI_BASE_URL, data=body, headers=headers,
                                 method="POST")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            choices = data.get("choices", [])
            if choices:
                text = choices[0].get("message", {}).get("content", "").strip()
                usage = data.get("usage", {})
                meta = {
                    "model": data.get("model", resolve_model(model, tier)),
                    "input_tokens": usage.get("prompt_tokens", 0),
                    "output_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }
                return True, text, meta
            return False, "", {"reason": "empty response"}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:300]
        except Exception:
            pass
        return False, "", {"reason": f"HTTP {e.code}: {detail}"}
    except Exception as e:
        return False, "", {"reason": str(e)}


# CLI
if __name__ == "__main__":
    p = argparse.ArgumentParser(description="OpenAI API caller (escalation layer)")
    p.add_argument("--prompt", help="Prompt text")
    p.add_argument("--prompt-file", dest="prompt_file", help="Read prompt from file")
    p.add_argument("--model", default=None)
    p.add_argument("--tier", choices=["low", "medium", "high"], default=None)
    p.add_argument("--system", default=None)
    p.add_argument("--max-tokens", type=int, default=1024, dest="max_tokens")
    p.add_argument("--temperature", type=float, default=0.3)
    args = p.parse_args()

    if args.prompt_file:
        prompt = open(args.prompt_file, encoding="utf-8").read()
    elif args.prompt:
        prompt = args.prompt
    else:
        prompt = sys.stdin.read()

    ok, text, meta = call(prompt, model=args.model, tier=args.tier,
                          max_tokens=args.max_tokens, temperature=args.temperature,
                          system=args.system)
    if ok:
        print(text)
    else:
        print(f"[ERROR] {meta.get('reason', 'unknown')}", file=sys.stderr)
        sys.exit(1)
