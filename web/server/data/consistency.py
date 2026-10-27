"""Compare the state snapshot and timeline lifecycle projection."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from web.server.data.state_reader import load_active
from web.server.data.timeline_reader import open_key_details


RECENT_WINDOW = timedelta(hours=24)


def check(state_dir: Path, db_path: Path, *, now=None) -> dict:
    active = load_active(state_dir)
    state_keys = set(active)
    timeline = open_key_details(db_path)
    timeline_keys = set(timeline)
    if state_keys == timeline_keys:
        return {"ok": True, "detail": None}

    now_dt = _as_datetime(now or datetime.now(timezone.utc))
    cutoff = now_dt - RECENT_WINDOW
    state_only = sorted(state_keys - timeline_keys)
    timeline_only = sorted(timeline_keys - state_keys)
    recent_state = [key for key in state_only if _is_recent(active[key].get("last_seen"), cutoff)]
    recent_timeline = [
        key for key in timeline_only
        if _is_recent(timeline[key].get("last_event_at"), cutoff)
    ]
    stale_state = [key for key in state_only if key not in recent_state]
    stale_timeline = [key for key in timeline_only if key not in recent_timeline]
    details = []
    _append_difference(details, "近期漂移", recent_state, recent_timeline)
    _append_difference(details, "陈旧未闭合", stale_state, stale_timeline)
    return {
        "ok": False,
        "level": "warning" if recent_state or recent_timeline else "info",
        "detail": "；".join(details),
    }


def _append_difference(details, label, state_only, timeline_only) -> None:
    parts = []
    if state_only:
        parts.append("状态机有而时间线无: " + ", ".join(state_only))
    if timeline_only:
        parts.append("时间线有而状态机无: " + ", ".join(timeline_only))
    if parts:
        details.append(label + "（" + "；".join(parts) + "）")


def _is_recent(value, cutoff: datetime) -> bool:
    try:
        return _as_datetime(value) >= cutoff
    except (TypeError, ValueError):
        return False


def _as_datetime(value) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
