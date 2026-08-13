#!/usr/bin/env python3
"""Shared helpers for the local meeting note-taker (recorder / transcribe / watcher).

Platform detection is delegated to .agent/scripts/harness_config.py, which caches
the output of detect_platform.sh. This file used to keep its own Python copy of
that table; two implementations of one rule is one too many.
"""
import json
import os
import platform
import re
import shutil
import sys

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
# PSB_REPO_ROOT lets tests point the recorder stack at a scratch repo (registry,
# Clients/, data/ all resolve there). Unset in production: repo root = parent of
# meeting-recorder/. Prefer --import-style over modifying files below this line.
REPO_ROOT = os.environ.get("PSB_REPO_ROOT") or os.path.dirname(MODULE_DIR)
CONFIG_PATH = os.path.join(MODULE_DIR, "config.json")

# Appended, not inserted at 0, so this never shadows a module the recorder
# scripts import from their own directory.
_AGENT_SCRIPTS = os.path.join(REPO_ROOT, ".agent", "scripts")
if _AGENT_SCRIPTS not in sys.path:
    sys.path.append(_AGENT_SCRIPTS)
try:
    import harness_config
except Exception:  # absent or unimportable -> the inline fallback below takes over
    harness_config = None

# Cron runs with a minimal PATH that excludes ~/.local/bin, so a bare "ffmpeg"
# in config resolves fine in an interactive shell and dies under cron with
# FileNotFoundError. Search these before giving up.
FFMPEG_FALLBACK_DIRS = (
    os.path.expanduser("~/.local/bin"),
    "/usr/local/bin",
    "/usr/bin",
    "/opt/homebrew/bin",
)

def detect_platform():
    """Return macos | wsl | windows, matching config.json's `machines` keys.

    Asks harness_config first so there is one platform table in the repo. The
    inline table stays as a fallback only: the recorder runs under cron and must
    not fail to pick a machine profile because a helper module moved.

    Must stay on harness_config's platform-only path (platform_facts, which
    platform() also wraps) and never on load(): load() resolves the RUNTIME too, and
    outside claude-code that shells into `agy models` behind an 8s timeout,
    measured at 6.9s. This function only ever needed the OS name, and the
    recorder calls it on every cron tick."""
    if harness_config is not None:
        try:
            plat = harness_config.platform_facts().get("platform")
            if plat in ("macos", "wsl", "windows"):
                return plat
        except Exception:
            pass
    sysname = platform.system()
    if sysname == "Darwin":
        return "macos"
    if sysname == "Linux":
        return "wsl"      # the owner's Linux is always WSL
    return "windows"

def resolve_ffmpeg(value):
    """Turn whatever config says into an absolute, existing ffmpeg path.

    Accepts an absolute path, a bare name, or nothing. Returns the input
    unchanged when it cannot do better, so callers still fail loudly rather
    than silently transcoding with the wrong binary."""
    value = (value or "ffmpeg").strip()
    if os.sep in value:
        expanded = os.path.expanduser(value)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded
        value = os.path.basename(expanded)
    found = shutil.which(value)
    if found:
        return found
    for d in FFMPEG_FALLBACK_DIRS:
        cand = os.path.join(d, value)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return value

def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    plat = detect_platform()
    machine = dict(cfg.get("machines", {}).get(plat, {}))
    if machine.get("recordings_dir"):
        machine["recordings_dir"] = os.path.expanduser(machine["recordings_dir"])
    if machine.get("whispercpp_model"):
        machine["whispercpp_model"] = os.path.expanduser(machine["whispercpp_model"])
    machine["ffmpeg"] = resolve_ffmpeg(machine.get("ffmpeg"))
    cfg["platform"] = plat
    cfg["machine"] = machine
    return cfg

def slugify(title):
    slug = re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_")
    return slug[:60] or "meeting"

# Workspace scoping for the recorder stack (single source of truth - watcher,
# v0-storage, v0-index and the smoke test all read these). `workspace` is
# stamped into every registry entry so a personal recording can never surface
# in the samudera scope (enforced in storage, indexing, and dashboard).
# None = the legacy on-machine pipeline (client "Work", existing behavior).
WORKSPACE_CLIENT = {
    None: "Work",
    "personal": "Personal",
    "samudera": "Samudera",
    "catalyze": "Work",
}
WORKSPACE_CALENDAR_PROFILE = {
    None: "work",
    "personal": "personal",
    "samudera": "samudera",
    "catalyze": "work",
}

def workspace_client(workspace):
    return WORKSPACE_CLIENT.get(workspace, "Personal")

def workspace_calendar_profile(workspace):
    return WORKSPACE_CALENDAR_PROFILE.get(workspace, "personal")

def fmt_ts(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

def parse_json_tail(text):
    """gcal_manager prints a 'Fetching events...' line before the JSON on
    stdout; parse from the first [ or { onward."""
    for i, ch in enumerate(text):
        if ch in "[{":
            return json.loads(text[i:])
    raise ValueError("no JSON found in output")

def load_gemini_key():
    """Reuse the Gemini API key from the gemini-image skill (metered, the owner's)."""
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key
    env_path = os.path.join(REPO_ROOT, ".agent", "skills", "gemini-image", "token.env")
    if os.path.exists(env_path):
        for line in open(env_path, encoding="utf-8"):
            line = line.strip()
            if line.startswith("GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip()
    sys.exit("ERROR: no GEMINI_API_KEY (env or .agent/skills/gemini-image/token.env)")

def load_openai_key():
    """OpenAI API key from the root .env, the same source openai_call.py uses."""
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key
    env_path = os.path.join(REPO_ROOT, ".env")
    if os.path.exists(env_path):
        for line in open(env_path, encoding="utf-8"):
            line = line.strip()
            if line.startswith("OPENAI_API_KEY="):
                return line.split("=", 1)[1].strip()
    sys.exit("ERROR: no OPENAI_API_KEY (env or root .env)")
