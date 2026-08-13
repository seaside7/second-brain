"""
Document Connector — Fetches file metadata and content from Google Drive.
Workspace-aware: reads folder config from workspace documents.json.
"""
import io
import json
import os
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / ".agent" / "workspaces"))
sys.path.insert(0, str(REPO_ROOT / "meeting-recorder"))
import workspace_resolver as ws
from common import workspace_client  # noqa: E402

SUPPORTED_MIME_TYPES = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "text/plain": "txt",
    "text/markdown": "md",
    "application/vnd.google-apps.document": "google-docs",
}

EXPORT_MIME = {
    "application/vnd.google-apps.document": "text/plain",
}

# Meeting transcripts live in the PERSONAL drive, under the workspace's business
# subfolder: Meeting Transcripts/<client>/<YYYY>/<MM>/ (storage owner and
# workspace are separate concepts). `client` is derived from the workspace so a
# samudera lookup can only ever resolve Meeting Transcripts/Samudera.
MEETING_ROOT = "Meeting Transcripts"


def _load_config(workspace_name=None):
    ctx = ws.get(workspace_name)
    cfg = ctx.config("documents")
    if not cfg:
        return None, ctx
    return cfg, ctx


def _get_drive_service(ctx, token_file=None):
    """Build Drive API service using a workspace token (default) or an explicit
    token_file (used for personal-drive meeting lookups)."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    scopes = ["https://www.googleapis.com/auth/drive.readonly"]
    if token_file is None:
        token_file = ctx.token("drive")

    # Fallback chain
    if not os.path.exists(token_file):
        old = str(REPO_ROOT / ".agent" / "skills" / "work-drive-connector" / "token.json")
        if os.path.exists(old):
            token_file = old

    if not os.path.exists(token_file):
        return None

    creds = Credentials.from_authorized_user_file(token_file, scopes)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            return None

    return build("drive", "v3", credentials=creds)


def list_files(workspace_name=None, page_size=100):
    """List supported files in the configured Drive folder.
    Returns list of {id, name, mimeType, modifiedTime, webViewLink, size}."""
    cfg, ctx = _load_config(workspace_name)
    if not cfg:
        return [], "No documents.json config found for this workspace."

    service = _get_drive_service(ctx)
    if not service:
        return [], "Drive authentication failed."

    folder_id = cfg.get("folder_id", "")
    shared_drive_id = cfg.get("shared_drive_id")

    if not folder_id:
        return [], "No folder_id configured in documents.json."

    # Build query for supported types in folder
    mime_filter = " or ".join(f"mimeType='{m}'" for m in SUPPORTED_MIME_TYPES.keys())
    query = f"'{folder_id}' in parents and ({mime_filter}) and trashed=false"

    kwargs = {
        "q": query,
        "pageSize": page_size,
        "fields": "files(id,name,mimeType,modifiedTime,webViewLink,size)",
        "orderBy": "modifiedTime desc",
    }
    if shared_drive_id:
        kwargs["corpora"] = "drive"
        kwargs["driveId"] = shared_drive_id
        kwargs["includeItemsFromAllDrives"] = True
        kwargs["supportsAllDrives"] = True

    try:
        results = service.files().list(**kwargs).execute()
        files = results.get("files", [])
        return files, None
    except Exception as e:
        return [], f"Drive API error: {e}"


def download_text(file_id, mime_type, workspace_name=None):
    """Download file content as text. Handles Google Docs export.
    Returns (text, error)."""
    cfg, ctx = _load_config(workspace_name)
    service = _get_drive_service(ctx)
    if not service:
        return None, "Drive auth failed."

    try:
        if mime_type in EXPORT_MIME:
            # Google Docs: export as plain text
            export_mime = EXPORT_MIME[mime_type]
            request = service.files().export_media(fileId=file_id, mimeType=export_mime)
            content = request.execute()
            return content.decode("utf-8", errors="replace"), None
        else:
            # Regular files: download
            request = service.files().get_media(fileId=file_id)
            content = request.execute()
            return content, None
    except Exception as e:
        return None, f"Download failed: {e}"


# ---------- meeting transcript scoping (workspace rules) ----------
#
# Meeting transcripts/MOMs are archived in the PERSONAL drive regardless of the
# meeting's workspace. A workspace's lookup scope is exactly
#   Meeting Transcripts/<client>/
# in that personal drive. These helpers are the ONLY surface the search layer
# may use for meeting transcripts: a samudera lookup resolves
# Meeting Transcripts/Samudera and can never reach other personal folders,
# personal meetings, or other workspaces' folders.

def _drive_id_from_url(value):
    """Accept a bare Drive folder id or a drive.google.com /folders/<id> URL."""
    value = (value or "").strip().strip('"')
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", value)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", value):
        return value
    return None


def _personal_meeting_root_id():
    """Raw id of the PERSONAL drive's Meeting Transcripts root folder, from
    .agent/workspaces/personal/documents.json -> meeting_folder. None when
    unset."""
    cfg_path = REPO_ROOT / ".agent" / "workspaces" / "personal" / "documents.json"
    if not cfg_path.exists():
        return None
    cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    return _drive_id_from_url(cfg.get("meeting_folder"))


def _find_folder_id(service, parent_id, name):
    """Return the id of folder `name` directly under parent_id, or None."""
    q = (f"'{parent_id}' in parents and name='{name}' "
         f"and mimeType='application/vnd.google-apps.folder' and trashed=false")
    res = service.files().list(q=q, fields="files(id,name)", pageSize=5).execute()
    files = res.get("files", [])
    return files[0]["id"] if files else None


def _walk_meeting_folder(service, folder_id, depth, out):
    """Collect supported files under folder_id, descending into subfolders
    (the Meeting Transcripts/<client>/<YYYY>/<MM> tree is depth <= 3)."""
    if depth <= 0:
        return
    mime_filter = " or ".join(f"mimeType='{m}'" for m in SUPPORTED_MIME_TYPES.keys())
    q = f"'{folder_id}' in parents and trashed=false"
    try:
        res = service.files().list(
            q=q, pageSize=200,
            fields="files(id,name,mimeType,modifiedTime,webViewLink,size,parents)").execute()
    except Exception:
        return
    for f in res.get("files", []):
        if f.get("mimeType") == "application/vnd.google-apps.folder":
            _walk_meeting_folder(service, f["id"], depth - 1, out)
        elif f.get("mimeType") in SUPPORTED_MIME_TYPES:
            out.append({k: f.get(k) for k in
                        ("id", "name", "mimeType", "modifiedTime", "webViewLink", "size")})


def list_meeting_files(workspace_name=None, max_depth=3):
    """List meeting transcripts for a workspace from the PERSONAL drive only.

    Scope = Meeting Transcripts/<client>/ under the personal drive's meeting
    root, where <client> is derived from the workspace (samudera -> Samudera).
    The personal drive's own token is used, so this works even when the target
    workspace has no Drive credentials. Returns (files, error) - same shape as
    list_files."""
    client = workspace_client(workspace_name)
    root_id = _personal_meeting_root_id()
    if not root_id:
        return [], "no meeting_folder configured in personal documents.json"
    ctx = ws.get("personal")
    service = _get_drive_service(ctx)
    if not service:
        return [], "personal Drive authentication failed."
    ws_folder = _find_folder_id(service, root_id, client)
    if not ws_folder:
        return [], f"no {client}/ folder under Meeting Transcripts yet"
    files = []
    _walk_meeting_folder(service, ws_folder, max_depth, files)
    return files, None
