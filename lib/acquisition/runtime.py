"""Atomic scheduler/collector runtime status snapshots."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from lib.atomic_file import atomic_write_json
from lib.file_lock import FileLock


def load_runtime(state_dir: Path) -> dict:
    path = Path(state_dir) / "acquisition-runtime.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        data = {}
    return data if isinstance(data, dict) else {}


def update_job(state_dir: Path, job: str, **fields) -> dict:
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "acquisition-runtime.json"
    with FileLock(str(state_dir / "acquisition-runtime.lock"), timeout=10):
        data = load_runtime(state_dir)
        jobs = data.setdefault("jobs", {})
        current = jobs.setdefault(job, {})
        current.update(fields)
        data["schema_version"] = 1
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        atomic_write_json(path, data)
    return data

