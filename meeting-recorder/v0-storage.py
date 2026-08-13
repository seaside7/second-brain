#!/usr/bin/env python3
"""Workspace-aware recording storage (v0).

Layout is driven by the registry entry's metadata, never by where the source
file happened to land:

  local: Clients/<client>/recordings/<date>/<slug>.<ext>
  cloud: Meeting Transcripts/<client>/<YYYY>/<MM>/<slug>.<ext>

STORAGE OWNER AND WORKSPACE ARE SEPARATE CONCEPTS (meeting-recorder rule):
every transcript/MOM is archived in the PERSONAL Google Drive, even for
Samudera/Catalyze meetings. The workspace determines the SUBFOLDER the file
lands in (its business scope), never which drive owns it:

  Personal Drive/Meeting Transcripts/
    Samudera/YYYY/MM/...     <- samudera meetings
    Personal/YYYY/MM/...     <- personal meetings
    Catalyze/YYYY/MM/...     <- catalyze meetings
    Work/YYYY/MM/...         <- legacy on-machine recordings

The meeting root folder id is read from
.agent/workspaces/personal/documents.json -> meeting_folder (this is the
personal drive's folder id; workspace subfolders are created under it).

CloudStore is the interface. DryRunCloudStore is the default backend (fail-soft,
prints keys, uploads nothing). GoogleDriveCloudStore is the real backend, wired
to the personal drive; it is ALSO fail-soft - any auth/API problem returns a
'failed: ...' status instead of raising, so a broken credential can never block
the local pipeline.
"""
import json
import os
import re
import shutil
import sys

from common import REPO_ROOT, workspace_client

MEETING_ROOT = "Meeting Transcripts"
_FOLDER_URL_RE = re.compile(r"/folders/([A-Za-z0-9_-]+)")


def _ext(path: str) -> str:
    if "." in os.path.basename(path):
        return path.rsplit(".", 1)[-1].lower()
    return "bin"


def _ym(rec) -> tuple[str, str]:
    """(year, month) from date_wib 'YYYY-MM-DD'; 'unknown' fallbacks keep the
    layout deterministic even when the date is missing."""
    d = rec.date_wib or ""
    if len(d) >= 7 and d[4] == "-":
        return d[:4], d[5:7]
    return "unknown", "unknown"


def _drive_id_from_url(value: str | None) -> str | None:
    """Accept a bare Drive folder id or a drive.google.com /folders/<id> URL."""
    value = (value or "").strip().strip('"')
    m = _FOLDER_URL_RE.search(value)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", value):
        return value
    return None


def personal_documents_config(repo_root=REPO_ROOT) -> dict:
    p = os.path.join(repo_root, ".agent", "workspaces", "personal", "documents.json")
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8-sig") as f:
        return json.load(f)


def meeting_root_folder_id(repo_root=REPO_ROOT) -> str | None:
    """Raw id of the PERSONAL drive's Meeting Transcripts root folder, from
    .agent/workspaces/personal/documents.json -> meeting_folder."""
    return _drive_id_from_url(personal_documents_config(repo_root).get("meeting_folder"))


def relative_storage_path(rec, ext: str) -> str:
    """Client-local path under a recordings/ root: <date>/<slug>.<ext>."""
    return os.path.join("recordings", rec.date_wib, f"{rec.storage_slug()}.{ext}")


def cloud_storage_key(rec, ext: str, name: str | None = None) -> str:
    """Cloud key: Meeting Transcripts/<client>/<YYYY>/<MM>/<slug>.<ext>. The
    workspace (-> client) is the top namespace below the meeting root, so a
    samudera recording can never resolve to a personal key.

    `name` overrides the slug (used for sidecar artifacts like transcript and
    MOM that must keep their own filename so they never collide with the
    audio key)."""
    y, m = _ym(rec)
    return os.path.join(MEETING_ROOT, rec.client, y, m,
                        f"{(name or rec.storage_slug())}.{ext}")


class LocalStore:
    """Copies the raw recording under Clients/<client>/recordings/."""

    def __init__(self, repo_root=REPO_ROOT):
        self.repo_root = repo_root

    def store(self, src_path: str, rec) -> str:
        rel = relative_storage_path(rec, _ext(src_path))
        dest = os.path.join(self.repo_root, "Clients", rec.client, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.abspath(src_path) != os.path.abspath(dest):
            shutil.copy2(src_path, dest)
        return os.path.relpath(dest, self.repo_root)


class CloudStore:
    """Interface for pushing recordings + index to cloud storage."""

    def upload_recording(self, src_path: str, rec, name: str | None = None) -> tuple[str, str]:
        raise NotImplementedError

    def push_index(self, index_payload: dict, workspace: str | None) -> str:
        raise NotImplementedError


class DryRunCloudStore(CloudStore):
    """Default backend: fail-soft. Logs the intended key, uploads nothing."""

    def upload_recording(self, src_path: str, rec, name: str | None = None) -> tuple[str, str]:
        key = cloud_storage_key(rec, _ext(src_path), name=name)
        print(f"[cloud:dry-run] upload -> {key}")
        return key, "dry-run"

    def push_index(self, index_payload: dict, workspace: str | None) -> str:
        key = os.path.join(MEETING_ROOT, workspace_client(workspace), "index.json")
        print(f"[cloud:dry-run] push index -> {key}")
        return key


class GoogleDriveCloudStore(CloudStore):
    """Real backend: uploads to the PERSONAL drive, under the workspace's
    business subfolder (Meeting Transcripts/<client>/<YYYY>/<MM>).

    Drive owner and workspace stay separate - credentials used are always the
    personal workspace's (token_drive.json), regardless of the recording's
    workspace. Fail-soft: returns a status string, never raises.
    """

    def __init__(self, repo_root=REPO_ROOT):
        self.repo_root = repo_root

    def _service(self):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        token_file = os.path.join(
            self.repo_root, ".agent", "workspaces", "personal", "token_drive.json")
        if not os.path.exists(token_file):
            return None, "no .agent/workspaces/personal/token_drive.json"
        creds = Credentials.from_authorized_user_file(
            token_file, ["https://www.googleapis.com/auth/drive"])
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as e:
                    return None, f"personal token refresh failed: {e}"
            else:
                return None, "personal token not valid and cannot refresh"
        return build("drive", "v3", credentials=creds), None

    def _find_or_create_folder(self, service, parent_id: str, name: str) -> str:
        q = (f"'{parent_id}' in parents and name='{name}' "
             f"and mimeType='application/vnd.google-apps.folder' and trashed=false")
        res = service.files().list(q=q, fields="files(id,name)", pageSize=5).execute()
        files = res.get("files", [])
        if files:
            return files[0]["id"]
        meta = {"name": name, "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id]}
        return service.files().create(body=meta, fields="id").execute()["id"]

    def _target_folder(self, service, rec) -> str:
        root = meeting_root_folder_id(self.repo_root)
        if not root:
            raise ValueError("personal documents.json has no meeting_folder")
        y, m = _ym(rec)
        ws_folder = self._find_or_create_folder(service, root, rec.client)
        y_folder = self._find_or_create_folder(service, ws_folder, y)
        return self._find_or_create_folder(service, y_folder, m)

    def _upload(self, service, folder_id: str, name: str, src_path: str,
                mime: str | None = None) -> str:
        from googleapiclient.http import MediaFileUpload

        body = {"name": name, "parents": [folder_id]}
        return service.files().create(
            body=body, media_body=MediaFileUpload(src_path, mimetype=mime),
            fields="id,webViewLink").execute()["id"]

    def upload_recording(self, src_path: str, rec, name: str | None = None) -> tuple[str, str]:
        key = cloud_storage_key(rec, _ext(src_path), name=name)
        file_name = f"{name}.{_ext(src_path)}" if name else os.path.basename(src_path)
        try:
            service, err = self._service()
            if err:
                return key, f"failed: {err}"
            folder_id = self._target_folder(service, rec)
            self._upload(service, folder_id, file_name, src_path)
            print(f"[cloud:drive] uploaded -> {key}")
            return key, "uploaded"
        except Exception as e:
            print(f"[cloud:drive] upload failed ({key}): {e}", file=sys.stderr)
            return key, f"failed: {e}"

    def push_index(self, index_payload: dict, workspace: str | None) -> str:
        client = workspace_client(workspace)
        key = os.path.join(MEETING_ROOT, client, "index.json")
        try:
            service, err = self._service()
            if err:
                print(f"[cloud:drive] index push failed: {err}", file=sys.stderr)
                return key
            root = meeting_root_folder_id(self.repo_root)
            if not root:
                raise ValueError("personal documents.json has no meeting_folder")
            ws_folder = self._find_or_create_folder(service, root, client)
            tmp = os.path.join(sys.prefix, "v0-index-tmp.json")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(index_payload, f, ensure_ascii=False, indent=2)
            try:
                self._upload(service, ws_folder, "index.json", tmp,
                             mime="application/json")
            finally:
                os.remove(tmp)
            print(f"[cloud:drive] index pushed -> {key}")
            return key
        except Exception as e:
            print(f"[cloud:drive] index push failed ({key}): {e}", file=sys.stderr)
            return key


def store_recording(src_path: str, rec, repo_root=REPO_ROOT, cloud=None) -> dict:
    """Local store + optional cloud upload. Returns the placement result and
    stamps the Recording's cloud fields (the caller writes them to the
    registry via insert_recording)."""
    local = LocalStore(repo_root)
    rel = local.store(src_path, rec)
    rec.local_path = os.path.join(repo_root, rel)
    cloud_status, cloud_path = "local-only", None
    if cloud is not None:
        cloud_path, cloud_status = cloud.upload_recording(src_path, rec)
    rec.cloud_path = cloud_path
    rec.cloud_status = cloud_status
    return {"local_rel": rel, "cloud_path": cloud_path, "cloud_status": cloud_status}
