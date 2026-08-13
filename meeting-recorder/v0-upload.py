#!/usr/bin/env python3
"""Upload a finished recording to the PERSONAL drive (meeting pipeline glue).

Takes a recorded audio file (or a registry recording_id) and pushes it through
the v0 pipeline: register -> local store -> upload to
  Meeting Transcripts/<client>/<YYYY>/<MM>/
in the personal drive, stamping the registry entry with cloud_path/cloud_status.

Usage:
  python meeting-recorder/v0-upload.py <audio.wav|recording_id> [--workspace personal|samudera|catalyze] [--cloud drive|dry-run]

--workspace None (default) = legacy "Work" client. --cloud dry-run prints the
intended key and uploads nothing.
"""
import argparse
import datetime
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


datamodel = _load("v0_datamodel", "v0-datamodel.py")
storage = _load("v0_storage", "v0-storage.py")

from common import REPO_ROOT  # noqa: E402  (after _load sets PSB_REPO_ROOT if set)

REGISTRY_PATH = os.path.join(REPO_ROOT, "journal", "fathom_registry.json")


def _read_sidecar(base):
    p = base + ".json"
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _registry_entry(rec_id):
    with open(REGISTRY_PATH, encoding="utf-8") as f:
        registry = json.load(f)
    return registry.get(rec_id)


def _duration_min(path):
    try:
        return max(1, round(float(os.path.getsize(path)) / 1_000_000))
    except Exception:
        return 1


def build_recording(arg, workspace):
    """Build a Recording from a file path or a registry recording_id."""
    if os.path.exists(arg):
        base = os.path.splitext(arg)[0]
        side = _read_sidecar(base)
        start_utc = side.get("start_utc")
        start_wib = datetime.datetime.now(datetime.timezone.utc)
        if start_utc:
            try:
                start_wib = datetime.datetime.fromisoformat(start_utc)
            except ValueError:
                pass
        start_wib = start_wib.astimezone(datetime.timezone(datetime.timedelta(hours=7)))
        # reuse an existing registry entry whose local_path matches, else new id
        rec_id = f"upload-{int(datetime.datetime.now().timestamp())}"
        return datamodel.Recording(
            recording_id=rec_id,
            source="local-recorder",
            title=side.get("title") or os.path.basename(base),
            date_wib=start_wib.strftime("%Y-%m-%d"),
            time_wib=start_wib.strftime("%H:%M"),
            duration_min=side.get("duration_min") or _duration_min(arg),
            workspace=workspace,
            matched_meeting=side.get("title"),
            participants=side.get("attendees") or [],
        ), os.path.abspath(arg)
    if arg in (json.load(open(REGISTRY_PATH, encoding="utf-8")) or {}):
        entry = _registry_entry(arg)
        rec = datamodel.Recording(
            recording_id=arg, source=entry.get("source", "local-recorder"),
            title=entry.get("title") or entry.get("matched_meeting") or arg,
            date_wib=entry.get("date_wib") or "unknown",
            time_wib=entry.get("time_wib") or "00:00",
            duration_min=int((entry.get("duration") or "1 min").split()[0]),
            workspace=workspace,
            matched_meeting=entry.get("matched_meeting"),
            participants=entry.get("participants") or [],
            language=entry.get("transcript_language"),
            local_path=entry.get("local_path", ""),
        )
        src = entry.get("local_path") or ""
        if src and not os.path.isabs(src):
            src = os.path.join(REPO_ROOT, src)
        return rec, src
    sys.exit(f"ERROR: {arg} is neither a file nor a known recording_id")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="audio file path or registry recording_id")
    ap.add_argument("--workspace", default=None,
                    choices=["personal", "samudera", "catalyze"],
                    help="workspace scope -> destination subfolder")
    ap.add_argument("--cloud", default="drive", choices=["drive", "dry-run"],
                    help="cloud backend (default drive = real personal-drive upload)")
    args = ap.parse_args()

    rec, src_path = build_recording(args.target, args.workspace)
    if not src_path or not os.path.exists(src_path):
        sys.exit(f"ERROR: source audio not found: {src_path}")

    cloud = (storage.GoogleDriveCloudStore() if args.cloud == "drive"
             else storage.DryRunCloudStore())
    result = storage.store_recording(src_path, rec, cloud=cloud)
    datamodel.insert_recording(REGISTRY_PATH, rec, overwrite=True)
    print(f"recording {rec.recording_id}")
    print(f"  workspace -> client : {args.workspace or 'work'} -> {rec.client}")
    print(f"  local store        : {result['local_rel']}")
    print(f"  cloud status       : {result['cloud_status']}")
    print(f"  cloud key          : {result['cloud_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
