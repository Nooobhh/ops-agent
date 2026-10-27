"""Alert-definition catalog and rule-level override endpoints."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from lib.alerts.rule_overrides import load_rule_overrides, set_rule_override
from lib.alerts.source_notify import (
    configured_source_ids,
    event_source_id,
    load_source_config,
    set_override,
    source_id as config_source_id,
    source_notify,
)
from lib.envpack import load_env_pack
from web.server.context import RequestContext, request_context
from web.server.data.state_reader import ALERT_CLASSES, active_payload, load_active
from web.server.data.timeline_reader import query_historical_summaries


_SEVERITY_OVERRIDE_FIELD = "severity" + "_override"


class SourceNotifyUpdate(BaseModel):
    notify: bool | None  # null = 恢复告警源配置里的默认值


class RuleOverrideUpdate(BaseModel):
    notify: bool | None = None
    severity: Literal["critical", "warning", "info"] | None = None


def router(*, state_dir: Path, repo_root: Path) -> APIRouter:
    routes = APIRouter()
    state_dir = Path(state_dir)
    repo_root = Path(repo_root)

    def sources_notify() -> dict:
        return source_notify(load_source_config(repo_root), state_dir)

    def source_ids() -> set:
        return set(configured_source_ids(load_source_config(repo_root)))

    def sources_meta() -> list:
        """告警规则页的来源 tab:按配置顺序,标签取配置 label,缺省「类型-集群短名」。"""
        pack = load_env_pack(repo_root)
        meta = []
        for name, source_cfg in (load_source_config(repo_root).get("sources") or {}).items():
            if not isinstance(source_cfg, dict):
                continue
            kind = str(source_cfg.get("type") or name)
            platform = source_cfg.get("platform")
            label = source_cfg.get("label") or (
                f"{kind.capitalize()}-{pack.short_name(str(platform))}" if platform else kind.capitalize()
            )
            meta.append({"id": config_source_id(name, source_cfg), "label": str(label)})
        return meta

    @routes.get("/api/alert-config")
    def get_alert_config(
        _context: RequestContext = Depends(request_context),
    ) -> dict:
        catalog = _load_catalog(state_dir / "alert-catalog.json")
        if catalog is None:
            return {"sources": {}, "catalog_available": False, "source_notify": sources_notify(),
                    "sources_meta": sources_meta()}
        overrides = load_rule_overrides(state_dir)
        runtime_classes = _load_runtime_classes(state_dir)
        sources = {}
        configured = source_ids()
        for source_id, raw_source in (catalog.get("sources") or {}).items():
            if source_id not in configured or not isinstance(raw_source, dict):
                continue
            alerts = []
            for raw_alert in raw_source.get("alerts") or []:
                if not isinstance(raw_alert, dict):
                    continue
                alert = dict(raw_alert)
                alertname = str(alert.get("alertname") or "")
                platform = str(alert.get("platform") or "")
                source_alert_key = (str(source_id), platform, alertname)
                runtime_class = runtime_classes.get(source_alert_key) \
                    or runtime_classes.get((str(source_id), "", alertname))
                if runtime_class and runtime_class != alert.get("class"):
                    # Keep the catalog/profile value available for diagnostics,
                    # while making the rule table agree with the alert list's
                    # latest runtime classification.
                    alert["baseline_class"] = alert.get("class") or "watch"
                    alert["class"] = runtime_class
                override = overrides.get(
                    f"{source_id}:{alert.get('alertname', '')}",
                )
                override = override if isinstance(override, dict) else {}
                alert["notify"] = override.get("notify", True)
                alert[_SEVERITY_OVERRIDE_FIELD] = override.get("severity")
                alerts.append(alert)
            sources[str(source_id)] = {
                "updated_at": raw_source.get("updated_at"),
                "alerts": alerts,
            }
        return {"sources": sources, "catalog_available": True, "source_notify": sources_notify(),
                "sources_meta": sources_meta()}

    @routes.put("/api/alert-sources/{source_id}/notify")
    def update_source_notify(
        source_id: str,
        body: SourceNotifyUpdate,
        _context: RequestContext = Depends(request_context),
    ) -> dict:
        """按来源开关中台推送;关了只是不推送,照常接收、记时间线与自动排查。"""
        if source_id not in sources_notify():
            raise HTTPException(status_code=404, detail="unknown source")
        set_override(state_dir, source_id, body.notify)
        return {"source_notify": sources_notify()}

    @routes.post("/api/alert-config/{source_id}/{alertname:path}")
    def update_alert_config(
        source_id: str,
        alertname: str,
        body: RuleOverrideUpdate,
        _context: RequestContext = Depends(request_context),
    ) -> dict:
        if source_id not in source_ids():
            raise HTTPException(status_code=422, detail="不支持的告警源")
        fields_set = body.model_fields_set
        if "notify" in fields_set and body.notify is None:
            raise HTTPException(status_code=422, detail="notify 必须是布尔值")
        key = f"{source_id}:{alertname}"
        try:
            entry = set_rule_override(
                state_dir,
                key,
                notify=body.notify if "notify" in fields_set else None,
                severity=body.severity if "severity" in fields_set else None,
                clear_severity="severity" in fields_set and body.severity is None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return dict(entry or {})

    return routes


def _load_runtime_classes(state_dir: Path) -> dict[tuple[str, str, str], str]:
    """Return the latest observed class for each configured alert rule.

    The catalog stores the static profile class (normally ``watch``), while
    the alert list renders the class assigned by the running alert state
    (for example ``flapping``).  Combining both here keeps the rule view in
    sync without mutating the catalog on every page load.
    """
    latest: dict[tuple[str, str, str], tuple[datetime, str]] = {}
    try:
        active = load_active(state_dir)
    except (OSError, ValueError, TypeError):
        active = {}
    for entry in active.values():
        payload = active_payload(entry) or {}
        _record_runtime_class(
            latest,
            source=payload.get("source"),
            platform=payload.get("platform"),
            alertname=payload.get("alertname"),
            alert_class=entry.get("class"),
            timestamp=entry.get("last_seen") or entry.get("first_seen"),
        )

    historical = query_historical_summaries(
        state_dir / "timeline.db",
        exclude_keys=set(active),
    )[0]
    for row in historical:
        _record_runtime_class(
            latest,
            source=row.get("source"),
            platform=row.get("platform"),
            alertname=row.get("alertname"),
            alert_class=row.get("alert_class"),
            timestamp=row.get("ts") or row.get("last_new"),
        )
    return {key: value[1] for key, value in latest.items()}


def _record_runtime_class(
    latest: dict[tuple[str, str, str], tuple[datetime, str]],
    *,
    source: object,
    platform: object,
    alertname: object,
    alert_class: object,
    timestamp: object,
) -> None:
    platform_value = str(platform or "")
    source_id = event_source_id(str(source or ""), platform_value)
    if source_id != str(source or ""):  # 按集群的源:集群已在源 id 里
        platform_value = ""
    alertname_value = str(alertname or "")
    class_value = str(alert_class or "")
    if not source or not alertname_value or class_value not in ALERT_CLASSES:
        return
    parsed = _parse_runtime_timestamp(timestamp)
    key = (source_id, platform_value, alertname_value)
    previous = latest.get(key)
    if previous is None or parsed >= previous[0]:
        latest[key] = (parsed, class_value)


def _parse_runtime_timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _load_catalog(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("sources"), dict):
        return None
    return data
