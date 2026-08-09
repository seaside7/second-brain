#!/usr/bin/env python3
"""
orchestrator.py — Second Brain dispatch layer.

Routes a task to the right skill + model tier, then executes it.
Connects model_router (which model?) → ai_call (run the model) → skill scripts.

Usage:
  python .agent/scripts/orchestrator.py --task "analyze last week's trades"
  python .agent/scripts/orchestrator.py --skill timesheet-writer --action append --project ABNJ --desc "Fixed auth bug"
  python .agent/scripts/orchestrator.py --skill knowledge-store --action search --query "JWT"
"""

import json
import subprocess
import sys
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / ".agent" / "scripts"
SKILLS_DIR = REPO_ROOT / ".agent" / "skills"

sys.path.insert(0, str(SCRIPTS_DIR))
from model_router import route, status as router_status

# ── Skill registry ──
# Maps skill names to their script entry points and task categories for routing.
SKILL_REGISTRY = {
    "dev-tracker": {
        "script": "skills/dev-tracker/scripts/dev_tracker.py",
        "category": "code_review",
        "description": "Start/log/complete dev task sessions",
        "cli_style": "positional",
    },
    "timesheet-writer": {
        "script": "skills/timesheet-writer/scripts/timesheet_writer.py",
        "category": "draft_boilerplate",
        "description": "Append timesheet entries to Google Sheets",
        "cli_style": "positional",
    },
    "knowledge-store": {
        "script": "skills/knowledge-store/scripts/knowledge_store.py",
        "category": "simple_lookup",
        "description": "Store and search long-term knowledge",
        "cli_style": "positional",
    },
    "meeting-intelligence": {
        "script": "skills/meeting-intelligence/scripts/meeting_engine.py",
        "category": "complex_synthesis",
        "description": "Extract tasks/decisions from meeting transcripts",
        "cli_style": "positional",
    },
    "gitlab-connector": {
        "script": "skills/gitlab-connector/scripts/gitlab_client.py",
        "category": "simple_lookup",
        "description": "Query GitLab: commits, MRs, pipelines, issues",
        "cli_style": "flag",
    },
    "gmail-connector": {
        "script": "skills/gmail-connector/gmail_manager.py",
        "category": "simple_lookup",
        "description": "Read/search/send Gmail",
        "cli_style": "positional",
    },
    "fathom-connector": {
        "script": "skills/fathom-connector/scripts/fathom_client.py",
        "category": "simple_lookup",
        "description": "Sync Fathom meeting recordings",
        "cli_style": "flag",
    },
    "google-calendar-connector": {
        "script": "skills/google-calendar-connector/gcal_manager.py",
        "category": "simple_lookup",
        "description": "Read/manage Google Calendar",
        "cli_style": "positional",
    },
    "mattermost-connector": {
        "script": "skills/mattermost-connector/scripts/mattermost_client.py",
        "category": "simple_lookup",
        "description": "Read/send Mattermost messages",
        "cli_style": "flag",
    },
    "trello-connector": {
        "script": "skills/trello-connector/scripts/trello_client.py",
        "category": "simple_lookup",
        "description": "Read Trello cards and boards",
        "cli_style": "flag",
    },
}


def list_skills():
    """Return registered skills with their status (available/missing)."""
    result = []
    for name, info in SKILL_REGISTRY.items():
        script_path = REPO_ROOT / ".agent" / info["script"]
        result.append({
            "name": name,
            "category": info["category"],
            "description": info["description"],
            "available": script_path.is_file(),
            "path": str(script_path),
        })
    return result


def find_skill(name):
    """Find a skill by name (exact or partial match). Returns (name, info) or (None, None)."""
    if name in SKILL_REGISTRY:
        return name, SKILL_REGISTRY[name]
    # Partial match
    matches = [k for k in SKILL_REGISTRY if name.lower() in k.lower()]
    if len(matches) == 1:
        return matches[0], SKILL_REGISTRY[matches[0]]
    return None, None


def run_skill(skill_name, action=None, **kwargs):
    """
    Run a registered skill directly (no LLM needed).
    Returns (success, output_text, meta).

    Handles two CLI styles:
      positional: script.py <action> --key value
      flag:       script.py --action <action> --key value
    """
    skill_key, info = find_skill(skill_name)
    if not skill_key:
        return False, f"Unknown skill: {skill_name}", {"reason": "unknown_skill"}

    script_rel = info["script"]
    script_path = REPO_ROOT / ".agent" / script_rel

    if not script_path.is_file():
        return False, f"Skill script not found: {script_rel}", {"reason": "script_missing"}

    cli_style = info.get("cli_style", "positional")

    # Build command
    cmd = [sys.executable, str(script_path)]
    if action:
        if cli_style == "flag":
            cmd.extend(["--action", action])
        else:
            cmd.append(action)

    # Add keyword args as --key value pairs
    for k, v in kwargs.items():
        if v is not None:
            flag = f"--{k.replace('_', '-')}"
            if v == "":
                # Boolean flag (e.g. --approved with no value)
                cmd.append(flag)
            else:
                cmd.append(flag)
                cmd.append(str(v))

    try:
        result = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120)
        success = result.returncode == 0
        output = (result.stdout or "").strip()
        # Include stderr for debugging but prioritize stdout
        if not output and result.stderr:
            output = result.stderr.strip()
        meta = {
            "skill": skill_key,
            "category": info["category"],
            "returncode": result.returncode,
            "script": str(script_path),
        }
        return success, output, meta
    except subprocess.TimeoutExpired:
        return False, "Skill timed out after 120s", {"reason": "timeout"}
    except Exception as e:
        return False, str(e), {"reason": "exception"}


def run_with_model(skill_prompt, task_category="complex_synthesis", model=None, timeout=180):
    """
    Run a task using an LLM model (via ai_call.py).
    Routes to the right model tier, then executes.

    Returns (success, output_text, meta).
    """
    # Route to determine model tier
    routing = route(task_category, skill_prompt[:200])
    target_model = model or routing["model"]

    if target_model == "local":
        return False, f"Task '{task_category}' routed to local — no LLM call needed. Use run_skill() for local scripts.", {"routed": "local"}

    # Use ai_call.py to execute
    ai_call_script = SCRIPTS_DIR / "ai_call.py"

    cmd = [
        sys.executable, str(ai_call_script),
        "--prompt", skill_prompt,
        "--model", target_model,
        "--timeout", str(timeout),
    ]

    try:
        result = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=timeout + 30)
        success = result.returncode == 0
        output = (result.stdout or "").strip()
        meta = {
            "model": target_model,
            "category": task_category,
            "cost": routing["estimated_cost"],
            "monthly_spend": routing["monthly_spend"],
            "returncode": result.returncode,
        }
        # Parse fallback sentinel
        if result.returncode == 3:
            meta["fallback"] = json.loads(output) if output else {"note": "unknown fallback"}
            return False, output, meta
        return success, output, meta
    except subprocess.TimeoutExpired:
        return False, "", {"reason": "timeout", "model": target_model}
    except Exception as e:
        return False, str(e), {"reason": "exception", "model": target_model}


def dispatch(task_description, auto_route=True):
    """
    Main entry point. Given a task description, decide whether to run a skill
    directly or invoke an LLM, then do it.

    Returns (success, output, meta).
    """
    if not task_description.strip():
        return False, "No task provided", {}

    # 1. Check if this maps to a registered skill
    skill_key, info = find_skill(task_description.split()[0]) if task_description else (None, None)
    category = info["category"] if info else "complex_synthesis"

    # 2. Route to model tier
    routing = route(category, task_description[:200])

    # 3. If local-only task and skill exists, run skill directly
    if routing["model"] == "local" and skill_key:
        return run_skill(skill_key, action=task_description.split()[1] if len(task_description.split()) > 1 else None)

    # 4. Otherwise, use LLM
    return run_with_model(task_description, task_category=category)


# ── CLI ──

def cmd_status():
    """Show orchestrator status."""
    rs = router_status()
    skills = list_skills()
    available = [s for s in skills if s["available"]]
    unavailable = [s for s in skills if not s["available"]]

    print("=== Second Brain Orchestrator ===")
    print(f"Model: default={rs['default']} | monthly spend=${rs['monthly_spend']:.3f}/{rs['limit']}")
    print(f"Skills: {len(available)} available, {len(unavailable)} unavailable")
    print()
    print("Available skills:")
    for s in available:
        print(f"  {s['name']:<25} [{s['category']:<20}] {s['description']}")
    if unavailable:
        print("\nMissing scripts:")
        for s in unavailable:
            print(f"  {s['name']:<25} (expected: {s['path']})")


def cmd_run(args):
    """Run a task."""
    if args.skill:
        # Direct skill invocation
        kwargs = {}
        for kv in args.kwargs or []:
            if "=" in kv:
                k, v = kv.split("=", 1)
                kwargs[k] = v
        ok, out, meta = run_skill(args.skill, action=args.action, **kwargs)
    else:
        # Task-based dispatch
        ok, out, meta = dispatch(args.task) if args.task else (False, "No task specified", {})

    print(json.dumps({"ok": ok, "output": out[:2000], "meta": meta}, indent=2))
    sys.exit(0 if ok else 1)


def main():
    import argparse
    p = argparse.ArgumentParser(description="Second Brain Orchestrator")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("status", help="Show orchestrator status")

    run_p = sub.add_parser("run", help="Run a task or skill")
    run_p.add_argument("--task", help="Task description (natural language)")
    run_p.add_argument("--skill", help="Skill name to run directly")
    run_p.add_argument("--action", help="Action for the skill")
    run_p.add_argument("--kwargs", nargs="*", help="Additional args as key=value")

    args = p.parse_args()

    if args.cmd == "status" or not args.cmd:
        cmd_status()
    elif args.cmd == "run":
        cmd_run(args)
    else:
        p.print_help()


if __name__ == "__main__":
    main()