#!/usr/bin/env python3
"""Re-authorize the personal Google Drive token (token_drive.json).

The personal token expired/revoked 2026-08-04; it cannot refresh, so it must be
re-created once. Two steps:

  1. Print the auth URL:
       python meeting-recorder/reauth_drive.py url
  2. After authorizing in the browser, copy the `code=` value from the
     redirected address bar and exchange it:
       python meeting-recorder/reauth_drive.py code <CODE>

Writes a fresh token to .agent/workspaces/personal/token_drive.json (scope
https://www.googleapis.com/auth/drive). Only the personal workspace is
touched.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CREDENTIALS = REPO_ROOT / ".agent" / "workspaces" / "personal" / "credentials.json"
TOKEN = REPO_ROOT / ".agent" / "workspaces" / "personal" / "token_drive.json"
SCOPES = ["https://www.googleapis.com/auth/drive"]
_STATE = Path(tempfile.gettempdir()) / "psb_reauth_pkce.json"


def main():
    if not CREDENTIALS.exists():
        print(f"missing {CREDENTIALS}")
        return 1

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS), SCOPES)
    flow.redirect_uri = "http://localhost:8080/"

    if len(sys.argv) < 2 or sys.argv[1] == "url":
        auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
        # PKCE: the code_verifier must be reused at exchange time, and it is
        # generated fresh per process - persist it for the `code` step.
        _STATE.write_text(json.dumps({"code_verifier": flow.code_verifier}),
                          encoding="utf-8")
        print("1. Visit this URL in your browser and authorize the app:")
        print(auth_url)
        print("2. After the redirect (page may fail to load), copy the 'code'"
              " parameter from the address bar.")
        print("3. Run:  python meeting-recorder/reauth_drive.py code <CODE>")
        return 0

    if sys.argv[1] == "code":
        if len(sys.argv) < 3:
            print("usage: python meeting-recorder/reauth_drive.py code <CODE>")
            return 1
        if not _STATE.exists():
            print("no PKCE state found - run 'reauth_drive.py url' first, "
                  "authorize, THEN exchange the code")
            return 1
        state = json.loads(_STATE.read_text(encoding="utf-8"))
        flow.code_verifier = state["code_verifier"]
        flow.fetch_token(code=sys.argv[2])
        creds = flow.credentials
        TOKEN.parent.mkdir(parents=True, exist_ok=True)
        TOKEN.write_text(creds.to_json(), encoding="utf-8")
        try:
            _STATE.unlink()
        except OSError:
            pass
        print(f"token written to {TOKEN}")
        return 0

    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
