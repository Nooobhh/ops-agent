#!/usr/bin/env python3
"""Collect one read-only Prometheus resource snapshot tick."""
from __future__ import annotations

import argparse
import json
import sys
import time
import yaml
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.acquisition.resources import collect_resource_snapshots, save_catalog
from lib.data_root import resolve_data_root
from lib.envpack import load_env_pack
from lib.acquisition.runtime import update_job
from lib.acquisition.resource_notifications import sync_resource_risk_notifications
from lib.acquisition.settings import load_settings, resolve_storage_path
from lib.acquisition.store import append_snapshot, latest_snapshots
from lib.acquisition.analyzer import analyze_resource_history, save_risks
from lib.acquisition.thresholds import load_overrides, merge_overrides
from lib.alerts.templates import effective_template, load_notification_settings
from lib.file_lock import FileLock
from scripts.alert_poll import build_notifier


def run(argv=None, *, now=None) -> int:
    args = _parse_args(argv)
    repo_root = Path(args.repo_root).resolve() if args.repo_root else Path(__file__).resolve().parents[1]
    state_dir = Path(args.state_dir)
    if not state_dir.is_absolute():
        state_dir = resolve_data_root(repo_root) / state_dir
    settings = load_settings(state_dir)
    resource_settings = settings["resources"]
    if not resource_settings["enabled"] and not args.dry_run:
        print(json.dumps({"ok": True, "skipped": True, "reason": "资源采集已停止"}, ensure_ascii=False))
        return 0

    lock = None
    if not args.dry_run:
        lock = FileLock(str(state_dir / "resource_collect.lock"), timeout=0)
        try:
            lock.__enter__()
        except TimeoutError:
            print(json.dumps({
                "ok": True, "skipped": True,
                "reason": "上一轮资源采集仍在运行",
            }, ensure_ascii=False))
            return 0

    started = datetime.now(timezone.utc)
    started_clock = time.monotonic()
    if not args.dry_run:
        update_job(
            state_dir, "resources", state="running", last_started_at=started.isoformat(),
        )
    try:
        effective_now = now if isinstance(now, datetime) else datetime.now(timezone.utc)
        root = resolve_storage_path(repo_root, resource_settings["snapshot_root"])
        backfilled = []
        data_gap = None
        if not args.dry_run:
            latest = latest_snapshots(root)
            prior_values = [
                _parse_time(item.get("scheduled_at") or item.get("collected_at"))
                for item in latest.values()
                if item.get("scheduled_at") or item.get("collected_at")
            ]
            prior_values = [item for item in prior_values if item is not None]
            current_bucket = _floor_bucket(effective_now)
            if prior_values:
                prior = min(prior_values)
                gap_minutes = (current_bucket - prior).total_seconds() / 60
                maximum = int(resource_settings.get("max_backfill_minutes", 90))
                if 5 < gap_minutes <= maximum:
                    cursor = _floor_bucket(prior) + timedelta(minutes=5)
                    while cursor < current_bucket:
                        rows, _catalog = collect_resource_snapshots(
                            repo_root, now=cursor, mode="backfill",
                        )
                        for row in rows:
                            append_snapshot(root, row)
                        backfilled.append(cursor.isoformat())
                        cursor += timedelta(minutes=5)
                elif gap_minutes > maximum:
                    data_gap = {
                        "from": prior.isoformat(), "to": current_bucket.isoformat(),
                        "minutes": round(gap_minutes, 1), "backfill_limit_minutes": maximum,
                    }
        snapshots, catalog = collect_resource_snapshots(repo_root, now=effective_now)
        if args.dry_run:
            print(json.dumps({
                "ok": True,
                "dry_run": True,
                "platforms": [
                    {
                        "platform": row["platform"], "status": row["status"],
                        "coverage": row["coverage"], "errors": len(row["errors"]),
                        "business_metrics": len(row.get("business_metrics") or []),
                    }
                    for row in snapshots
                ],
                "catalog_services": len(catalog["services"]),
                "unmonitored_components": len(catalog["unmonitored_components"]),
            }, ensure_ascii=False))
            return 0

        paths = [str(append_snapshot(root, snapshot)) for snapshot in snapshots]
        save_catalog(state_dir / "resource-catalog.json", catalog)
        metric_config = yaml.safe_load(
            load_env_pack(repo_root).config_file("resource-metrics.yaml").read_text(encoding="utf-8")
        ) or {}
        metric_config = merge_overrides(metric_config, load_overrides(state_dir))
        risks = analyze_resource_history(root, metric_config, now=now)
        save_risks(state_dir / "resource-risks.json", risks)
        risk_notifications_enabled = bool(
            resource_settings.get("risk_notifications_enabled", False),
        )
        risk_notification_result = sync_resource_risk_notifications(
            risks,
            state_dir,
            enabled=risk_notifications_enabled,
            notifier=build_notifier(
                {}, state_dir, repo_root=repo_root,
            ) if risk_notifications_enabled else None,
            template=effective_template(load_notification_settings(state_dir)),
            now=effective_now,
        )
        completed = datetime.now(timezone.utc)
        update_job(
            state_dir, "resources", state="running",
            last_completed_at=completed.isoformat(), last_success_at=completed.isoformat(),
            duration_seconds=round(time.monotonic() - started_clock, 3),
            consecutive_failures=0, last_error=None,
            last_snapshot_paths=paths,
            backfilled_buckets=len(backfilled), data_gap=data_gap,
        )
        print(json.dumps({
            "ok": True,
            "snapshots": len(snapshots),
            "paths": paths,
            "coverage": {row["platform"]: row["coverage"] for row in snapshots},
            "business_metrics": {
                row["platform"]: len(row.get("business_metrics") or [])
                for row in snapshots
            },
            "risk_findings": len(risks["findings"]),
            "risk_notifications": risk_notification_result,
            "backfilled_buckets": len(backfilled),
            "data_gap": data_gap,
        }, ensure_ascii=False))
        return 0
    except Exception as exc:
        if args.dry_run:
            print(json.dumps({
                "ok": False, "error": type(exc).__name__, "detail": str(exc),
            }, ensure_ascii=False), file=sys.stderr)
            return 1
        runtime = update_job(state_dir, "resources")
        previous = ((runtime.get("jobs") or {}).get("resources") or {})
        update_job(
            state_dir, "resources", state="degraded",
            last_completed_at=datetime.now(timezone.utc).isoformat(),
            duration_seconds=round(time.monotonic() - started_clock, 3),
            consecutive_failures=int(previous.get("consecutive_failures", 0)) + 1,
            last_error=f"{type(exc).__name__}: {exc}",
        )
        print(json.dumps({
            "ok": False, "error": type(exc).__name__, "detail": str(exc),
        }, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        if lock is not None:
            lock.__exit__(None, None, None)


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root")
    parser.add_argument("--state-dir", default="state")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def _parse_time(value):
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _floor_bucket(value: datetime) -> datetime:
    value = value.astimezone(timezone.utc)
    return value.replace(minute=value.minute - value.minute % 5, second=0, microsecond=0)


if __name__ == "__main__":
    raise SystemExit(run())
