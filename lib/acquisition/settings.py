"""Machine-local acquisition settings with atomic persistence."""
from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from lib.atomic_file import atomic_write_json
from lib.data_root import resolve_records_root
from lib.envpack import env_timezone
from lib.file_lock import FileLock


# timezone 由 default_settings() 填环境时区(env.yaml),不读宿主时区
_DEFAULTS = {
    "schema_version": 1,
    "timezone": "",
    "alerts": {
        "enabled": True,
        "interval_seconds": 300,
        "history_root": "alerts",
        "retention_days": 90,
    },
    "resources": {
        "enabled": True,
        "interval_seconds": 300,
        "snapshot_root": "telemetry/resources",
        "retention_days": 90,
        "compression": "gzip",
        "max_backfill_minutes": 90,
        "risk_notifications_enabled": False,
    },
    "maintenance": {
        "enabled": True,
        "hour": 2,
        "minute": 30,
    },
}


class AcquisitionSettingsError(ValueError):
    pass


def default_settings() -> dict:
    defaults = deepcopy(_DEFAULTS)
    defaults["timezone"] = env_timezone().key
    return defaults


def load_settings(state_dir: Path) -> dict:
    """Load the full settings document; missing/corrupt files use safe defaults."""
    state_dir = Path(state_dir)
    path = state_dir / "acquisition-settings.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        raw = {}
    settings = _deep_merge(default_settings(), raw if isinstance(raw, Mapping) else {})

    # Compatibility migration: an explicit legacy alert gate wins until the
    # acquisition settings document is first saved.
    if not path.is_file():
        marker = state_dir / "poll-enabled.json"
        try:
            gate = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            gate = {}
        if isinstance(gate, Mapping) and isinstance(gate.get("enabled"), bool):
            settings["alerts"]["enabled"] = gate["enabled"]
    return validate_settings(settings)


def save_settings(state_dir: Path, settings: Mapping[str, Any]) -> dict:
    state_dir = Path(state_dir)
    normalized = validate_settings(settings)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "acquisition-settings.json"
    lock_path = state_dir / "acquisition-settings.lock"
    with FileLock(str(lock_path), timeout=10):
        atomic_write_json(path, normalized)
    return normalized


def validate_settings(settings: Mapping[str, Any]) -> dict:
    value = _deep_merge(default_settings(), settings)
    if int(value.get("schema_version", 0)) != 1:
        raise AcquisitionSettingsError("仅支持采集设置 schema_version=1")
    timezone = env_timezone().key
    if value.get("timezone") != timezone:
        raise AcquisitionSettingsError(f"采集存储时区固定为环境时区 {timezone}")

    alerts = value["alerts"]
    resources = value["resources"]
    maintenance = value["maintenance"]
    for section_name, section in (("alerts", alerts), ("resources", resources)):
        if not isinstance(section.get("enabled"), bool):
            raise AcquisitionSettingsError(f"{section_name}.enabled 必须是布尔值")
        interval = _bounded_int(section.get("interval_seconds"), 60, 3600)
        retention = _bounded_int(section.get("retention_days"), 1, 3650)
        section["interval_seconds"] = interval
        section["retention_days"] = retention

    alerts["history_root"] = _validate_path_text(alerts.get("history_root"), "alerts.history_root")
    resources["snapshot_root"] = _validate_path_text(
        resources.get("snapshot_root"), "resources.snapshot_root",
    )
    resources["max_backfill_minutes"] = _bounded_int(
        resources.get("max_backfill_minutes"), 0, 120,
    )
    if resources.get("compression") not in {"gzip", "none"}:
        raise AcquisitionSettingsError("resources.compression 只能是 gzip 或 none")
    if not isinstance(resources.get("risk_notifications_enabled"), bool):
        raise AcquisitionSettingsError("resources.risk_notifications_enabled 必须是布尔值")

    if not isinstance(maintenance.get("enabled"), bool):
        raise AcquisitionSettingsError("maintenance.enabled 必须是布尔值")
    maintenance["hour"] = _bounded_int(maintenance.get("hour"), 0, 23)
    maintenance["minute"] = _bounded_int(maintenance.get("minute"), 0, 59)
    return value


def resolve_storage_path(repo_root: Path, value: str) -> Path:
    """相对存储路径(缺省 alerts、telemetry/resources)按记录根解析。"""
    path = Path(_strip_legacy_records_prefix(value)).expanduser()
    resolved = path.resolve() if path.is_absolute() else (resolve_records_root(repo_root) / path).resolve()
    if resolved == Path(resolved.anchor):
        raise AcquisitionSettingsError("存储路径不能是文件系统根目录")
    return resolved


def path_status(repo_root: Path, value: str) -> dict:
    resolved = resolve_storage_path(repo_root, value)
    probe = resolved
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    writable = probe.is_dir() and os.access(probe, os.W_OK)
    try:
        stat = os.statvfs(probe)
        available_bytes = int(stat.f_bavail * stat.f_frsize)
    except OSError:
        available_bytes = None
    return {
        "configured": value,
        "resolved": str(resolved),
        "exists": resolved.is_dir(),
        "writable": writable,
        "available_bytes": available_bytes,
    }


def ensure_distinct_storage_roots(repo_root: Path, settings: Mapping[str, Any]) -> None:
    alerts = resolve_storage_path(repo_root, settings["alerts"]["history_root"])
    resources = resolve_storage_path(repo_root, settings["resources"]["snapshot_root"])
    if alerts == resources:
        raise AcquisitionSettingsError("告警历史路径与资源快照路径不能相同")


def _deep_merge(defaults: Mapping[str, Any], raw: Mapping[str, Any]) -> dict:
    result = deepcopy(dict(defaults))
    for key, item in raw.items():
        if key in result and isinstance(result[key], dict) and isinstance(item, Mapping):
            result[key] = _deep_merge(result[key], item)
        else:
            result[key] = deepcopy(item)
    return result


def _bounded_int(value, lower: int, upper: int) -> int:
    if isinstance(value, bool):
        raise AcquisitionSettingsError(f"数值必须在 {lower}..{upper} 之间")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise AcquisitionSettingsError(f"数值必须在 {lower}..{upper} 之间") from exc
    if not lower <= parsed <= upper:
        raise AcquisitionSettingsError(f"数值必须在 {lower}..{upper} 之间")
    return parsed


def _validate_path_text(value, field: str) -> str:
    text = str(value or "").strip()
    if not text or "\x00" in text:
        raise AcquisitionSettingsError(f"{field} 不能为空")
    return _strip_legacy_records_prefix(text)


def _strip_legacy_records_prefix(value: str) -> str:
    """旧设置按数据根存 records/alerts 等;改按记录根解析后去掉 records/ 前缀,指向不变。"""
    path = Path(value)
    if not path.is_absolute() and len(path.parts) > 1 and path.parts[0] == "records":
        return Path(*path.parts[1:]).as_posix()
    return value

