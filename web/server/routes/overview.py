"""Alert overview endpoint."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Query

from lib.alerts.rule_overrides import load_rule_overrides
from lib.config import resolve_credentials_path
from lib.data_root import local_config_path
from lib.envpack import load_env_pack
from lib.credentials import FileCredentials
from web.server.context import RequestContext, request_context
from web.server.data.chronic import chronic_keys
from web.server.data.consistency import check
from web.server.data.dedup_live import compute_live
from web.server.data.state_reader import (
    active_payload,
    effective_class,
    load_active,
    load_source_fail,
)
from web.server.routes.alerts import _muted_from_override
from web.server.data.timeline_reader import (
    count_new_today,
    count_resolved_today,
    distribution,
    range_bounds,
    recent_changes,
    suppressed_keys_today,
)


def router(*, state_dir: Path, repo_root: Path,
           chronic_hours: float = 24.0) -> APIRouter:
    routes = APIRouter()
    state_dir = Path(state_dir)
    repo_root = Path(repo_root)

    @routes.get("/api/overview")
    def overview(
        range_key: Literal["24h", "7d", "30d"] = Query("24h", alias="range"),
        _context: RequestContext = Depends(request_context),
    ) -> dict:
        now = datetime.now(timezone.utc)
        db_path = state_dir / "timeline.db"
        active = load_active(state_dir)
        live = compute_live(
            load_env_pack(repo_root).config_file("alert-dedup.yaml"),
            active,
            load_source_fail(state_dir),
        )
        chronic = chronic_keys(
            active, now=now, threshold_hours=chronic_hours,
        )
        overrides = load_rule_overrides(state_dir)
        effective_by_key = {}
        muted_by_key = {}
        for key, entry in active.items():
            payload = active_payload(entry or {}) or {}
            source, platform, alertname, _host = _key_fields(key)
            muted = _muted_from_override(
                overrides,
                payload.get("source") or source,
                payload.get("platform") or platform,
                payload.get("alertname") or alertname,
                bool(entry.get("muted", False)),
            )
            muted_by_key[key] = muted
            effective_by_key[key] = effective_class(entry.get("class"), muted)
        effective_classes = list(effective_by_key.values())
        chronic_class_count = effective_classes.count("chronic")
        ignore_class_count = effective_classes.count("ignore")
        since, until = range_bounds(range_key, now=now)
        change_rows, change_total = recent_changes(
            db_path, since=since, until=until, limit=10,
        )
        changes = [
            _render_change(
                row,
                active_entry=active.get(row["key"]),
                suppressed=row["key"] in live["mapping"],
                chronic=row["key"] in chronic,
                muted=muted_by_key.get(row["key"], _muted_from_override(
                    overrides,
                    row.get("source") or _key_fields(row["key"])[0],
                    row.get("platform") or _key_fields(row["key"])[1],
                    row.get("alertname") or _key_fields(row["key"])[2],
                    False,
                )),
                effective_alert_class=effective_by_key.get(row["key"]),
            )
            for row in change_rows
        ]
        stats = {
            "active": len(active),
            "active_non_chronic": len(active) - len(chronic),
            "new_today": count_new_today(db_path, now=now),
            "resolved_today": count_resolved_today(db_path, now=now),
            "suppressed_today": len(
                suppressed_keys_today(db_path, now=now)
                | set(live["mapping"])
            ),
            "chronic": len(chronic),
        }
        # Keep the established degraded response shape for an explicitly
        # unreadable timeline file; normal and not-yet-created timelines expose
        # the expanded taxonomy stats.
        if not _timeline_file_unreadable(db_path):
            stats.update({
                "chronic_class": chronic_class_count,
                "active_non_chronic_class": len(active) - chronic_class_count,
                "ignore_class": ignore_class_count,
            })
        credential_warnings = _credential_warnings(repo_root)
        return {
            "generated_at": now.isoformat(),
            "stats": stats,
            "credential_warnings": credential_warnings,
            "distribution": distribution(db_path, range_key=range_key),
            "changes": {
                "range": range_key,
                "total": change_total,
                "items": changes,
            },
            "consistency": check(state_dir, db_path),
        }

    return routes


def _credential_warnings(repo_root: Path, *, threshold_days: int = 7) -> dict:
    """Return display-only SSH credential expiry warnings."""
    try:
        credentials = FileCredentials(
            resolve_credentials_path(repo_root),
            merge_home=True,
            profile_path=local_config_path(repo_root, "auth-profiles.json"),
        )
        nodes = credentials.cache_status()
    except Exception:
        return {
            "available": False,
            "threshold_days": threshold_days,
            "count": 0,
            "expired_count": 0,
            "items": [],
        }

    now = datetime.now(timezone.utc).timestamp()
    threshold_seconds = threshold_days * 86400
    items = []
    for node in nodes:
        valid_to = int(node.get("valid_to") or 0)
        if valid_to <= 0 or valid_to - now >= threshold_seconds:
            continue
        remaining_days = int((valid_to - now) // 86400)
        items.append({
            "host": node.get("host", ""),
            "target": node.get("target", node.get("host", "")),
            "valid_to": valid_to,
            "state": "expired" if valid_to <= now else "expiring",
            "remaining_days": remaining_days,
        })
    items.sort(key=lambda item: (item["valid_to"], item["host"]))
    return {
        "available": True,
        "threshold_days": threshold_days,
        "count": len(items),
        "expired_count": sum(item["state"] == "expired" for item in items),
        "items": items,
    }


def _render_change(row: dict, *, active_entry: dict | None,
                   suppressed: bool, chronic: bool,
                   muted: bool = False,
                   effective_alert_class: str | None = None) -> dict:
    payload = active_payload(active_entry or {}) or {}
    source, platform, alertname, host = _key_fields(row["key"])
    alert_class = (
        (active_entry or {}).get("class") or row.get("alert_class") or "watch"
    )
    return {
        "key": row["key"],
        "source": payload.get("source") or row.get("source") or source,
        "platform": payload.get("platform") or row.get("platform") or platform,
        "alertname": payload.get("alertname") or row.get("alertname") or alertname,
        "host": (
            payload.get("host") or payload.get("host_raw")
            or row.get("host") or host
        ),
        "severity": (
            payload.get("severity") or (active_entry or {}).get("severity")
            or row.get("severity")
        ),
        "service": payload.get("service") or row.get("service"),
        "change_type": row["type"],
        "changed_at": row["ts"],
        "change_count": row["change_count"],
        "occurrence_count": int(row.get("occurrence_count") or 0),
        "status": (
            "suppressed" if suppressed else "active" if active_entry else "resolved"
        ),
        "chronic": chronic if active_entry else False,
        "muted": muted,
        "class": alert_class,
        "effective_class": effective_alert_class or effective_class(
            alert_class, muted,
        ),
        "first_seen": (
            (active_entry or {}).get("first_seen") or row.get("first_seen")
        ),
    }


def _key_fields(key: str) -> tuple[str, str, str, str | None]:
    parts = str(key).split(":", 3)
    return tuple(parts) if len(parts) == 4 else ("", "", "", None)


def _timeline_file_unreadable(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        with path.open("rb") as handle:
            return handle.read(16) != b"SQLite format 3\x00"
    except OSError:
        return True
