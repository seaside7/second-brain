#!/usr/bin/env python3
"""Recording data model + the single registry writer for the workspace-aware
pipeline (v0).

v0 is deliberately small: a Recording carries enough metadata for the storage
and index layers to place it per-workspace (workspace -> client -> date), and
insert_recording() is the one writer every path funnels through so the registry
shape stays uniform. `workspace` is None for the legacy on-machine pipeline
(client "Work", existing behavior) and "personal"/"samudera"/"catalyze" for the
workspace-aware path.
"""
import datetime
import json
import os
from dataclasses import dataclass, field

from common import slugify, workspace_client


@dataclass
class Recording:
    recording_id: str
    source: str                    # local-recorder | vexa | fathom | smoke-test
    title: str
    date_wib: str                  # YYYY-MM-DD
    time_wib: str                  # HH:MM
    duration_min: int
    workspace: str | None = None   # None = legacy "Work" client
    client: str = field(init=False)
    matched_meeting: str | None = None
    participants: list[str] = field(default_factory=list)
    language: str | None = None
    local_path: str = ""           # raw audio source (repo-absolute)
    cloud_path: str | None = None  # cloud key once placed
    cloud_status: str = "pending"  # pending | local-only | dry-run | uploaded | failed
    created_utc: str = field(default_factory=lambda: datetime.datetime.now(
        datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    def __post_init__(self):
        self.client = workspace_client(self.workspace)

    def storage_slug(self) -> str:
        return slugify(self.matched_meeting or self.title)

    def to_registry_entry(self) -> dict:
        return {
            "recording_id": self.recording_id,
            "source": self.source,
            "workspace": self.workspace,
            "client": self.client,
            "date_wib": self.date_wib,
            "time_wib": self.time_wib,
            "duration": f"{max(1, self.duration_min)} min",
            "title": self.title,
            "matched_meeting": self.matched_meeting,
            "participants": self.participants,
            "transcript_language": self.language,
            "local_path": self.local_path,
            "cloud_path": self.cloud_path,
            "cloud_status": self.cloud_status,
            "last_synced_utc": self.created_utc,
        }


def insert_recording(registry_path: str, rec: Recording, overwrite: bool = False) -> str:
    """Add `rec` to the registry under its recording_id (the single writer).

    Idempotency guard: a second insert of the same id raises unless overwrite
    is set. Creates the registry file if missing (tests, fresh setups), so the
    file is never a precondition.
    """
    registry = {}
    if os.path.exists(registry_path):
        with open(registry_path, encoding="utf-8-sig") as f:
            registry = json.load(f)
    if rec.recording_id in registry and not overwrite:
        raise ValueError(
            f"recording {rec.recording_id!r} already in registry (overwrite=False)")
    registry[rec.recording_id] = rec.to_registry_entry()
    os.makedirs(os.path.dirname(registry_path), exist_ok=True)
    tmp = registry_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2, ensure_ascii=False)
    os.replace(tmp, registry_path)
    return rec.recording_id
