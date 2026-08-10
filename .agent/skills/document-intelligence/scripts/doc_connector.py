"""
Document Connector — Fetches file metadata and content from Google Drive.
Workspace-aware: reads folder config from workspace documents.json.
"""
import io
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / ".agent" / "workspaces"))
import workspace_resolver as ws

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


def _load_config(workspace_name=None):
    ctx = ws.get(workspace_name)
    cfg = ctx.config("documents")
    if not cfg:
        return None, ctx
    return cfg, ctx


def _get_drive_service(ctx):
    """Build Drive API service using workspace token."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    scopes = ["https://www.googleapis.com/auth/drive.readonly"]
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
