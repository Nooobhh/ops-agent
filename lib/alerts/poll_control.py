"""Persistent alert acquisition gate.

采集已改为中台内嵌 24x7 调度。启用状态跨机器重启保留；`since_boot`
仅为旧 API 兼容字段，不再参与 effective 判定。
"""
from __future__ import annotations

import ctypes
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lib.atomic_file import atomic_write_json


_AUTO_BOOT_TIME = object()


def load_status(marker_path: Path, *, boot_time=_AUTO_BOOT_TIME) -> dict:
    path = Path(marker_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        data = {}

    enabled = data.get("enabled", True) is True
    updated_at = _parse_datetime(data.get("updated_at"))
    if not enabled:
        reason = "告警轮询已停止"
    else:
        reason = None
    return {
        "enabled": enabled,
        "effective": enabled,
        "reason": reason,
        "updated_at": updated_at.isoformat() if updated_at else None,
        "since_boot": True,
    }


def set_enabled(marker_path: Path, enabled: bool, *, now=None,
                boot_time=_AUTO_BOOT_TIME) -> dict:
    path = Path(marker_path)
    now_dt = _as_utc(now if isinstance(now, datetime) else datetime.now(timezone.utc))
    payload = {
        "enabled": bool(enabled),
        "updated_at": now_dt.isoformat(),
        "updated_by": "web",
    }
    atomic_write_json(path, payload)
    return load_status(path, boot_time=boot_time)


def system_boot_time() -> datetime | None:
    try:
        if sys.platform == "win32":
            uptime_ms = int(ctypes.windll.kernel32.GetTickCount64())
            return datetime.now(timezone.utc) - timedelta(milliseconds=uptime_ms)
        if sys.platform == "darwin":
            output = subprocess.run(
                ["sysctl", "-n", "kern.boottime"],
                check=True, capture_output=True, text=True, timeout=5,
            ).stdout
            match = re.search(r"sec\s*=\s*(\d+)", output)
            return datetime.fromtimestamp(int(match.group(1)), timezone.utc) if match else None
        if sys.platform.startswith("linux"):
            for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
                if line.startswith("btime "):
                    return datetime.fromtimestamp(int(line.split()[1]), timezone.utc)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return None


def _parse_datetime(value) -> datetime | None:
    if not value:
        return None
    try:
        return _as_utc(datetime.fromisoformat(str(value)))
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
