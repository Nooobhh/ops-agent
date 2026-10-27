"""Read-only access to the alert state snapshot."""
from __future__ import annotations

from pathlib import Path

from lib.alerts.state import AlertsState

ALERT_CLASSES = {"watch", "chronic", "ignore", "flapping"}


def load_active(state_dir: Path) -> dict:
    state = AlertsState.load(Path(state_dir) / "alerts.json")
    return dict(state.active)


def load_source_fail(state_dir: Path) -> dict:
    state = AlertsState.load(Path(state_dir) / "alerts.json")
    return dict(state.source_fail)


def load_last_tick_at(state_dir: Path) -> str | None:
    state = AlertsState.load(Path(state_dir) / "alerts.json")
    return state.last_tick_at


def active_payload(entry: dict) -> dict | None:
    payload = (entry or {}).get("event")
    return payload if isinstance(payload, dict) else None


def effective_class(alert_class, muted: bool) -> str:
    if muted:
        return "ignore"
    value = str(alert_class or "watch")
    return value if value in ALERT_CLASSES else "watch"
