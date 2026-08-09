#!/usr/bin/env python3
"""
deepseek_call.py — Call DeepSeek API directly.

Used by the action executor and orchestrator when model_router routes to "deepseek".
Uses the OpenAI-compatible API at api.deepseek.com.

Usage:
    python deepseek_call.py --prompt "Classify this task"
    python deepseek_call.py --prompt "..." --model deepseek-chat
    python deepseek_call.py --prompt-file input.txt
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

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1/chat/completions"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_TIMEOUT = 30


def call(prompt, model=None, max_tokens=1024, temperature=0.3, timeout=DEFAULT_TIMEOUT):
    """Call DeepSeek API. Returns (ok, text, meta)."""
    if not DEEPSEEK_API_KEY:
        return False, "", {"reason": "DEEPSEEK_API_KEY not set in .env"}

    body = json.dumps({
        "model": model or DEFAULT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
    }

    req = urllib.request.Request(DEEPSEEK_BASE_URL, data=body, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            choices = data.get("choices", [])
            if choices:
                text = choices[0].get("message", {}).get("content", "").strip()
                usage = data.get("usage", {})
                meta = {
                    "model": data.get("model", model or DEFAULT_MODEL),
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
    p = argparse.ArgumentParser(description="DeepSeek API caller")
    p.add_argument("--prompt", help="Prompt text")
    p.add_argument("--prompt-file", dest="prompt_file", help="Read prompt from file")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--max-tokens", type=int, default=1024, dest="max_tokens")
    p.add_argument("--temperature", type=float, default=0.3)
    args = p.parse_args()

    if args.prompt_file:
        prompt = open(args.prompt_file, encoding="utf-8").read()
    elif args.prompt:
        prompt = args.prompt
    else:
        prompt = sys.stdin.read()

    ok, text, meta = call(prompt, model=args.model, max_tokens=args.max_tokens,
                          temperature=args.temperature)
    if ok:
        print(text)
    else:
        print(f"[ERROR] {meta.get('reason', 'unknown')}", file=sys.stderr)
        sys.exit(1)
