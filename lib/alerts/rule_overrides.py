"""Persistent rule-level alert overrides shared by polling and the web UI."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from lib.atomic_file import atomic_write_json
from lib.file_lock import FileLock


_VALID_SEVERITIES = {"critical", "warning", "info"}
_WRITE_LOCK = threading.Lock()


def load_rule_overrides(state_dir: Path) -> dict:
    """Return the override mapping, failing soft on absent or malformed state."""
    path = Path(state_dir) / "alert-rule-overrides.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    overrides = data.get("overrides")
    return dict(overrides) if isinstance(overrides, dict) else {}


def set_rule_override(
    state_dir: Path,
    key: str,
    *,
    notify: bool | None = None,
    severity: str | None = None,
    clear: bool = False,
    clear_severity: bool = False,
) -> dict | None:
    """Partially update one rule override or remove it with ``clear=True``."""
    if severity is not None and severity not in _VALID_SEVERITIES:
        raise ValueError(f"unsupported severity: {severity}")
    if clear_severity and severity is not None:
        raise ValueError("severity and clear_severity cannot be used together")
    if notify is not None and not isinstance(notify, bool):
        raise ValueError("notify must be a boolean")

    state_dir = Path(state_dir)
    with _WRITE_LOCK:
        with FileLock(str(state_dir / "alert-rule-overrides.lock")):
            overrides = load_rule_overrides(state_dir)
            if clear:
                if key in overrides:
                    overrides.pop(key, None)
                    _save(state_dir, overrides)
                return None

            previous = overrides.get(key)
            entry = dict(previous) if isinstance(previous, dict) else {
                "notify": True,
                "severity": None,
            }
            changed = not isinstance(previous, dict)
            if notify is not None and entry.get("notify") != notify:
                entry["notify"] = notify
                changed = True
            if severity is not None and entry.get("severity") != severity:
                entry["severity"] = severity
                changed = True
            if clear_severity and entry.get("severity") is not None:
                entry["severity"] = None
                changed = True
            if not changed:
                return dict(entry)

            entry["updated_at"] = datetime.now(timezone.utc).isoformat()
            entry["updated_by"] = "web"
            overrides[str(key)] = entry
            _save(state_dir, overrides)
            return dict(entry)


def _save(state_dir: Path, overrides: dict) -> None:
    atomic_write_json(
        state_dir / "alert-rule-overrides.json",
        {"version": 1, "overrides": overrides},
    )
