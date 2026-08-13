#!/usr/bin/env python3
"""LIVE drive smoke test (opt-in) - verifies the v0 meeting-storage pipeline
against your REAL personal Google Drive using your personal token.

Run:  python meeting-recorder/smoke_v0_drive.py

Creates a clearly-labeled test tree under your meeting root:
  Meeting Transcripts/SmokeTest/<YYYY>/<MM>/smoke-test-<ts>.txt
then lists it back and prints the Drive link. Nothing is written anywhere
else, and nothing is deleted.
"""
import datetime
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


v0_storage = _load("v0_storage", "v0-storage.py")
GoogleDriveCloudStore = v0_storage.GoogleDriveCloudStore
meeting_root_folder_id = v0_storage.meeting_root_folder_id


def main():
    store = GoogleDriveCloudStore()
    service, err = store._service()
    if err:
        print(f"AUTH FAILED: {err}")
        return 1
    root = meeting_root_folder_id()
    if not root:
        print("no meeting_folder configured in personal documents.json")
        return 1

    res = service.files().list(
        q=f"'{root}' in parents and trashed=false",
        fields="files(id,name,mimeType)").execute()
    children = res.get("files", [])
    print(f"Meeting root ({root}) children now: "
          f"{[(f['name'], f['mimeType']) for f in children] or '(empty)'}")

    now = datetime.datetime.now()
    y, m = str(now.year), f"{now.month:02d}"
    test_folder = store._find_or_create_folder(service, root, "SmokeTest")
    y_folder = store._find_or_create_folder(service, test_folder, y)
    m_folder = store._find_or_create_folder(service, y_folder, m)

    name = f"smoke-test-{now.strftime('%Y%m%d-%H%M%S')}.txt"
    tmp = os.path.join(sys.prefix, name)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("v0 live smoke test - personal drive meeting storage OK\n")
    try:
        file_id = store._upload(service, m_folder, name, tmp, mime="text/plain")
    finally:
        os.remove(tmp)

    meta = service.files().get(fileId=file_id, fields="webViewLink").execute()
    print(f"UPLOADED: {name}")
    print(f"  tree: Meeting Transcripts/SmokeTest/{y}/{m}/")
    print(f"  file id: {file_id}")
    print(f"  link: {meta.get('webViewLink')}")
    print("Verify at the URL above. Delete the SmokeTest folder when done "
          "(or ask me to, with your OK).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
