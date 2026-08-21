#!/usr/bin/env python3
"""reauth_drive.py - Re-authenticate the personal Drive token."""
import os
import sys

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    print("pip install google-auth-oauthlib")
    sys.exit(1)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, '..', '..'))
CREDENTIALS = os.path.join(REPO_ROOT, '.agent', 'workspaces', 'personal', 'credentials.json')
TOKEN_OUT = os.path.join(REPO_ROOT, '.agent', 'workspaces', 'personal', 'token_drive.json')
SCOPES = ['https://www.googleapis.com/auth/drive']

if not os.path.exists(CREDENTIALS):
    print("Missing: %s" % CREDENTIALS)
    sys.exit(1)

flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS, SCOPES)
flow.redirect_uri = 'http://localhost:8080/'
auth_url, _ = flow.authorization_url(prompt='consent', access_type='offline')

print("\n1. Open this URL in your browser:\n")
print(auth_url)
print()
code = input("2. Paste the authorization code here: ").strip()
flow.fetch_token(code=code)

os.makedirs(os.path.dirname(TOKEN_OUT), exist_ok=True)
with open(TOKEN_OUT, 'w', encoding='utf-8') as f:
    f.write(flow.credentials.to_json())

print("\nToken saved to: %s" % TOKEN_OUT)
print("Refresh token expires:", flow.credentials.expiry)
