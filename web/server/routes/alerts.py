"""Alert list and detail endpoints."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from lib.alerts.dedup import DedupRules, apply, load_rules
from lib.data_root import resolve_data_root
from lib.envpack import load_env_pack
from lib.alerts.rule_overrides import load_rule_overrides
from lib.alerts.source_notify import event_source_id
from lib.alerts.timeline import occurrences as lifecycle_occurrences
from web.server.context import RequestContext, request_context
from web.server.data.chronic import chronic_keys
from web.server.data.dedup_live import compute_live
from web.server.data.resource_refs import attach_host_refs, host_ref_resolver
from web.server.data.routing_reader import lookup
from web.server.data.state_reader import (
    ALERT_CLASSES,
    active_payload,
    effective_class,
    load_active,
    load_source_fail,
)
from web.server.data.timeline_reader import (
    distinct_field_values,
    historical_missing_fields,
    latest_events_for_keys,
    latest_suppressions_for_keys,
    open_key_details,
    query_events,
    query_historical_summaries,
    range_bounds,
    suppressed_keys_today,
    suppression_peers,
)


_FACET_FIELDS = ("source", "platform", "severity", "service")
_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}
_ALERT_STATUSES = {"active", "resolved", "suppressed"}


def router(*, state_dir: Path, repo_root: Path,
           chronic_hours: float = 24.0) -> APIRouter:
    routes = APIRouter()
    state_dir = Path(state_dir)
    repo_root = Path(repo_root)

    @routes.get("/api/alerts")
    def alert_list(
        source: str | None = None,
        platform: str | None = None,
        host: str | None = None,
        severity: str | None = None,
        service: str | None = None,
        status: str | None = None,
        class_: str | None = Query(None, alias="class"),
        chronic: Literal["only", "exclude"] | None = None,
        from_: str | None = Query(None, alias="from"),
        to: str | None = None,
        first_seen_from: str | None = None,
        suppressed_today: bool | None = None,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        _context: RequestContext = Depends(request_context),
    ) -> dict:
        _validate_time_filter("from", from_)
        _validate_time_filter("to", to)
        _validate_time_filter("first_seen_from", first_seen_from, status_code=422)
        statuses = None
        if status is not None:
            statuses = [part.strip() for part in status.split(",")]
            if not statuses or any(value not in _ALERT_STATUSES for value in statuses):
                raise HTTPException(status_code=422, detail="status 值不正确")
        classes = []
        if class_ is not None:
            classes = [part.strip() for part in class_.split(",") if part.strip()]
            if not classes or any(value not in ALERT_CLASSES for value in classes):
                raise HTTPException(status_code=422, detail="class 值不正确")
        requested = {
            "source": source, "platform": platform, "host": host,
            "severity": severity, "service": service, "status": statuses,
            "chronic": chronic, "from": from_, "to": to,
            "classes": classes,
            "first_seen_from": first_seen_from,
            "suppressed_today": suppressed_today,
        }
        items, total, missing_historical_fields = build_alert_page(
            state_dir=state_dir, repo_root=repo_root,
            filters=requested, limit=limit, offset=offset,
            chronic_hours=chronic_hours,
        )
        notices = [
            name for name, value in (("severity", severity), ("service", service))
            if value and name in missing_historical_fields
        ]
        filter_notice = None
        if notices:
            filter_notice = (
                "上线前的历史事件未记录 " + "/".join(notices)
                + " 字段；这部分数据不会出现在结果中。"
            )
        attach_host_refs(items, host_ref_resolver(repo_root), node_field="host")
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "filter_notice": filter_notice,
        }

    @routes.get("/api/alerts/facets")
    def alert_facets(
        _context: RequestContext = Depends(request_context),
    ) -> dict:
        active = load_active(state_dir)
        since, until = range_bounds("30d", now=datetime.now(timezone.utc))
        values = distinct_field_values(
            state_dir / "timeline.db", fields=_FACET_FIELDS, since=since, until=until,
        )
        for entry in active.values():
            payload = active_payload(entry or {}) or {}
            active_values = {
                "source": payload.get("source"),
                "platform": payload.get("platform"),
                "severity": payload.get("severity") or entry.get("severity"),
                "service": payload.get("service"),
            }
            for field, value in active_values.items():
                cleaned = str(value).strip() if value is not None else ""
                if cleaned:
                    values[field].add(cleaned)
        return {
            field: sorted(
                values[field],
                key=(
                    lambda value: (_SEVERITY_ORDER.get(value, len(_SEVERITY_ORDER)), value)
                    if field == "severity" else value
                ),
            )
            for field in _FACET_FIELDS
        }

    @routes.get("/api/alerts/{key}")
    def alert_detail(
        key: str,
        _context: RequestContext = Depends(request_context),
    ) -> dict:
        return build_alert_detail(
            key=key, state_dir=state_dir, repo_root=repo_root,
        )

    return routes


def _muted_from_override(
    overrides: dict, source: str, platform: str, alertname: str,
    fallback: bool,
) -> bool:
    source_id = event_source_id(source, platform)
    override = overrides.get(f"{source_id}:{alertname}")
    override_notify = (
        override.get("notify") if isinstance(override, dict) else None
    )
    return (
        not override_notify
        if isinstance(override_notify, bool)
        else fallback
    )


def build_alert_page(*, state_dir: Path, repo_root: Path, filters: dict,
                     limit: int | None, offset: int,
                     chronic_hours: float = 24.0) -> tuple[list, int, set]:
    state_dir = Path(state_dir)
    repo_root = Path(repo_root)
    db_path = state_dir / "timeline.db"
    temporal = {"from": filters.get("from"), "to": filters.get("to")}
    active = load_active(state_dir)
    overrides = load_rule_overrides(state_dir)
    now = datetime.now(timezone.utc)
    chronic = chronic_keys(active, now=now, threshold_hours=chronic_hours)
    live = compute_live(
        load_env_pack(repo_root).config_file("alert-dedup.yaml"), active, load_source_fail(state_dir),
    )["mapping"]
    suppressed_today_keys = None
    if filters.get("suppressed_today") is True:
        suppressed_today_keys = (
            suppressed_keys_today(db_path, now=now) | set(live)
        )
    match_filters = {
        **filters,
        "_suppressed_today_keys": suppressed_today_keys,
    }
    active_latest = latest_events_for_keys(db_path, active, filters=temporal)
    persisted = latest_suppressions_for_keys(db_path, active)
    open_details = open_key_details(db_path) if active else {}
    active_items = []
    for key, entry in active.items():
        if any(temporal.values()) and key not in active_latest \
                and not _timestamp_in_range(
                    entry.get("last_seen"), temporal["from"], temporal["to"],
                ):
            continue
        row = active_latest.get(key) or {}
        payload = active_payload(entry or {}) or {}
        source, platform, alertname, host = _key_fields(key)
        muted = _muted_from_override(
            overrides,
            payload.get("source") or source,
            payload.get("platform") or platform,
            payload.get("alertname") or alertname,
            bool(entry.get("muted", False)),
        )
        item = {
            "key": key,
            "source": payload.get("source") or source,
            "platform": payload.get("platform") or platform,
            "alertname": payload.get("alertname") or alertname,
            "host": payload.get("host") or payload.get("host_raw") or host,
            "status": "suppressed" if key in live else "active",
            "chronic": key in chronic,
            "severity": payload.get("severity") or entry.get("severity"),
            "muted": muted,
            "service": payload.get("service"),
            "class": entry.get("class"),
            "effective_class": effective_class(entry.get("class"), muted),
            "first_seen": (
                (open_details.get(key) or {}).get("started_at")
                or entry.get("first_seen")
            ),
            "last_seen": entry.get("last_seen"),
            "occurrences": entry.get("occurrences"),
            "triage": entry.get("triage"),
            "suppressed_by": live.get(key) or persisted.get(key),
            "latest_event": row or None,
        }
        if _summary_matches(item, match_filters):
            active_items.append(item)

    history_filters = {
        name: filters.get(name)
        for name in ("source", "platform", "host", "severity", "service", "from", "to")
    }
    statuses = filters.get("status")
    include_history = (
        (statuses is None or "resolved" in statuses)
        and filters.get("chronic") != "only"
    )
    needs_post_filter = bool(
        filters.get("first_seen_from")
        or suppressed_today_keys is not None
        or filters.get("classes")
    )
    history_limit = (
        None if needs_post_filter
        else (offset + limit) if limit is not None
        else None
    )
    history_rows, history_total = query_historical_summaries(
        db_path, filters=history_filters, exclude_keys=set(active),
        limit=history_limit if include_history else 0,
    ) if include_history else ([], 0)
    historical_items = [
        item
        for row in history_rows
        for item in [_historical_summary(row, overrides)]
        if _summary_matches(item, match_filters)
    ]
    if needs_post_filter:
        history_total = len(historical_items)

    requested_history_fields = {
        name for name in ("severity", "service") if filters.get(name)
    }
    missing_fields = set()
    if include_history and requested_history_fields:
        notice_filters = {
            name: filters.get(name)
            for name in ("source", "platform", "host", "from", "to")
        }
        missing_fields = historical_missing_fields(
            db_path,
            filters=notice_filters,
            exclude_keys=set(active),
            fields=requested_history_fields,
        )

    combined = active_items + historical_items
    combined.sort(key=_first_seen_sort_key, reverse=True)
    total = len(active_items) + history_total
    end = offset + limit if limit is not None else None
    return combined[offset:end], total, missing_fields


def _summary_matches(item: dict, filters: dict) -> bool:
    for name in ("source", "platform", "host", "severity", "service"):
        value = filters.get(name)
        if value is not None and str(item.get(name) or "") != value:
            return False
    statuses = filters.get("status")
    if statuses is not None and item.get("status") not in statuses:
        return False
    if filters.get("chronic") == "only" and not item.get("chronic"):
        return False
    if filters.get("chronic") == "exclude" and item.get("chronic"):
        return False
    classes = filters.get("classes") or []
    if classes and item.get("effective_class") not in classes:
        return False
    first_seen_from = filters.get("first_seen_from")
    if first_seen_from and not _timestamp_in_range(
        item.get("first_seen"), first_seen_from, None,
    ):
        return False
    suppressed_today_keys = filters.get("_suppressed_today_keys")
    if suppressed_today_keys is not None and item.get("key") not in suppressed_today_keys:
        return False
    return True


def _historical_summary(row: dict, overrides: dict | None = None) -> dict:
    latest_event = dict(row)
    first_seen = latest_event.pop("last_new", None) or row.get("ts")
    occurrence_count = int(latest_event.pop("occurrence_count", 0) or 0)
    suppressed_by = latest_event.pop("historical_suppressed_by", None)
    source_field = row.get("source") or _key_fields(row["key"])[0]
    platform_field = row.get("platform") or _key_fields(row["key"])[1]
    alertname_field = row.get("alertname") or _key_fields(row["key"])[2]
    muted = _muted_from_override(
        overrides or {}, source_field, platform_field, alertname_field, False,
    )
    alert_class = row.get("alert_class")
    return {
        "key": row["key"],
        "source": source_field,
        "platform": platform_field,
        "alertname": alertname_field,
        "host": row.get("host") or _key_fields(row["key"])[3],
        "status": "resolved",
        "chronic": False,
        "severity": row.get("severity"),
        "muted": muted,
        "service": row.get("service"),
        "class": alert_class,
        "effective_class": effective_class(alert_class, muted),
        "first_seen": first_seen,
        "last_seen": row.get("ts"),
        "occurrences": occurrence_count,
        "triage": None,
        "suppressed_by": suppressed_by,
        "latest_event": latest_event,
    }


def _first_seen_sort_key(item: dict) -> tuple[bool, datetime]:
    value = item.get("first_seen")
    if not value:
        return False, datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = _as_datetime(value)
    except (TypeError, ValueError):
        return False, datetime.min.replace(tzinfo=timezone.utc)
    return True, parsed


def build_alert_detail(*, key: str, state_dir: Path, repo_root: Path) -> dict:
    state_dir = Path(state_dir)
    repo_root = Path(repo_root)
    db_path = state_dir / "timeline.db"
    events = query_events(db_path, key=key)
    active = load_active(state_dir)
    entry = active.get(key)
    if entry is None and not events:
        raise HTTPException(status_code=404, detail="alert not found")

    latest = events[0] if events else {}
    payload = active_payload(entry or {})
    source, platform, alertname, host = _key_fields(key)
    source = (payload or {}).get("source") or latest.get("source") or source
    platform = (payload or {}).get("platform") or latest.get("platform") or platform
    alertname = (payload or {}).get("alertname") or latest.get("alertname") or alertname
    host = ((payload or {}).get("host") or (payload or {}).get("host_raw")
            or latest.get("host") or host)

    intervals = lifecycle_occurrences(db_path, key)
    now = datetime.now(timezone.utc)
    rendered_intervals = []
    for interval in intervals:
        started = _as_datetime(interval["started_at"])
        resolved_at = interval.get("resolved_at")
        ended = _as_datetime(resolved_at) if resolved_at else now
        rendered_intervals.append({
            **interval,
            "duration_seconds": max(0, int((ended - started).total_seconds())),
            "open": resolved_at is None,
        })

    new_events_ascending = [
        event for event in reversed(events) if event.get("type") == "new"
    ]
    payload_source_at = _parse_optional((payload or {}).get("source_active_at"))
    # 区间与 new 事件一一对应(每个 new 开启一个区间);逐区间用源端时间修正起点,
    # 条件:早于轮询首见、且不穿透上一区间
    for index, interval in enumerate(rendered_intervals):
        source_at = _event_source_active_at(
            new_events_ascending[index]
            if index < len(new_events_ascending) else None
        )
        if interval["open"] and entry and payload_source_at:
            source_at = payload_source_at
        if source_at is None:
            continue
        started = _as_datetime(interval["started_at"])
        previous_end = _parse_optional(
            rendered_intervals[index - 1].get("resolved_at") if index else None
        )
        if source_at >= started \
                or (previous_end is not None and source_at <= previous_end):
            continue
        ended = _parse_optional(interval.get("resolved_at")) or now
        interval["started_at"] = source_at.isoformat()
        interval["duration_seconds"] = max(0, int((ended - source_at).total_seconds()))
        interval["started_from"] = "source"

    latest_new_source_at = next(
        (
            parsed
            for event in reversed(new_events_ascending)
            for parsed in [_event_source_active_at(event)]
            if parsed is not None
        ),
        None,
    )
    source_active_at = (
        payload_source_at if entry and payload_source_at else latest_new_source_at
    )

    if entry:
        source_id = event_source_id(source, platform)
        override = load_rule_overrides(state_dir).get(
            f"{source_id}:{alertname}",
        )
        override_notify = (
            override.get("notify") if isinstance(override, dict) else None
        )
        muted = (
            not override_notify
            if isinstance(override_notify, bool)
            else bool(entry.get("muted", False))
        )
        open_interval = rendered_intervals[-1] if (
            rendered_intervals and rendered_intervals[-1]["open"]
        ) else None
        current = {
            "status": "active",
            "severity": (payload or {}).get("severity") or entry.get("severity"),
            "service": (payload or {}).get("service"),
            "first_seen": (
                open_interval["started_at"] if open_interval
                else entry.get("first_seen")
            ),
            "last_seen": entry.get("last_seen"),
            "occurrences": len(rendered_intervals) or entry.get("occurrences"),
            "class": entry.get("class"),
            "effective_class": effective_class(entry.get("class"), muted),
            "triage": entry.get("triage"),
            "muted": muted,
        }
    else:
        source_id = event_source_id(source, platform)
        override = load_rule_overrides(state_dir).get(
            f"{source_id}:{alertname}",
        )
        override_notify = (
            override.get("notify") if isinstance(override, dict) else None
        )
        current = {
            "status": "resolved",
            "severity": latest.get("severity"),
            "service": latest.get("service"),
            "first_seen": rendered_intervals[-1]["started_at"] if rendered_intervals else latest.get("ts"),
            "last_seen": latest.get("ts"),
            "occurrences": len(rendered_intervals),
            "class": latest.get("alert_class"),
            "triage": None,
            "muted": (
                not override_notify
                if isinstance(override_notify, bool)
                else False
            ),
        }
        current["effective_class"] = effective_class(
            current["class"], current["muted"],
        )
    current["source_active_at"] = (
        source_active_at.isoformat() if source_active_at else None
    )

    rules_path = load_env_pack(repo_root).config_file("alert-dedup.yaml")
    live_result = compute_live(rules_path, active, load_source_fail(state_dir))
    mapping = live_result["mapping"]
    historical_links = suppression_peers(db_path, key)
    historical_winner = historical_links["suppressed_by"] or next(
        (row["suppressed_by"] for row in events if row.get("suppressed_by")), None,
    )
    live_suppresses = sorted(loser for loser, winner in mapping.items() if winner == key)
    live = {
        "suppressed_by": mapping.get(key),
        "suppresses": live_suppresses,
        "excluded_sources": live_result["excluded_sources"],
    }
    peer_relations = []
    if mapping.get(key):
        peer_relations.append((mapping[key], "suppressed_by"))
    peer_relations.extend((loser, "suppresses") for loser in live_suppresses)
    if historical_winner:
        peer_relations.append((historical_winner, "suppressed_by"))
    peer_relations.extend(
        (loser, "suppresses") for loser in historical_links["suppresses"]
    )

    peer_latest_by_key = latest_events_for_keys(
        db_path, (peer_key for peer_key, _relation in peer_relations),
    )
    peers = []
    seen = set()
    for peer_key, relation in peer_relations:
        identity = (peer_key, relation)
        if identity in seen or peer_key == key:
            continue
        seen.add(identity)
        peer_entry = active.get(peer_key)
        peer_payload = active_payload(peer_entry or {})
        peers.append({
            "key": peer_key,
            "source": (peer_payload or {}).get("source") or _key_fields(peer_key)[0],
            "relation": relation,
            "payload": peer_payload,
            "latest_event": peer_latest_by_key.get(peer_key),
        })

    return {
        "key": key,
        "source": source,
        "source_id": event_source_id(source, platform),
        "platform": platform,
        "alertname": alertname,
        "host": host,
        "host_ref": host_ref_resolver(repo_root)(platform, host),
        "description": _alert_description(payload, events),
        "current": current,
        "payload": {
            "available": payload is not None,
            "source_of": "state" if payload is not None else None,
            "data": payload,
        },
        "occurrences": rendered_intervals,
        "events": events,
        "routing": lookup(resolve_data_root(repo_root) / "build" / "routing.json", alertname, platform),
        "dedup": {
            "live": live,
            "historical": {"suppressed_by": historical_winner, "incomplete": True},
            "rule": _matching_rule(rules_path, active, key, mapping),
        },
        "peers": peers,
    }


def _matching_rule(rules_path: Path, active: dict, key: str, mapping: dict):
    related = mapping.get(key) or next(
        (loser for loser, winner in mapping.items() if winner == key), None,
    )
    if related is None:
        return None
    loaded = load_rules(rules_path)
    for rule in loaded.rules:
        single = apply(DedupRules(version=loaded.version, rules=(rule,)), active)
        if single.get(key) == mapping.get(key) or any(
            loser == related and winner == key for loser, winner in single.items()
        ):
            return {"winner": rule.get("winner"), "reason": rule.get("reason")}
    return None


def _key_fields(key: str):
    parts = str(key).split(":", 3)
    return tuple(parts) if len(parts) == 4 else ("", "", "", None)


def _parse_optional(value) -> datetime | None:
    if not value:
        return None
    try:
        return _as_datetime(value)
    except (TypeError, ValueError):
        return None


def _event_source_active_at(event: dict | None) -> datetime | None:
    detail = (event or {}).get("detail")
    if not isinstance(detail, dict):
        return None
    return _parse_optional(detail.get("source_active_at"))


def _alert_description(payload: dict | None, events: list) -> str | None:
    candidate = (payload or {}).get("detail")
    if isinstance(candidate, str) and candidate:
        return candidate
    for event in events:
        detail = event.get("detail")
        if event.get("type") == "new" and isinstance(detail, dict):
            description = detail.get("description")
            if isinstance(description, str) and description:
                return description
    return None


def _timestamp_in_range(value, from_, to) -> bool:
    if not value:
        return False
    parsed = _as_datetime(value)
    return (not from_ or parsed >= _as_datetime(from_)) and (not to or parsed <= _as_datetime(to))


def _validate_time_filter(
    name: str,
    value: str | None,
    *,
    status_code: int = 400,
) -> None:
    if not value:
        return
    try:
        _as_datetime(value)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status_code,
            detail=f"{name} 时间格式无效，请使用 ISO 8601",
        ) from None


def _as_datetime(value) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
