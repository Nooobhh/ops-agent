"""Lifecycle-aware alert JSONL retention."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def prune_event_logs(root: Path, cutoff, *, preserve_keys=None) -> int:
    root = Path(root)
    if not root.exists():
        return 0
    cutoff_date = cutoff.date() if isinstance(cutoff, datetime) else cutoff
    preserve_keys = set(preserve_keys or set())
    removed = 0
    for path in root.glob("*.jsonl"):
        try:
            file_date = datetime.strptime(path.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if file_date >= cutoff_date:
            continue
        if preserve_keys and contains_open_key(path, preserve_keys):
            continue
        path.unlink()
        removed += 1
    return removed


def contains_open_key(path: Path, preserve_keys: set[str]) -> bool:
    """Conservatively retain a day containing the start of any active key."""
    try:
        with Path(path).open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("type") == "new" and row.get("key") in preserve_keys:
                    return True
    except OSError:
        return True
    return False
