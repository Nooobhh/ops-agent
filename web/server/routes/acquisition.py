"""Acquisition settings, runtime health and resource snapshot endpoints."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
import yaml

from lib.acquisition.runtime import load_runtime
from lib.acquisition.resource_notifications import load_resource_risk_notification_state
from lib.acquisition.settings import (
    AcquisitionSettingsError,
    ensure_distinct_storage_roots,
    load_settings,
    path_status,
    resolve_storage_path,
    save_settings,
)
from lib.acquisition.store import iter_snapshots, latest_snapshots, storage_summary
from lib.acquisition.thresholds import (
    ResourceThresholdError,
    resource_alert_settings,
    save_overrides,
)
from lib.alerts.poll_control import set_enabled
from lib.envpack import env_timezone, load_env_pack
from web.server.context import RequestContext, request_context
from web.server.data.resource_refs import attach_host_refs, host_ref_resolver


class AlertAcquisitionBody(BaseModel):
    enabled: bool = True
    interval_seconds: int = Field(default=300, ge=60, le=3600)
    history_root: str = Field(default="alerts", min_length=1, max_length=1000)
    retention_days: int = Field(default=90, ge=1, le=3650)


class ResourceAcquisitionBody(BaseModel):
    enabled: bool = True
    interval_seconds: int = Field(default=300, ge=60, le=3600)
    snapshot_root: str = Field(
        default="telemetry/resources", min_length=1, max_length=1000,
    )
    retention_days: int = Field(default=90, ge=1, le=3650)
    compression: str = "gzip"
    max_backfill_minutes: int = Field(default=90, ge=0, le=120)
    risk_notifications_enabled: bool = False


class MaintenanceBody(BaseModel):
    enabled: bool = True
    hour: int = Field(default=2, ge=0, le=23)
    minute: int = Field(default=30, ge=0, le=59)


class AcquisitionSettingsBody(BaseModel):
    schema_version: int = 1
    timezone: str = Field(default_factory=lambda: env_timezone().key)
    alerts: AlertAcquisitionBody
    resources: ResourceAcquisitionBody
    maintenance: MaintenanceBody


class ResourceAlertSettingsBody(BaseModel):
    schema_version: int = 1
    overrides: dict = Field(default_factory=dict)


def router(*, state_dir: Path, repo_root: Path) -> APIRouter:
    routes = APIRouter()
    state_dir = Path(state_dir)
    repo_root = Path(repo_root)

    def payload(settings=None) -> dict:
        settings = settings or load_settings(state_dir)
        resource_root = resolve_storage_path(
            repo_root, settings["resources"]["snapshot_root"],
        )
        alert_root = resolve_storage_path(repo_root, settings["alerts"]["history_root"])
        return {
            "settings": settings,
            "paths": {
                "alerts": path_status(repo_root, settings["alerts"]["history_root"]),
                "resources": path_status(repo_root, settings["resources"]["snapshot_root"]),
            },
            "storage": {
                "alerts": _directory_summary(alert_root, suffixes=(".jsonl",)),
                "resources": storage_summary(resource_root),
            },
            "resource_notifications": {
                "enabled": bool(settings["resources"].get("risk_notifications_enabled")),
                **load_resource_risk_notification_state(state_dir),
            },
            "runtime": load_runtime(state_dir),
        }

    @routes.get("/api/acquisition")
    def get_acquisition(
        _context: RequestContext = Depends(request_context),
    ) -> dict:
        return payload()

    @routes.put("/api/acquisition/settings")
    def put_acquisition(
        body: AcquisitionSettingsBody,
        request: Request,
        _context: RequestContext = Depends(request_context),
    ) -> dict:
        try:
            candidate = body.model_dump()
            ensure_distinct_storage_roots(repo_root, candidate)
            settings = save_settings(state_dir, candidate)
            set_enabled(state_dir / "poll-enabled.json", settings["alerts"]["enabled"])
        except AcquisitionSettingsError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        scheduler = getattr(request.app.state, "scheduler", None)
        if scheduler is not None:
            scheduler.reconcile()
        return payload(settings)

    @routes.get("/api/resource-alert-settings")
    def get_resource_alert_settings(
        _context: RequestContext = Depends(request_context),
    ) -> dict:
        return resource_alert_settings(repo_root, state_dir)

    @routes.put("/api/resource-alert-settings")
    def put_resource_alert_settings(
        body: ResourceAlertSettingsBody,
        _context: RequestContext = Depends(request_context),
    ) -> dict:
        if body.schema_version != 1:
            raise HTTPException(status_code=422, detail="仅支持资源告警设置 schema_version=1")
        try:
            save_overrides(repo_root, state_dir, body.overrides)
        except ResourceThresholdError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return resource_alert_settings(repo_root, state_dir)

    @routes.get("/api/resources/latest")
    def get_latest_resources(
        platform: str | None = Query(
            default=None, pattern="^[a-z0-9][a-z0-9_-]{0,63}$",
        ),
        _context: RequestContext = Depends(request_context),
    ) -> dict:
        settings = load_settings(state_dir)
        root = resolve_storage_path(repo_root, settings["resources"]["snapshot_root"])
        excluded = _excluded_resource_platforms(repo_root)
        latest = {
            key: row for key, row in latest_snapshots(root).items()
            if key not in excluded
        }
        rows = [latest[platform]] if platform and platform in latest else (
            [] if platform else [latest[key] for key in sorted(latest)]
        )
        catalog = _read_json(state_dir / "resource-catalog.json")
        resolve = host_ref_resolver(repo_root)
        for row in rows:
            attach_host_refs(row.get("nodes") or [], resolve)
            attach_host_refs(row.get("services") or [], resolve)
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "items": rows,
            "catalog": catalog,
        }

    @routes.get("/api/resources/summary")
    def get_resource_summary(
        _context: RequestContext = Depends(request_context),
    ) -> dict:
        settings = load_settings(state_dir)
        root = resolve_storage_path(repo_root, settings["resources"]["snapshot_root"])
        excluded = _excluded_resource_platforms(repo_root)
        latest = {
            key: row for key, row in latest_snapshots(root).items()
            if key not in excluded
        }
        runtime = ((load_runtime(state_dir).get("jobs") or {}).get("resources") or {})
        risks = _filter_risks(
            _read_json(state_dir / "resource-risks.json"), excluded,
        )
        rows = [latest[key] for key in sorted(latest)]
        platform_rows = []
        for row in rows:
            coverage = row.get("coverage") or {}
            platform_rows.append({
                "platform": row.get("platform"),
                "status": row.get("status") or "error",
                "collected_at": row.get("collected_at"),
                "expected_nodes": int(coverage.get("expected_nodes", 0)),
                "observed_nodes": int(coverage.get("observed_nodes", 0)),
                "expected_services": int(coverage.get("expected_services", 0)),
                "observed_services": int(coverage.get("observed_services", 0)),
                "error_count": len(row.get("errors") or []),
            })
        findings = risks.get("findings") if isinstance(risks.get("findings"), list) else []
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "enabled": bool(settings["resources"]["enabled"]),
            "interval_seconds": int(settings["resources"]["interval_seconds"]),
            "runtime_state": _resource_runtime_state(settings, runtime),
            "last_collected_at": max(
                (str(row.get("collected_at")) for row in rows if row.get("collected_at")),
                default=None,
            ),
            "last_success_at": runtime.get("last_success_at"),
            "consecutive_failures": int(runtime.get("consecutive_failures", 0)),
            "last_error": runtime.get("last_error"),
            "platforms": platform_rows,
            "stats": {
                "expected_nodes": sum(item["expected_nodes"] for item in platform_rows),
                "observed_nodes": sum(item["observed_nodes"] for item in platform_rows),
                "expected_services": sum(item["expected_services"] for item in platform_rows),
                "observed_services": sum(item["observed_services"] for item in platform_rows),
                "local_mounts": sum(
                    1 for row in rows for node in (row.get("nodes") or [])
                    for fs in (node.get("filesystems") or []) if fs.get("kind") == "local"
                ),
                "risk_count": len(findings),
                "snapshot_count": int(risks.get("snapshot_count", 0) or 0),
            },
            "risk_severity": {
                level: sum(item.get("severity") == level for item in findings)
                for level in ("critical", "warning", "watch")
            },
        }

    @routes.get("/api/resources/history")
    def get_resource_history(
        platform: str = Query(pattern="^[a-z0-9][a-z0-9_-]{0,63}$"),
        node: str | None = Query(default=None, min_length=1, max_length=100),
        hours: int = Query(default=24, ge=1, le=24 * 90),
        _context: RequestContext = Depends(request_context),
    ) -> dict:
        settings = load_settings(state_dir)
        root = resolve_storage_path(repo_root, settings["resources"]["snapshot_root"])
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        points = []
        for row in iter_snapshots(root, since=since, platform=platform):
            nodes = row.get("nodes") or []
            if node:
                nodes = [item for item in nodes if item.get("node") == node]
            services = row.get("services") or []
            if node:
                services = [item for item in services if item.get("node") == node]
            business_metrics = row.get("business_metrics") or []
            if node:
                business_metrics = [
                    item for item in business_metrics if item.get("node") == node
                ]
            points.append({
                "collected_at": row.get("collected_at"),
                "status": row.get("status"),
                "nodes": nodes,
                "services": services,
                "business_metrics": business_metrics,
            })
        return {"platform": platform, "node": node, "hours": hours, "points": points}

    @routes.get("/api/resource-risks")
    def get_resource_risks(
        _context: RequestContext = Depends(request_context),
    ) -> dict:
        risks = _filter_risks(
            _read_json(state_dir / "resource-risks.json"),
            _excluded_resource_platforms(repo_root),
        )
        if risks:
            attach_host_refs(risks.get("findings") or [], host_ref_resolver(repo_root))
        return risks or {
            "schema_version": 1, "generated_at": None, "snapshot_count": 0,
            "forecast_status": [], "findings": [],
        }

    return routes


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 1, "updated_at": None, "services": [],
                "unmonitored_components": []}
    return value if isinstance(value, dict) else {}


def _excluded_resource_platforms(repo_root: Path) -> set[str]:
    try:
        config = yaml.safe_load(
            load_env_pack(Path(repo_root)).config_file("resource-metrics.yaml").read_text(
                encoding="utf-8",
            )
        ) or {}
    except (OSError, yaml.YAMLError):
        return set()
    return {
        str(value).strip().lower()
        for value in ((config.get("node") or {}).get("excluded_platforms") or [])
    }


def _filter_risks(payload: dict, excluded: set[str]) -> dict:
    if not isinstance(payload, dict) or not excluded:
        return payload
    result = dict(payload)
    result["findings"] = [
        row for row in (payload.get("findings") or [])
        if str(row.get("platform") or "").lower() not in excluded
    ]
    result["forecast_status"] = [
        row for row in (payload.get("forecast_status") or [])
        if str(row.get("platform") or "").lower() not in excluded
    ]
    return result


def _directory_summary(root: Path, *, suffixes: tuple[str, ...]) -> dict:
    files = []
    total = 0
    if root.exists():
        for path in sorted(root.iterdir()):
            if path.is_file() and path.name.endswith(suffixes):
                size = path.stat().st_size
                total += size
                files.append({"name": path.name, "size_bytes": size})
    return {
        "file_count": len(files), "total_bytes": total,
        "oldest": files[0]["name"] if files else None,
        "newest": files[-1]["name"] if files else None,
    }


def _resource_runtime_state(settings: dict, runtime: dict) -> str:
    if not settings["resources"]["enabled"]:
        return "stopped"
    if runtime.get("state") == "degraded" or int(runtime.get("consecutive_failures", 0)) > 0:
        return "stalled"
    last_success = _parse_datetime(runtime.get("last_success_at"))
    if last_success is None:
        return "starting" if runtime.get("last_started_at") else "stopped"
    stale_after = max(600, int(settings["resources"]["interval_seconds"]) * 2)
    if datetime.now(timezone.utc) - last_success > timedelta(seconds=stale_after):
        return "stalled"
    return "running"


def _parse_datetime(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
