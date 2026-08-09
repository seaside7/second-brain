#!/usr/bin/env python3
"""
action_executor.py — Action Layer for the Second Brain.

Receives natural language requests, detects intent, selects skills,
validates parameters, requests approval for write actions, executes,
and logs results.

Usage:
    python action_executor.py "Find my unread emails"
    python action_executor.py "Send Marc an email saying the document is ready"
    python action_executor.py "Create a Trello card for the SSO bug"
    python action_executor.py --auto-approve "List my GitLab MRs"

Architecture:
    Natural language → intent detection → skill selection → parameter extraction
    → approval gate (write actions) → execution → result → log
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / ".agent" / "scripts"
STATE_DIR = REPO_ROOT / "journal" / "state"
ACTION_LOG = STATE_DIR / "action_log.jsonl"

sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(REPO_ROOT / ".agent" / "workspaces"))

from orchestrator import run_skill, SKILL_REGISTRY
from model_router import route
import workspace_resolver as ws

WIB = timezone(timedelta(hours=7))

# UTF-8
try:
    if sys.stdout.encoding != 'utf-8':
        sys.stdout.reconfigure(encoding='utf-8')
    if sys.stderr.encoding != 'utf-8':
        sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

# ── Action Policies ──

POLICY_READ = "read"         # Execute automatically
POLICY_WRITE = "write"       # Require approval
POLICY_DESTRUCTIVE = "destructive"  # Require explicit approval

# Maps skill+action to policy. Default: read (safe)
ACTION_POLICIES = {
    # Gmail
    ("gmail-connector", "list"): POLICY_READ,
    ("gmail-connector", "get"): POLICY_READ,
    ("gmail-connector", "profile"): POLICY_READ,
    ("gmail-connector", "send"): POLICY_WRITE,
    ("gmail-connector", "archive"): POLICY_WRITE,
    # Mattermost
    ("mattermost-connector", "list_teams"): POLICY_READ,
    ("mattermost-connector", "list_channels"): POLICY_READ,
    ("mattermost-connector", "list_dms"): POLICY_READ,
    ("mattermost-connector", "history"): POLICY_READ,
    ("mattermost-connector", "search"): POLICY_READ,
    ("mattermost-connector", "user_info"): POLICY_READ,
    ("mattermost-connector", "post"): POLICY_WRITE,
    # Trello
    ("trello-connector", "list_boards"): POLICY_READ,
    ("trello-connector", "list_lists"): POLICY_READ,
    ("trello-connector", "list_cards"): POLICY_READ,
    ("trello-connector", "get_card"): POLICY_READ,
    ("trello-connector", "my_cards"): POLICY_READ,
    ("trello-connector", "move_card"): POLICY_WRITE,
    ("trello-connector", "comment"): POLICY_WRITE,
    # GitLab
    ("gitlab-connector", "list_groups"): POLICY_READ,
    ("gitlab-connector", "list_projects"): POLICY_READ,
    ("gitlab-connector", "list_issues"): POLICY_READ,
    ("gitlab-connector", "get_issue"): POLICY_READ,
    ("gitlab-connector", "list_mrs"): POLICY_READ,
    ("gitlab-connector", "get_mr"): POLICY_READ,
    ("gitlab-connector", "search"): POLICY_READ,
    ("gitlab-connector", "pipelines"): POLICY_READ,
    ("gitlab-connector", "my_commits_today"): POLICY_READ,
    ("gitlab-connector", "create_issue"): POLICY_WRITE,
    # Meeting Intelligence
    ("meeting-intelligence", "sync"): POLICY_READ,
    ("meeting-intelligence", "list"): POLICY_READ,
    ("meeting-intelligence", "tasks"): POLICY_READ,
    # Knowledge Store
    ("knowledge-store", "search"): POLICY_READ,
    ("knowledge-store", "add"): POLICY_READ,
    ("knowledge-store", "list"): POLICY_READ,
    ("knowledge-store", "status"): POLICY_READ,
    # Dev Tracker
    ("dev-tracker", "status"): POLICY_READ,
    ("dev-tracker", "list"): POLICY_READ,
    ("dev-tracker", "start"): POLICY_WRITE,
    ("dev-tracker", "complete"): POLICY_WRITE,
    # Calendar
    ("google-calendar-connector", "sweep"): POLICY_READ,
    ("google-calendar-connector", "list"): POLICY_READ,
    ("google-calendar-connector", "create"): POLICY_WRITE,
}

# ── Intent Detection (rule-based + LLM fallback) ──

# Keyword → (skill, action, required_params)
INTENT_PATTERNS = [
    # Gmail
    (["unread email", "check email", "list email", "read email", "find email", "search email"],
     "gmail-connector", "list", {}),
    (["send email", "reply email", "email to", "send message to", "email saying", "send an email", "reply to"],
     "gmail-connector", "send", {"to": None, "subject": None, "body": None}),
    # Mattermost
    (["mattermost message", "send mattermost", "message on mattermost"],
     "mattermost-connector", "post", {"channel": None, "text": None}),
    (["mattermost channels", "list mattermost"],
     "mattermost-connector", "list_channels", {}),
    (["mattermost dms", "mattermost direct"],
     "mattermost-connector", "list_dms", {}),
    # Trello
    (["my trello", "trello tasks", "trello cards", "show trello", "list trello cards"],
     "trello-connector", "my_cards", {}),
    (["list trello boards", "trello boards"],
     "trello-connector", "list_boards", {}),
    (["create trello", "add trello", "new trello card"],
     "trello-connector", "comment", {"card_id": None, "text": None}),
    # GitLab
    (["create issue", "gitlab issue", "new issue", "file issue", "report bug"],
     "gitlab-connector", "create_issue", {"project_id": None, "title": None}),
    (["merge request", "list mrs", "my mrs", "open mrs"],
     "gitlab-connector", "list_mrs", {"project_id": None}),
    (["gitlab commits", "my commits", "today commits"],
     "gitlab-connector", "my_commits_today", {}),
    (["gitlab projects", "list projects"],
     "gitlab-connector", "list_projects", {}),
    (["search code", "search gitlab", "find in code"],
     "gitlab-connector", "search", {"query": None}),
    # Meetings
    (["sync meetings", "fetch meetings", "get meetings"],
     "meeting-intelligence", "sync", {}),
    (["list meetings", "recent meetings", "show meetings", "latest meetings", "my meetings"],
     "meeting-intelligence", "list", {}),
    (["meeting tasks", "meeting action items"],
     "meeting-intelligence", "tasks", {}),
    # Knowledge
    (["remember", "store knowledge", "save this"],
     "knowledge-store", "add", {"category": None, "content": None}),
    (["recall", "search knowledge", "what do i know about"],
     "knowledge-store", "search", {"query": None}),
    # Calendar
    (["calendar", "schedule", "my meetings today", "today events"],
     "google-calendar-connector", "sweep", {}),
    # Dev Tracker
    (["start task", "dev start", "begin working"],
     "dev-tracker", "start", {"title": None}),
    (["task status", "current task", "what am i working on"],
     "dev-tracker", "status", {}),
]


def detect_intent_local(text):
    """Rule-based intent detection. Returns (skill, action, params) or None."""
    text_lower = text.lower()
    for keywords, skill, action, required_params in INTENT_PATTERNS:
        for kw in keywords:
            if kw in text_lower:
                return skill, action, dict(required_params)
    return None, None, None


def detect_intent_llm(text):
    """Use LLM for intent detection when rules fail."""
    routing = route("complex_synthesis", text[:200])
    if routing["model"] == "local":
        return None, None, None

    skills_desc = "\n".join(
        f"- {name}: {info['description']} (actions: varies)"
        for name, info in SKILL_REGISTRY.items()
    )

    prompt = f"""You are an action router for a productivity system. Given a user request, determine which skill and action to use.

Available skills:
{skills_desc}

User request: "{text}"

Reply with ONLY a JSON object (no markdown, no explanation):
{{"skill": "skill-name", "action": "action-name", "params": {{"key": "value"}}}}

If you cannot determine the action, reply: {{"skill": null, "action": null, "params": {{}}}}"""

    import subprocess
    try:
        r = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "ai_call.py"),
             "--prompt", prompt, "--model", routing["model"], "--timeout", "30"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=45)
        output = (r.stdout or "").strip()
        # Extract JSON from output
        if "{" in output:
            json_str = output[output.index("{"):output.rindex("}") + 1]
            data = json.loads(json_str)
            return data.get("skill"), data.get("action"), data.get("params", {})
    except Exception:
        pass
    return None, None, None


def detect_intent(text):
    """Detect intent: try rules first, then LLM."""
    skill, action, params = detect_intent_local(text)
    if skill:
        return skill, action, params, "rule"

    skill, action, params = detect_intent_llm(text)
    if skill:
        return skill, action, params or {}, "llm"

    return None, None, None, None


# ── Parameter Extraction ──

def extract_params_from_text(text, required_params):
    """Extract missing parameters from the natural language text.
    Returns (params_dict, missing_list)."""
    params = dict(required_params)
    missing = []

    text_lower = text.lower()

    # Email-specific extraction
    if "to" in params and params["to"] is None:
        # Look for email addresses
        import re
        emails = re.findall(r'[\w\.\-]+@[\w\.\-]+\.\w+', text)
        if emails:
            params["to"] = emails[0]
        # Look for "to NAME" pattern
        elif " to " in text_lower:
            after_to = text[text_lower.index(" to ") + 4:].strip()
            # Take first word(s) before a verb/preposition
            name = after_to.split(" saying ")[0].split(" that ")[0].split(" and ")[0].strip()
            if name and len(name) < 50:
                params["to"] = name

    if "subject" in params and params["subject"] is None:
        # Generate from context
        params["subject"] = text[:60] if len(text) < 60 else text[:57] + "..."

    if "body" in params and params["body"] is None:
        # Look for "saying X" or "that X" patterns
        for marker in ["saying ", "that ", "telling ", ": "]:
            if marker in text_lower:
                idx = text_lower.index(marker) + len(marker)
                params["body"] = text[idx:].strip().rstrip(".")
                break

    if "text" in params and params["text"] is None:
        for marker in ["saying ", "message ", "that ", ": "]:
            if marker in text_lower:
                idx = text_lower.index(marker) + len(marker)
                params["text"] = text[idx:].strip().rstrip(".")
                break

    if "title" in params and params["title"] is None:
        # Use the whole request as title context
        params["title"] = text[:120]

    if "query" in params and params["query"] is None:
        # Use keywords from the request
        stop_words = {"find", "search", "for", "the", "a", "an", "in", "about", "what", "do", "i", "know"}
        words = [w for w in text.split() if w.lower() not in stop_words]
        params["query"] = " ".join(words[-3:]) if words else text

    if "content" in params and params["content"] is None:
        params["content"] = text

    # Check what's still missing
    for k, v in params.items():
        if v is None:
            missing.append(k)

    return params, missing


# ── Approval ──

def get_policy(skill, action):
    """Get the action policy (read/write/destructive)."""
    return ACTION_POLICIES.get((skill, action), POLICY_READ)


def request_approval(skill, action, params):
    """Display the proposed action and ask for approval. Returns True if approved."""
    print("\n" + "=" * 50)
    print("  ACTION REQUIRES APPROVAL")
    print("=" * 50)
    print(f"  Skill:  {skill}")
    print(f"  Action: {action}")
    print(f"  Params:")
    for k, v in params.items():
        if v is not None:
            val = str(v)
            if len(val) > 80:
                val = val[:77] + "..."
            print(f"    {k}: {val}")
    print("=" * 50)

    try:
        answer = input("\n  Approve? [y/N]: ").strip().lower()
        return answer in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


# ── Execution ──

def execute_action(skill, action, params, approved=False):
    """Execute the skill with the given parameters."""
    # Filter out None params
    clean_params = {k: v for k, v in params.items() if v is not None}

    # Only add --approved flag for write actions that support it
    policy = get_policy(skill, action)
    if policy in (POLICY_WRITE, POLICY_DESTRUCTIVE) and approved:
        clean_params["approved"] = ""

    ok, output, meta = run_skill(skill, action=action, **clean_params)
    return ok, output, meta


# ── Logging ──

def log_action(request, skill, action, params, policy, approved, ok, output, method):
    """Append action to the log file."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(WIB).isoformat(timespec="seconds"),
        "workspace": ws.get_active_name(),
        "request": request[:500],
        "skill": skill,
        "action": action,
        "params": {k: str(v)[:200] for k, v in (params or {}).items() if v is not None},
        "policy": policy,
        "approved": approved,
        "success": ok,
        "method": method,
        "output_preview": (output or "")[:200],
    }
    with open(ACTION_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ── Main Pipeline ──

def process_request(text, auto_approve=False):
    """Full pipeline: intent → params → approval → execute → log."""
    print(f"\n  Request: \"{text}\"")
    print(f"  Workspace: {ws.get_active_name()}")
    print()

    # 1. Detect intent
    skill, action, params, method = detect_intent(text)

    if not skill:
        print("  [?] Could not determine what action to take.")
        print("  Try being more specific, e.g.:")
        print("    - 'Find my unread emails'")
        print("    - 'Create a GitLab issue for the login bug'")
        print("    - 'Show my Trello cards'")
        log_action(text, None, None, None, None, False, False, "intent_failed", method)
        return False

    print(f"  [>] Detected: {skill} → {action} (via {method})")

    # 2. Extract/validate parameters
    if params:
        params, missing = extract_params_from_text(text, params)
        if missing:
            print(f"  [?] Missing required parameters: {', '.join(missing)}")
            for m in missing:
                try:
                    val = input(f"    {m}: ").strip()
                    if val:
                        params[m] = val
                except (EOFError, KeyboardInterrupt):
                    print("  [X] Cancelled.")
                    return False

    # 3. Check policy
    policy = get_policy(skill, action)
    approved = True

    if policy in (POLICY_WRITE, POLICY_DESTRUCTIVE) and not auto_approve:
        approved = request_approval(skill, action, params or {})
        if not approved:
            print("  [X] Action rejected by user.")
            log_action(text, skill, action, params, policy, False, False, "rejected", method)
            return False

    # 4. Execute
    print(f"  [*] Executing {skill} {action}...")
    ok, output, meta = execute_action(skill, action, params or {}, approved=approved)

    # 5. Display result
    if ok:
        print(f"  [OK] Success.")
        if output:
            # Show first 500 chars of output
            preview = output[:500]
            if len(output) > 500:
                preview += f"\n  ... ({len(output)} chars total)"
            print(f"\n{preview}")
    else:
        print(f"  [FAIL] {meta.get('reason', 'unknown error')}")
        if output:
            print(f"  Output: {output[:200]}")

    # 6. Log
    log_action(text, skill, action, params, policy, approved, ok, output[:200] if output else "", method)

    return ok


# ── CLI ──

def main():
    p = argparse.ArgumentParser(description="Second Brain Action Executor")
    p.add_argument("request", nargs="?", help="Natural language request")
    p.add_argument("--auto-approve", action="store_true", dest="auto_approve",
                   help="Skip approval for write actions (use with caution)")
    p.add_argument("--log", action="store_true", help="Show recent action log")
    args = p.parse_args()

    if args.log:
        if ACTION_LOG.exists():
            lines = ACTION_LOG.read_text(encoding="utf-8").strip().split("\n")
            print(f"Recent actions ({len(lines)} total):\n")
            for line in lines[-10:]:
                entry = json.loads(line)
                status = "OK" if entry["success"] else "FAIL"
                print(f"  [{entry['timestamp'][-8:]}] [{status}] {entry.get('skill','?')}:{entry.get('action','?')} "
                      f"← \"{entry['request'][:50]}\"")
        else:
            print("No action log yet.")
        return

    if not args.request:
        # Interactive mode
        print("Second Brain Action Executor")
        print("Type a request (or 'quit' to exit):\n")
        while True:
            try:
                text = input(">> ").strip()
                if text.lower() in ("quit", "exit", "q"):
                    break
                if text:
                    process_request(text, auto_approve=args.auto_approve)
                    print()
            except (EOFError, KeyboardInterrupt):
                break
    else:
        ok = process_request(args.request, auto_approve=args.auto_approve)
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
