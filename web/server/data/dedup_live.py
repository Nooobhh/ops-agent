"""Recompute current cross-source suppression without persisting it."""
from __future__ import annotations

from pathlib import Path

from lib.alerts.dedup import apply, load_rules
from lib.alerts.source_notify import event_source_id


def compute_live(rules_path: Path, active: dict, source_fail: dict) -> dict:
    excluded = sorted(
        str(source) for source, count in (source_fail or {}).items()
        if int(count or 0) > 0
    )
    excluded_set = set(excluded)
    eligible = {
        key: entry for key, entry in (active or {}).items()
        if _source_id(entry) not in excluded_set
    }
    path = Path(rules_path)
    if not path.exists():
        mapping = {}
    else:
        mapping = apply(load_rules(path), eligible)
    return {"mapping": mapping, "excluded_sources": excluded}


def _source_id(entry: dict) -> str:
    event = (entry or {}).get("event") or {}
    return event_source_id(str(event.get("source") or ""), event.get("platform"))
