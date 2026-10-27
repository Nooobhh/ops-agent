"""One-shot notifications for continuous resource risk findings."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from lib.alerts.notify import MultiNotifier, render_resource_risk_block
from lib.alerts.templates import effective_template, load_notification_settings
from lib.atomic_file import atomic_write_json
from lib.file_lock import FileLock


_STATE_FILE = "resource-risk-notifications.json"


def load_resource_risk_notification_state(state_dir: Path) -> dict:
    """Return non-sensitive delivery status for the settings page."""
    state = _load(Path(state_dir) / _STATE_FILE)
    active = state.get("active")
    active = active if isinstance(active, Mapping) else {}
    notified_count = sum(
        bool(item.get("notified_at"))
        for item in active.values()
        if isinstance(item, Mapping)
    )
    return {
        "active_count": len(active),
        "notified_count": notified_count,
        "pending_count": len(active) - notified_count,
        "updated_at": state.get("updated_at"),
    }


def sync_resource_risk_notifications(
    risks: Mapping[str, Any],
    state_dir: Path,
    *,
    enabled: bool,
    notifier: MultiNotifier | Any = None,
    template: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Sync the active risk set and send only the first message per risk segment.

    A finding key is the identity of one continuous risk segment. It is removed
    as soon as the analyzer no longer reports it, so a later reappearance is a
    new segment and produces one new notification.
    """
    now_dt = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    now_iso = now_dt.astimezone(timezone.utc).isoformat()
    state_dir = Path(state_dir)
    path = state_dir / _STATE_FILE
    with FileLock(str(state_dir / "resource-risk-notifications.lock"), timeout=10):
        previous = _load(path)
        previous_active = previous.get("active") or {}
        if not isinstance(previous_active, Mapping):
            previous_active = {}
        current_findings = {
            str(item.get("key")): item
            for item in (risks.get("findings") or [])
            if isinstance(item, Mapping) and item.get("key")
        }
        resolved = len(set(previous_active) - set(current_findings))
        active = {}
        pending = []
        for key, finding in current_findings.items():
            old = previous_active.get(key)
            entry = dict(old) if isinstance(old, Mapping) else {
                "first_seen_at": now_iso,
                "notified_at": None,
            }
            entry.update({
                "last_seen_at": now_iso,
                "severity": finding.get("severity"),
                "title": finding.get("title"),
            })
            if not entry.get("notified_at") and enabled and _has_delivery(notifier):
                pending.append(finding)
            active[key] = entry

        blocks = [
            render_resource_risk_block(
                finding,
                template=template or effective_template(load_notification_settings(state_dir)),
                now=now_dt,
            )
            for finding in pending
        ]
        if blocks and notifier is not None:
            notifier.dispatch(blocks)
            for finding in pending:
                entry = active[str(finding["key"])]
                entry["notified_at"] = now_iso

        payload = {
            "schema_version": 1,
            "updated_at": now_iso,
            "active": active,
        }
        atomic_write_json(path, payload)
    return {
        "notified": len(pending) if blocks and notifier is not None else 0,
        "active": len(active),
        "resolved": resolved,
    }


def _has_delivery(notifier) -> bool:
    if notifier is None:
        return False
    channels = getattr(notifier, "notifiers", None)
    return channels is None or bool(channels)


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        value = {}
    return value if isinstance(value, dict) else {}

