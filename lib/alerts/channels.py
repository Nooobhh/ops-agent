"""Persistent notification-channel configuration shared by web and alert polling."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from lib.atomic_file import atomic_write_json
from lib.file_lock import FileLock


CHANNEL_TYPES = ("wecom", "feishu")

_CREDENTIAL_FIELDS = {
    "wecom": ("webhook_key",),
    "feishu": ("webhook", "secret"),
}
_WRITE_LOCK = threading.Lock()


def channels_path(state_dir: Path) -> Path:
    return Path(state_dir) / "notify-channels.json"


def load_channels(state_dir: Path) -> dict:
    path = channels_path(state_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    channels = data.get("channels")
    return dict(channels) if isinstance(channels, dict) else {}


def save_channel(
    state_dir: Path,
    channel_type: str,
    *,
    enabled: bool,
    credentials_update: dict | None = None,
    now: datetime | None = None,
) -> dict:
    if channel_type not in CHANNEL_TYPES:
        raise ValueError(f"unsupported channel type: {channel_type}")

    state_dir = Path(state_dir)
    with _WRITE_LOCK:
        with FileLock(str(state_dir / "notify-channels.lock")):
            channels = load_channels(state_dir)
            previous = channels.get(channel_type)
            entry = dict(previous) if isinstance(previous, dict) else {}
            if credentials_update is not None:
                for field in _CREDENTIAL_FIELDS[channel_type]:
                    if field not in credentials_update:
                        continue
                    value = credentials_update[field]
                    if value == "" and field in {"webhook_key", "webhook"}:
                        continue
                    entry[field] = value
            entry["enabled"] = bool(enabled)
            entry["updated_at"] = _utc_iso(now)
            entry["updated_by"] = "web"
            channels[channel_type] = entry
            atomic_write_json(channels_path(state_dir), {"version": 1, "channels": channels})
            return dict(entry)


def migrate_wecom(
    state_dir: Path,
    webhook_key: str,
    *,
    now: datetime | None = None,
) -> None:
    state_dir = Path(state_dir)
    with _WRITE_LOCK:
        lock = FileLock(str(state_dir / "notify-channels.lock"))
        try:
            lock.__enter__()
        except (OSError, TimeoutError):
            return
        try:
            channels = load_channels(state_dir)
            if "wecom" in channels:
                return
            channels["wecom"] = {
                "enabled": True,
                "webhook_key": str(webhook_key),
                "updated_at": _utc_iso(now),
                "updated_by": "migration",
            }
            atomic_write_json(channels_path(state_dir), {"version": 1, "channels": channels})
        finally:
            lock.__exit__(None, None, None)


def _utc_iso(now: datetime | None) -> str:
    value = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()

