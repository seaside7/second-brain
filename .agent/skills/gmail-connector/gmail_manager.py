#!/usr/bin/env python3
"""
Gmail Manager
Provides listing, searching, reading, and basic management of Gmail messages.

Auth: credentials resolved via workspace_resolver (workspace-specific tokens).
"""
import os
import sys
import argparse
import base64
import signal
from email.mime.text import MIMEText
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, '..', '..', '..'))

# Workspace resolver
sys.path.insert(0, os.path.join(REPO_ROOT, '.agent', 'workspaces'))
import workspace_resolver as ws

# Global timeout: 300 seconds
def timeout_handler(signum, frame):
    print("[ERROR] Gmail Manager timed out after 300 seconds", file=sys.stderr)
    sys.exit(1)

if os.name != 'nt':
    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(300)

# Scopes
SCOPES = ['https://www.googleapis.com/auth/gmail.modify']

# ---------- workspace-aware credentials ----------

_current_workspace = None


def get_current_workspace():
    return _current_workspace


def get_workspace_config():
    return ws.get(_current_workspace)


def _resolve_paths(workspace_name=None):
    """Resolve credentials.json and token path for the given workspace."""
    global _current_workspace
    ctx = ws.get(workspace_name)
    _current_workspace = ctx.name

    token_file = ctx.token('gmail')
    credentials_file = ctx.credentials

    # Fallback: old locations if workspace token doesn't exist
    if not os.path.exists(token_file):
        old_token = os.path.join(SCRIPT_DIR, 'token_gmail_work.json')
        if os.path.exists(old_token):
            token_file = old_token

    if not os.path.exists(credentials_file):
        old_creds = os.path.join(REPO_ROOT, '.agent', 'skills', 'work-drive-connector', 'credentials.json')
        if os.path.exists(old_creds):
            credentials_file = old_creds

    return credentials_file, token_file


# Module-level paths (set by CLI or default)
CREDENTIALS_FILE = None
TOKEN_FILE = None


def _init_paths(workspace_name=None):
    """Initialize module-level paths. Called once at CLI start."""
    global CREDENTIALS_FILE, TOKEN_FILE
    CREDENTIALS_FILE, TOKEN_FILE = _resolve_paths(workspace_name)


def authenticate():
    """Authenticate and return credentials."""
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            # Save refreshed token
            with open(TOKEN_FILE, 'w') as token:
                token.write(creds.to_json())
        else:
            if not os.path.exists(CREDENTIALS_FILE):
                print(f"Error: {CREDENTIALS_FILE} not found.", file=sys.stderr)
                print(f"Place credentials.json in: .agent/workspaces/{_current_workspace}/", file=sys.stderr)
                return None
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            flow.redirect_uri = 'http://localhost:8080/'

            auth_url_str, _ = flow.authorization_url(prompt='consent', access_type='offline')
            print(f"\n[Gmail] [{_current_workspace}] Authentication Required!")
            print(f"1. Visit this URL in your browser:\n   {auth_url_str}")
            print(f"2. Authorize the application and copy the 'code' parameter from the resulting URL.")
            print(f"   (The page may fail to load, just copy the 'code=' value from the address bar)")

            code = input(f"\n[Gmail] [{_current_workspace}] Enter the authorization code: ").strip()
            flow.fetch_token(code=code)
            creds = flow.credentials

        with open(TOKEN_FILE, 'w') as token:
            token.write(creds.to_json())
    return creds

# ---------- actions ----------

def list_emails(query=None, max_results=10):
    creds = authenticate()
    if not creds:
        return

    service = build('gmail', 'v1', credentials=creds)
    try:
        results = service.users().messages().list(userId='me', q=query, maxResults=max_results).execute()
        messages = results.get('messages', [])

        if not messages:
            print(f"[{_current_workspace}] No messages found.")
            return

        print(f"[{_current_workspace}] Found {len(messages)} messages:")
        for msg in messages:
            msg_id = msg['id']
            full_msg = service.users().messages().get(userId='me', id=msg_id, format='metadata', metadataHeaders=['Subject', 'From', 'Date']).execute()
            headers = full_msg.get('payload', {}).get('headers', [])

            subject = next((h['value'] for h in headers if h['name'] == 'Subject'), '(No Subject)')
            sender = next((h['value'] for h in headers if h['name'] == 'From'), '(Unknown Sender)')
            date = next((h['value'] for h in headers if h['name'] == 'Date'), '(No Date)')

            print(f"- [{msg_id}] From: {sender} | Subject: {subject} | Date: {date}")

    except Exception as e:
        print(f"An error occurred: {e}")


def get_email(msg_id):
    creds = authenticate()
    if not creds:
        return

    service = build('gmail', 'v1', credentials=creds)
    try:
        message = service.users().messages().get(userId='me', id=msg_id, format='full').execute()
        payload = message.get('payload', {})
        headers = payload.get('headers', [])

        subject = next((h['value'] for h in headers if h['name'] == 'Subject'), '(No Subject)')
        sender = next((h['value'] for h in headers if h['name'] == 'From'), '(Unknown Sender)')
        date = next((h['value'] for h in headers if h['name'] == 'Date'), '(No Date)')

        print(f"ID: {msg_id}")
        print(f"From: {sender}")
        print(f"Date: {date}")
        print(f"Subject: {subject}")
        print("-" * 40)

        body = ""
        if 'parts' in payload:
            for part in payload['parts']:
                if part['mimeType'] == 'text/plain':
                    data = part['body'].get('data')
                    if data:
                        body += base64.urlsafe_b64decode(data).decode()
        else:
            data = payload['body'].get('data')
            if data:
                body = base64.urlsafe_b64decode(data).decode()

        print(body if body else "(Empty body or HTML-only email)")

    except Exception as e:
        print(f"An error occurred: {e}")


def archive_email(msg_id):
    creds = authenticate()
    if not creds:
        return

    service = build('gmail', 'v1', credentials=creds)
    try:
        service.users().messages().modify(
            userId='me',
            id=msg_id,
            body={'removeLabelIds': ['INBOX']}
        ).execute()
        print(f"[{_current_workspace}] Message {msg_id} archived.")
    except Exception as e:
        print(f"An error occurred: {e}")


def get_profile():
    creds = authenticate()
    if not creds:
        return

    service = build('gmail', 'v1', credentials=creds)
    try:
        profile = service.users().getProfile(userId='me').execute()
        print(f"[{_current_workspace}] User: {profile.get('emailAddress')}")
        print(f"Total Messages: {profile.get('messagesTotal')}")
        print(f"Total Threads: {profile.get('threadsTotal')}")
    except Exception as e:
        print(f"An error occurred: {e}")


def send_email(to, subject, body, cc=None):
    creds = authenticate()
    if not creds:
        return

    service = build('gmail', 'v1', credentials=creds)

    msg = MIMEText(body)
    msg['To'] = to
    if cc:
        msg['Cc'] = cc
    msg['Subject'] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    try:
        sent = service.users().messages().send(userId='me', body={'raw': raw}).execute()
        print(f"[{_current_workspace}] [OK] Email sent. Message ID: {sent.get('id')}")
        print(f"     To: {to}")
        if cc:
            print(f"     Cc: {cc}")
        print(f"     Subject: {subject}")
    except Exception as e:
        print(f"An error occurred: {e}")

# --- Headless two-step auth ---

def _new_flow():
    flow = InstalledAppFlow.from_client_secrets_file(
        CREDENTIALS_FILE, SCOPES, autogenerate_code_verifier=False)
    flow.redirect_uri = 'http://localhost:8080/'
    return flow


def auth_url():
    if not os.path.exists(CREDENTIALS_FILE):
        print(f"Error: {CREDENTIALS_FILE} not found.", file=sys.stderr)
        return
    flow = _new_flow()
    url, _ = flow.authorization_url(prompt='consent', access_type='offline')
    ctx = ws.get(_current_workspace)
    print(f"[Gmail] [{_current_workspace}] Step 1 - authorize as {ctx.owner_email}:")
    print(url)
    print("\nThe page will try to load http://localhost:8080/ and fail - that is expected.")
    print("Copy the 'code=' value from the address bar, then run:")
    print('  auth-save --code "PASTE_CODE_HERE"')


def auth_save(code):
    flow = _new_flow()
    flow.fetch_token(code=code)
    with open(TOKEN_FILE, 'w') as token:
        token.write(flow.credentials.to_json())
    print(f"[{_current_workspace}] [OK] Token saved to {TOKEN_FILE}")

# ---------- CLI ----------

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Gmail Manager')
    parser.add_argument('--workspace', default=None,
                        help='Workspace name (default: active workspace)')
    subparsers = parser.add_subparsers(dest='command')

    # Profile
    subparsers.add_parser('profile', help='Get user profile')

    # List/Search
    list_parser = subparsers.add_parser('list', help='List/Search emails')
    list_parser.add_argument('--query', help='Search query (e.g., "from:work")')
    list_parser.add_argument('--limit', type=int, default=10, help='Max results')

    # Get
    get_parser = subparsers.add_parser('get', help='Get full email content')
    get_parser.add_argument('id', help='Message ID')

    # Archive
    archive_parser = subparsers.add_parser('archive', help='Archive an email')
    archive_parser.add_argument('id', help='Message ID')

    # Send
    send_parser = subparsers.add_parser('send', help='Send a plain-text email')
    send_parser.add_argument('--to', required=True, help='Recipient(s), comma-separated')
    send_parser.add_argument('--cc', default=None, help='Cc recipient(s), comma-separated')
    send_parser.add_argument('--subject', required=True, help='Subject line')
    body_group = send_parser.add_mutually_exclusive_group(required=True)
    body_group.add_argument('--body', help='Inline body text')
    body_group.add_argument('--body-file', help='Path to a file containing the body text')

    # Headless auth
    subparsers.add_parser('auth-url', help='Print OAuth URL (step 1, headless auth)')
    auth_save_parser = subparsers.add_parser('auth-save', help='Exchange code for token (step 2)')
    auth_save_parser.add_argument('--code', required=True, help='Authorization code from redirect URL')

    args = parser.parse_args()

    # Initialize paths from workspace
    _init_paths(args.workspace)

    if args.command == 'profile':
        get_profile()
    elif args.command == 'list':
        list_emails(query=args.query, max_results=args.limit)
    elif args.command == 'get':
        get_email(args.id)
    elif args.command == 'archive':
        archive_email(args.id)
    elif args.command == 'send':
        body = open(args.body_file).read() if args.body_file else args.body
        send_email(args.to, args.subject, body, cc=args.cc)
    elif args.command == 'auth-url':
        auth_url()
    elif args.command == 'auth-save':
        auth_save(args.code)
    else:
        parser.print_help()
