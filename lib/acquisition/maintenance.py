"""Daily acquisition storage sealing and retention."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from lib.alerts import timeline
from lib.alerts.history import prune_event_logs
from lib.alerts.state import AlertsState
from .settings import ensure_distinct_storage_roots, load_settings, resolve_storage_path
from .store import prune_snapshots, seal_completed_days


def run_maintenance(repo_root: Path, state_dir: Path, *, now=None) -> dict:
    repo_root = Path(repo_root)
    state_dir = Path(state_dir)
    settings = load_settings(state_dir)
    ensure_distinct_storage_roots(repo_root, settings)
    resources = settings["resources"]
    root = resolve_storage_path(repo_root, resources["snapshot_root"])
    sealed = seal_completed_days(
        root, now=now, compression=resources.get("compression", "gzip"),
    )
    removed = prune_snapshots(
        root, retention_days=int(resources["retention_days"]), now=now,
    )
    alerts = settings["alerts"]
    alert_root = resolve_storage_path(repo_root, alerts["history_root"])
    now_dt = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    cutoff = now_dt - timedelta(days=int(alerts["retention_days"]))
    alert_state = AlertsState.load(state_dir / "alerts.json")
    timeline_removed = timeline.prune(state_dir / "timeline.db", cutoff)
    alert_files_removed = prune_event_logs(
        alert_root, cutoff, preserve_keys=set(alert_state.active),
    )
    return {
        "sealed": [str(path) for path in sealed],
        "removed": [str(path) for path in removed],
        "alert_files_removed": alert_files_removed,
        "timeline_events_removed": timeline_removed,
    }
