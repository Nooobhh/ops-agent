"""Pure helpers for duration-based chronic active-alert classification."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def is_chronic(entry: dict, *, now: datetime, threshold_hours: float) -> bool:
    first_seen = entry.get("first_seen")
    if not first_seen:
        return False
    try:
        started = datetime.fromisoformat(str(first_seen))
    except (TypeError, ValueError):
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc) - started.astimezone(timezone.utc) >= timedelta(
        hours=threshold_hours,
    )


def chronic_keys(active: dict, *, now: datetime, threshold_hours: float) -> set[str]:
    return {
        str(key)
        for key, entry in active.items()
        if is_chronic(entry, now=now, threshold_hours=threshold_hours)
    }
