#!/usr/bin/env python3
"""Smoke test for the v0 workspace-aware storage + index stack.

Run:  python meeting-recorder/smoke_v0.py

Redirects REPO_ROOT to a scratch dir via PSB_REPO_ROOT (see common.py) so the
real repo is never touched, then verifies for fake personal + samudera +
legacy-Work recordings:
  1. PSB_REPO_ROOT isolation is active.
  2. insert_recording stamps the registry with workspace + derived client and
     enforces the idempotency guard.
  3. LocalStore places each recording under Clients/<client>/recordings/<date>/.
  4. Cloud keys follow the meeting layout Meeting Transcripts/<client>/<YYYY>/<MM>/
     and are workspace-isolated (samudera never collides with personal).
  5. Drive folder ids are resolved from a URL and from personal documents.json
     -> meeting_folder.
  6. build_index writes one index.json per client, each containing ONLY its own
     workspace's recordings, and the CLI dry-run path exits clean.
Exits non-zero on the first failing check.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_audio():
    return b"PSB-SMOKE-v0 fake recording payload (not real audio)"


def main():
    scratch = tempfile.mkdtemp(prefix="psb-v0-smoke-")
    os.environ["PSB_REPO_ROOT"] = scratch
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}"
              + (f"  -- {detail}" if detail else ""))

    try:
        datamodel = _load("v0_datamodel", "v0-datamodel.py")
        storage = _load("v0_storage", "v0-storage.py")
        v0index = _load("v0_index", "v0-index.py")

        # 1. isolation hook is active
        from common import REPO_ROOT
        check("REPO_ROOT isolation (PSB_REPO_ROOT)",
              REPO_ROOT == scratch, f"REPO_ROOT={REPO_ROOT}")

        # 2. fake recordings + the single registry writer
        recs = [
            datamodel.Recording(
                recording_id="fake-personal-1", source="smoke-test",
                title="Nyusul kuliah AI engineering", date_wib="2026-08-13",
                time_wib="08:15", duration_min=31, workspace="personal"),
            datamodel.Recording(
                recording_id="fake-samudera-1", source="smoke-test",
                title="PRD review - port transformation", date_wib="2026-08-13",
                time_wib="09:30", duration_min=45, workspace="samudera"),
            datamodel.Recording(
                recording_id="fake-legacy-1", source="smoke-test",
                title="WWF data platform sync", date_wib="2026-08-13",
                time_wib="14:00", duration_min=20, workspace=None),
        ]
        registry_path = os.path.join(scratch, "journal", "fathom_registry.json")
        for r in recs:
            datamodel.insert_recording(registry_path, r)
        with open(registry_path, encoding="utf-8") as f:
            reg = json.load(f)
        check("registry stamped workspace + derived client",
              reg["fake-personal-1"]["client"] == "Personal"
              and reg["fake-samudera-1"]["client"] == "Samudera"
              and reg["fake-legacy-1"]["client"] == "Work",
              f"clients={[reg[k]['client'] for k in reg]}")

        try:
            datamodel.insert_recording(registry_path, recs[0])
            check("insert idempotency guard", False, "duplicate insert did not raise")
        except ValueError:
            check("insert idempotency guard", True)

        # 3. source files + LocalStore placement
        srcs = []
        for r in recs:
            p = os.path.join(scratch, "incoming", f"{r.recording_id}.wav")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(_fake_audio())
            srcs.append(p)
        store = storage.LocalStore(scratch)
        placements = [store.store(p, r) for p, r in zip(srcs, recs)]
        expected_client = [r.client for r in recs]
        check("local store per-client layout",
              all(placements[i].startswith(
                  os.path.join("Clients", expected_client[i], "recordings"))
                  for i in range(3)),
              placements)
        check("stored files exist",
              all(os.path.isfile(os.path.join(scratch, rel)) for rel in placements))
        check("workspace folders isolated on disk",
              os.path.isdir(os.path.join(scratch, "Clients", "Personal", "recordings"))
              and os.path.isdir(os.path.join(scratch, "Clients", "Samudera", "recordings")))

        # 4. cloud keys follow the meeting layout, isolated by workspace
        cloud = storage.DryRunCloudStore()
        keys = [cloud.upload_recording(p, r)[0] for p, r in zip(srcs, recs)]
        expect_keys = [
            os.path.join("Meeting Transcripts", "Personal", "2026", "08",
                         "Nyusul_kuliah_AI_engineering.wav"),
            os.path.join("Meeting Transcripts", "Samudera", "2026", "08",
                         "PRD_review_port_transformation.wav"),
            os.path.join("Meeting Transcripts", "Work", "2026", "08",
                         "WWF_data_platform_sync.wav"),
        ]
        check("cloud keys = Meeting Transcripts/<client>/YYYY/MM",
              keys == expect_keys, keys)
        check("cloud keys workspace-isolated",
              keys[0].split(os.sep)[1] == "Personal"
              and keys[1].split(os.sep)[1] == "Samudera"
              and keys[2].split(os.sep)[1] == "Work")

        # 5. drive id resolution: URL form + personal documents.json meeting_folder
        url = "https://drive.google.com/drive/u/0/folders/14YDU-8ZfVnWmDx53I8hpuGcc8XwFkUbf"
        check("drive id from URL", storage._drive_id_from_url(url) ==
              "14YDU-8ZfVnWmDx53I8hpuGcc8XwFkUbf")
        check("drive id passes through raw id",
              storage._drive_id_from_url("1OWdG2K8n3CXh4JX-m3ebKmOr_2owz-ZB") ==
              "1OWdG2K8n3CXh4JX-m3ebKmOr_2owz-ZB")
        personal_cfg_dir = os.path.join(scratch, ".agent", "workspaces", "personal")
        os.makedirs(personal_cfg_dir, exist_ok=True)
        with open(os.path.join(personal_cfg_dir, "documents.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"source": "google_drive",
                       "meeting_folder": url}, f)
        check("meeting root resolved from personal documents.json",
              storage.meeting_root_folder_id(scratch) ==
              "14YDU-8ZfVnWmDx53I8hpuGcc8XwFkUbf")

        # 5. per-workspace index files, isolation enforced in the payloads
        v0index.build_index(scratch)
        idx_p = os.path.join(scratch, "Clients", "Personal", "recordings", "index.json")
        idx_s = os.path.join(scratch, "Clients", "Samudera", "recordings", "index.json")
        idx_w = os.path.join(scratch, "Clients", "Work", "recordings", "index.json")
        check("index.json per client written",
              os.path.isfile(idx_p) and os.path.isfile(idx_s) and os.path.isfile(idx_w))
        with open(idx_p, encoding="utf-8") as f:
            pidx = json.load(f)
        with open(idx_s, encoding="utf-8") as f:
            sidx = json.load(f)
        check("personal index holds only personal",
              pidx["workspace"] == "personal" and pidx["count"] == 1
              and all(r["workspace"] == "personal" for r in pidx["recordings"]),
              f"count={pidx['count']}")
        check("samudera index holds only samudera",
              sidx["workspace"] == "samudera" and sidx["count"] == 1
              and all(r["workspace"] == "samudera" for r in sidx["recordings"]),
              f"count={sidx['count']}")
        pids = {r["recording_id"] for r in pidx["recordings"]}
        sids = {r["recording_id"] for r in sidx["recordings"]}
        check("indexes never cross workspaces", not (pids & sids))

        # 5b. CLI dry-run path exits clean
        proc = subprocess.run(
            [sys.executable, os.path.join(HERE, "v0-index.py"),
             "--repo", scratch, "--dry-run"],
            capture_output=True, text=True, timeout=60)
        check("v0-index.py --dry-run exits clean", proc.returncode == 0,
              proc.stderr.strip() or "rc=0")

        failed = [c for c in checks if not c[1]]
        print("")
        if failed:
            print(f"SMOKE FAILED: {len(failed)}/{len(checks)} checks failed")
            for name, _ok, detail in failed:
                print(f"  - {name}: {detail}")
            return 1
        print(f"SMOKE PASSED: {len(checks)}/{len(checks)} checks")
        return 0
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
