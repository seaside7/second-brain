#!/usr/bin/env python3
"""Build + push the per-workspace recording index (v0).

Reads journal/fathom_registry.json, groups recordings by workspace, writes one
index.json per client under Clients/<client>/recordings/, then pushes each to
cloud storage via the CloudStore interface (DryRunCloudStore by default -
fail-soft, prints the key, uploads nothing). Prints ONE summary of the run.

Usage:
  python v0-index.py [--dry-run] [--repo <root>]

--dry-run: print the intended index paths + cloud keys, write nothing.
"""
import argparse
import datetime
import importlib.util
import json
import os
import sys

from common import REPO_ROOT, workspace_client

REGISTRY_REL = os.path.join("journal", "fathom_registry.json")


def _load_hyphen_sibling(name, filename):
    """v0-* files are hyphen-named (scripts, not importable packages); load the
    sibling module by path so v0-index can use its DryRunCloudStore."""
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(name, os.path.join(here, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


v0_storage = _load_hyphen_sibling("v0_storage", "v0-storage.py")


def load_registry(repo_root=REPO_ROOT) -> dict:
    p = os.path.join(repo_root, REGISTRY_REL)
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8-sig") as f:
        return json.load(f)


def summarize(rec_id: str, entry: dict) -> dict:
    return {
        "recording_id": rec_id,
        "date_wib": entry.get("date_wib"),
        "time_wib": entry.get("time_wib"),
        "duration": entry.get("duration"),
        "title": (entry.get("matched_meeting")
                  or entry.get("title") or entry.get("raw_title")),
        "participants": entry.get("participants") or [],
        "workspace": entry.get("workspace"),
        "client": entry.get("client") or workspace_client(entry.get("workspace")),
        "cloud_path": entry.get("cloud_path"),
        "cloud_status": entry.get("cloud_status", "pending"),
    }


def build_index(repo_root=REPO_ROOT, dry_run=False, cloud=None):
    """Group the registry by workspace; write one index.json per client.

    Returns (written_paths, summary_rows, payloads) where summary_rows is a
    list of (workspace, client, count, index_path) for the run summary."""
    registry = load_registry(repo_root)
    by_ws: dict = {}
    for rec_id, entry in registry.items():
        ws = entry.get("workspace")  # None -> legacy Work client
        by_ws.setdefault(ws, []).append(summarize(rec_id, entry))

    written, summary_rows, payloads = [], [], []
    for ws in sorted(by_ws, key=lambda w: (w is not None, w or "")):
        recs = sorted(by_ws[ws],
                      key=lambda r: (r["date_wib"] or "", r["time_wib"] or ""))
        client = workspace_client(ws)
        payload = {
            "generated_utc": datetime.datetime.now(
                datetime.timezone.utc).isoformat(timespec="seconds"),
            "workspace": ws,
            "client": client,
            "count": len(recs),
            "recordings": recs,
        }
        payloads.append((ws, client, payload))
        idx_path = os.path.join(
            repo_root, "Clients", client, "recordings", "index.json")
        if dry_run:
            print(f"[index:dry-run] would write {os.path.relpath(idx_path, repo_root)} "
                  f"({client}: {len(recs)} recordings)")
        else:
            os.makedirs(os.path.dirname(idx_path), exist_ok=True)
            with open(idx_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            written.append(idx_path)
        summary_rows.append((ws, client, len(recs), idx_path))

    if cloud is not None:
        for ws, client, payload in payloads:
            cloud.push_index(payload, ws)
    return written, summary_rows


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="print intended index paths + cloud keys, write nothing")
    ap.add_argument("--cloud", default="dry-run", choices=["none", "dry-run", "drive"],
                    help="cloud backend for the index push (default: dry-run - "
                         "prints keys only; drive = real personal-drive upload)")
    ap.add_argument("--repo", default=None,
                    help="repo root to operate on (default: REPO_ROOT, "
                         "override with PSB_REPO_ROOT)")
    args = ap.parse_args()
    repo_root = os.path.abspath(args.repo) if args.repo else REPO_ROOT

    cloud = None
    if not args.dry_run:
        cloud = {"none": None, "dry-run": v0_storage.DryRunCloudStore,
                 "drive": v0_storage.GoogleDriveCloudStore}[args.cloud](repo_root)
    written, summary_rows = build_index(repo_root, dry_run=args.dry_run, cloud=cloud)

    print("v0 index summary")
    if not summary_rows:
        print("  no recordings in registry -> nothing indexed")
        return 0
    for ws, client, count, idx_path in summary_rows:
        print(f"  {client:9s} workspace={ws or 'work':9s} {count} recording(s) "
              f"-> {os.path.relpath(idx_path, repo_root)}")
    total = sum(row[2] for row in summary_rows)
    print(f"  total: {total} recording(s) across {len(summary_rows)} client index(es)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
