#!/usr/bin/env python3
"""
inbox_sweep.py — Sweep all connected sources for the active workspace,
deduplicate against task store, classify priority, and surface summary.

Routes through orchestrator -> model_router for model-tier decisions.
"""

import json
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / ".agent" / "scripts"

# UTF-8 on Windows
try:
    if sys.stdout.encoding != 'utf-8':
        sys.stdout.reconfigure(encoding='utf-8')
    if sys.stderr.encoding != 'utf-8':
        sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

sys.path.insert(0, str(SCRIPTS_DIR))
from orchestrator import run_skill, route
from task_store import append_task, get_tasks, count_by_status

WIB = timezone(timedelta(hours=7))

# — Source sweeps (actions match actual CLI args) —

SOURCES = {
    "mattermost": {
        "skill": "mattermost-connector",
        "action": "list_dms",
        "source_name": "mattermost",
        "priority": "high",
    },
    "trello": {
        "skill": "trello-connector",
        "action": "my_cards",
        "source_name": "trello",
        "priority": "medium",
    },
    "gmail": {
        "skill": "gmail-connector",
        "action": "list",
        "extra_args": {"query": "is:unread", "limit": "10"},
        "source_name": "gmail",
        "priority": "medium",
    },
    "gitlab": {
        "skill": "gitlab-connector",
        "action": "--action",
        "extra_args": {"action": "my_commits_today"},
        "source_name": "gitlab",
        "priority": "low",
    },
    "meetings": {
        "skill": "meeting-intelligence",
        "action": "list",
        "source_name": "meeting",  # task_store uses singular
        "priority": "high",
    },
}


def sweep_source(name, config):
    """Sweep one source via orchestrator."""
    print(f"  [{name}] sweeping...", end=" ", flush=True)

    ok, out, meta = run_skill(config["skill"], action=config["action"],
                             **config.get("extra_args", {}))

    if not ok:
        reason = meta.get('reason', 'failed')
        print(f"[WARN] {reason}")
        return []

    # Parse output using source-specific normalizer
    parser = SOURCE_PARSERS.get(name, _parse_generic)
    items = parser(out, config)

    print(f"[OK] {len(items)} items")
    return items


# ── Source-specific parsers ──
# Each parser takes raw stdout and returns a list of normalized task dicts:
# {"title": str, "description": str, "priority": str, "source_ref": str, "url": str}

def _parse_gmail(out, config):
    """Parse gmail list output. Format: '- [MSG_ID] From: X | Subject: Y | Date: Z'"""
    items = []
    for line in out.split("\n"):
        line = line.strip()
        if not line or not line.startswith("- ["):
            continue
        # Extract: - [MSG_ID] From: sender | Subject: subject | Date: date
        try:
            # Remove leading "- "
            rest = line[2:]
            # Extract ID
            id_end = rest.index("]")
            msg_id = rest[1:id_end]
            rest = rest[id_end + 2:]  # skip "] "

            parts = rest.split(" | ")
            sender = parts[0].replace("From: ", "") if len(parts) > 0 else ""
            subject = parts[1].replace("Subject: ", "") if len(parts) > 1 else ""
            date = parts[2].replace("Date: ", "") if len(parts) > 2 else ""

            if not subject or subject == "(No Subject)":
                continue

            items.append({
                "title": subject,
                "description": f"From: {sender} ({date})",
                "priority": "unknown",
                "source_ref": msg_id,
                "url": f"https://mail.google.com/mail/u/0/#inbox/{msg_id}",
            })
        except (ValueError, IndexError):
            continue
    return items


def _parse_mattermost(out, config):
    """Parse mattermost list_dms output. Format: '  label - ID: xxx (DM|Group)'"""
    items = []
    for line in out.split("\n"):
        line = line.strip()
        if not line or line.startswith("[") or line.startswith("Found"):
            continue
        # Format: "label - ID: channel_id (DM|Group)"
        if " - ID: " in line:
            parts = line.split(" - ID: ")
            label = parts[0].strip()
            channel_id = parts[1].split(" ")[0] if len(parts) > 1 else ""
            # DM channel names aren't tasks themselves; skip them
            # Mattermost DM list is not actionable — would need history reading
            # For now, just record that the channel exists
            if label and not label.startswith("Found"):
                items.append({
                    "title": f"Unread DM: {label}",
                    "description": f"Mattermost channel {channel_id}",
                    "priority": "medium",
                    "source_ref": channel_id,
                    "url": "",
                })
    return items


def _parse_trello(out, config):
    """Parse trello my_cards output. Format: '  Card Name\\n       ID: xxx  labels: ...'"""
    items = []
    lines = out.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        # Skip header/count lines
        if not line or line.startswith("[") or line.startswith("Found"):
            i += 1
            continue
        # Card title line (not indented with ID:)
        if line and "ID:" not in line and not line.startswith("labels:"):
            title = line
            card_id = ""
            labels = ""
            due = ""
            # Next line may have ID and metadata
            if i + 1 < len(lines):
                meta_line = lines[i + 1].strip()
                if "ID:" in meta_line:
                    # Extract ID
                    if "ID: " in meta_line:
                        card_id = meta_line.split("ID: ")[1].split(" ")[0].strip()
                    if "labels:" in meta_line:
                        labels = meta_line.split("labels:")[1].strip()
                    if "due:" in meta_line:
                        due = meta_line.split("due:")[1].strip()
                    i += 1  # skip the meta line

            if title:
                desc_parts = []
                if labels and labels != "-":
                    desc_parts.append(f"Labels: {labels}")
                if due:
                    desc_parts.append(f"Due: {due}")

                items.append({
                    "title": title,
                    "description": "; ".join(desc_parts) if desc_parts else "",
                    "priority": "high" if due else "medium",
                    "source_ref": card_id,
                    "url": f"https://trello.com/c/{card_id}" if card_id else "",
                })
        i += 1
    return items


def _parse_gitlab(out, config):
    """Parse gitlab my_commits_today output (JSON mode preferred, fallback to text)."""
    # Try JSON first
    try:
        data = json.loads(out)
        if isinstance(data, list):
            items = []
            for project_data in data:
                project = project_data.get("project", "")
                for commit in project_data.get("commits", []):
                    title = commit.get("title", commit.get("message", "")).split("\n")[0]
                    items.append({
                        "title": f"[{project.split('/')[-1]}] {title}",
                        "description": f"Commit in {project}",
                        "priority": "low",
                        "source_ref": commit.get("short_id", commit.get("id", "")[:8]),
                        "url": commit.get("web_url", ""),
                    })
            return items
    except json.JSONDecodeError:
        pass

    # Fallback: text output
    items = []
    for line in out.split("\n"):
        line = line.strip()
        if not line or line.startswith("[") or line.startswith("Found") or "commit" not in line.lower():
            continue
        # Format: "    abc1234 - commit message"
        if " - " in line:
            parts = line.split(" - ", 1)
            short_id = parts[0].strip()
            message = parts[1].strip() if len(parts) > 1 else ""
            if message:
                items.append({
                    "title": message,
                    "description": f"Commit {short_id}",
                    "priority": "low",
                    "source_ref": short_id,
                    "url": "",
                })
    return items


def _parse_meetings(out, config):
    """Parse meeting-intelligence list output."""
    # Meeting tasks are already in the universal task store via meeting_engine.py sync
    # This parser handles the `list` command text output
    items = []
    for line in out.split("\n"):
        line = line.strip()
        if not line or line.startswith("[") or "No meetings" in line:
            continue
        # Format: "  [2026-08-07] Meeting Title (source) duration"
        if line.startswith("[") or ("]" in line and "(" in line):
            try:
                # Extract date and title
                bracket_end = line.index("]")
                rest = line[bracket_end + 2:].strip()
                # Remove trailing (source) and duration
                if "(" in rest:
                    title = rest[:rest.rindex("(")].strip()
                else:
                    title = rest
                if title:
                    items.append({
                        "title": f"Meeting: {title}",
                        "description": "Review meeting notes and action items",
                        "priority": "medium",
                        "source_ref": "",
                        "url": "",
                    })
            except (ValueError, IndexError):
                continue
    return items


def _parse_generic(out, config):
    """Last resort: try JSON, then skip unparseable output."""
    try:
        data = json.loads(out)
        if isinstance(data, list):
            return [{"title": item.get("title", str(item)[:100]),
                     "description": item.get("description", ""),
                     "priority": "unknown",
                     "source_ref": item.get("id", ""),
                     "url": item.get("url", "")} for item in data if isinstance(item, dict)]
    except json.JSONDecodeError:
        pass
    # Don't create tasks from unparseable text — return empty
    return []


SOURCE_PARSERS = {
    "gmail": _parse_gmail,
    "mattermost": _parse_mattermost,
    "trello": _parse_trello,
    "gitlab": _parse_gitlab,
    "meetings": _parse_meetings,
}


def classify_item(raw_text):
    """Classify priority using orchestrator routing."""
    routing = route("complex_synthesis", raw_text[:200])

    if routing["model"] == "local":
        t = raw_text.lower()
        if any(kw in t for kw in ["urgent", "asap", "production", "down"]):
            return "high"
        if any(kw in t for kw in ["qa", "test", "review"]):
            return "medium"
        return "unknown"

    prompt = f"""Classify this task item by priority. Reply with ONLY one word: high, medium, or low.

Item: {raw_text[:500]}

Priority:"""

    try:
        r = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "ai_call.py"),
             "--prompt", prompt, "--model", routing["model"], "--timeout", "30"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=45)
        answer = (r.stdout or "").strip().lower()
        for p in ["high", "medium", "low"]:
            if p in answer:
                return p
        return "unknown"
    except Exception:
        return "unknown"


def dedupe_and_store(items, source_name):
    """Deduplicate and append to task store."""
    new_count = 0
    skip_count = 0

    for item in items:
        title = item.get("title", "").strip()
        if not title:
            continue

        result = append_task({
            "workspace": "catalyze",
            "source": source_name,
            "title": title,
            "description": item.get("description", ""),
            "priority": item.get("priority", "unknown"),
            "source_ref": item.get("url", item.get("source_ref", "")),
            "source_id": item.get("source_ref", ""),
            "confidence": 0.7,
        })
        if result:
            new_count += 1
        else:
            skip_count += 1

    print(f"  [{source_name}] {new_count} new, {skip_count} skipped")
    return new_count, skip_count


# — Main —

def main():
    now = datetime.now(WIB)
    print(f"=== Inbox Sweep — {now.strftime('%Y-%m-%d %H:%M WIB')} ===")
    print()

    counts = count_by_status("catalyze")
    total = sum(counts.values())
    print(f"Task store: {total} total "
          f"({counts.get('new', 0)} new, {counts.get('open', 0)} open, "
          f"{counts.get('in_progress', 0)} in progress)")
    print()

    total_new = 0
    total_skip = 0

    for name, cfg in SOURCES.items():
        print(f"--- {name.upper()} ---")
        try:
            items = sweep_source(name, cfg)
            if items:
                src_name = cfg.get("source_name", name)
                new, skip = dedupe_and_store(items, src_name)
                total_new += new
                total_skip += skip
        except Exception as e:
            print(f"  [{name}] ERROR: {e}")
        print()

    print(f"=== Sweep Complete ===")
    print(f"New tasks: {total_new}")
    print(f"Duplicates skipped: {total_skip}")
    print(f"Sources scanned: {len(SOURCES)}")

    urgent = [t for t in get_tasks(workspace="catalyze", status=["new", "open"], limit=20)
              if t.get("priority") == "high"]
    if urgent:
        print(f"\n>> {len(urgent)} HIGH PRIORITY items:")
        for t in urgent[:5]:
            print(f"  [{t['source']}] {t['title'][:80]}")

    return total_new


if __name__ == "__main__":
    sys.exit(main() or 0)