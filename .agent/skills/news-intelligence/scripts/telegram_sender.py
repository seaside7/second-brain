#!/usr/bin/env python3
"""
telegram_sender.py — Send messages via Telegram Bot API for the news-intelligence skill.

Follows the project's messaging connector pattern:
- urllib.request for HTTP
- token.env + .env fallback for credentials
- require_send_approval gate for outbound sends
- 60s per-request timeout, 180s global timeout
"""
import argparse
import json
import os
import signal
import sys
import urllib.request
import urllib.error
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SKILL_DIR.parent.parent.parent

SCRIPTS_DIR = REPO_ROOT / ".agent" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
try:
    from file_utils import require_send_approval
except ImportError:
    def require_send_approval(action_label, approved):
        if not approved:
            print(f"Error: refusing to {action_label} without approval. "
                  "Pass --approved once the owner has approved this draft.",
                  file=sys.stderr)
            sys.exit(1)

TOKEN_ENV_PATH = SKILL_DIR / "token.env"
DEFAULT_TIMEOUT = 60


def timeout_handler(signum, frame):
    print("[ERROR] Telegram Sender timed out after 180 seconds", file=sys.stderr)
    sys.exit(1)


if os.name != "nt":
    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(180)


def load_telegram_token():
    if TOKEN_ENV_PATH.exists():
        with open(TOKEN_ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    return line.split("=", 1)[1].strip()
    return os.environ.get("TELEGRAM_BOT_TOKEN", "")


def load_telegram_chat_id():
    if TOKEN_ENV_PATH.exists():
        with open(TOKEN_ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("TELEGRAM_CHAT_ID="):
                    return line.split("=", 1)[1].strip()
    return os.environ.get("TELEGRAM_CHAT_ID", "")


def send_message(text, token=None, chat_id=None, parse_mode="HTML", dry_run=False):
    token = token or load_telegram_token()
    chat_id = chat_id or load_telegram_chat_id()

    if not token:
        return False, "TELEGRAM_BOT_TOKEN not set in token.env or environment"
    if not chat_id:
        return False, "TELEGRAM_CHAT_ID not set in token.env or environment"

    if dry_run:
        print(f"[DRY-RUN] Would send {len(text)} chars to Telegram chat {chat_id}", file=sys.stderr)
        return True, "dry_run"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": False,
    }).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok"):
                print(f"[INFO] Telegram message sent (msg_id={data.get('result', {}).get('message_id')})",
                      file=sys.stderr)
                return True, data
            return False, data.get("description", "unknown error")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")[:500]
        except Exception:
            pass
        return False, f"HTTP {e.code}: {body}"
    except Exception as e:
        return False, str(e)


def main():
    parser = argparse.ArgumentParser(description="Telegram Bot API Sender")
    parser.add_argument("--action", required=True, choices=["send"])
    parser.add_argument("--text", help="Message text to send (HTML format supported)")
    parser.add_argument("--text-file", dest="text_file", help="Read message text from file")
    parser.add_argument("--token", help="Telegram Bot Token (or set TELEGRAM_BOT_TOKEN)")
    parser.add_argument("--chat-id", dest="chat_id", help="Telegram Chat ID (or set TELEGRAM_CHAT_ID)")
    parser.add_argument("--approved", action="store_true",
                        help="Explicit approval to send (required for actual delivery)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be sent without actually sending")
    args = parser.parse_args()

    if not args.dry_run:
        require_send_approval("send Telegram message", args.approved)

    if args.text_file:
        with open(args.text_file, "r", encoding="utf-8") as f:
            text = f.read()
    elif args.text:
        text = args.text
    else:
        text = sys.stdin.read()

    if not text.strip():
        print("[ERROR] No message text provided", file=sys.stderr)
        sys.exit(1)

    ok, result = send_message(text, token=args.token, chat_id=args.chat_id, dry_run=args.dry_run)
    if not ok:
        print(f"[ERROR] Failed to send Telegram message: {result}", file=sys.stderr)
        sys.exit(1)
    print("OK")


if __name__ == "__main__":
    main()
