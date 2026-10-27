"""告警源轮询 tick 入口:拉源、分级、采证、通知、落事件流。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Tuple

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts._common import build_pool, emit_ok
from lib.data_root import local_config_path, resolve_data_root
from lib.alerts.channels import load_channels, migrate_wecom
from lib.alerts.model import AlertEvent
from lib.alerts.notify import (
    FeishuNotifier,
    MultiNotifier,
    WecomNotifier,
    render_chronic_transition_block,
    render_cluster_wide_block,
    render_alert_notification,
    render_host_added_block,
)
from lib.alerts.poll_control import load_status as load_poll_status
from lib.alerts.profiles import baseline_class, classify, load_profiles, match_profile
from lib.alerts.rule_overrides import load_rule_overrides
from lib.alerts.source_notify import GLOBAL_SOURCE_TYPES, event_source_id, source_id, source_notify
from lib.credentials import CredentialStore
from lib.alerts.sources import AlertSourceContext, PrometheusSource, SourceError, alert_source_factory
from lib.alerts.state import AlertsState, acquire_lock, release_lock
from lib.alerts import timeline
from lib.alerts.history import prune_event_logs
from lib.alerts.dedup import apply as apply_dedup
from lib.alerts.dedup import load_rules as load_dedup_rules
from lib.alerts.triage_runner import run_auto_triage
from lib.alerts.templates import load_notification_settings, render_template
from lib.atomic_file import atomic_write_json
from lib.config import load_platform
from lib.envpack import load_env_pack
from lib.network_env import configure_no_proxy
from lib.persistent_http import persistent_json_get
from lib.plugins import PluginService
from lib.triage import load_routing, match_alert
from lib.acquisition.settings import load_settings as load_acquisition_settings
from lib.acquisition.settings import resolve_storage_path


OVERLAP_MS = 60_000
# Prometheus 插件把到窗口末尾 2 个步长(2 分钟)内仍出现的时段标为未收口;重叠须盖住它,
# 下一 tick 才看得到收口。窗口起点在源内取整到整分钟,重叠部分的重复事件靠 event_id 去重。
PROMETHEUS_OVERLAP_MS = 180_000
INITIAL_REPLAY_MS = 300_000
# 源连续失败时 last_tick_ms 不推进,窗口会随时间无限膨胀。
# Prometheus query_range 上限 11000 点/序列,step 15s 即 45.8h 后恒返 400 无法自愈。
MAX_REPLAY_MS = 6 * 60 * 60 * 1000


def run(argv=None, *, now=None, records_alerts_root=None) -> int:
    repo_root = Path(__file__).resolve().parents[1]
    args = _parse_args(argv)
    now_dt = _ensure_now(now)
    data_root = resolve_data_root(repo_root)
    state_dir = Path(args.state_dir)
    absolute_state_dir = state_dir if state_dir.is_absolute() else data_root / state_dir
    routing_path = Path(args.routing)
    if not routing_path.is_absolute():
        routing_path = data_root / routing_path
    acquisition_settings = load_acquisition_settings(absolute_state_dir)
    alert_settings = acquisition_settings["alerts"]
    if records_alerts_root is None:
        records_alerts_root = resolve_storage_path(
            repo_root, alert_settings["history_root"],
        )
    else:
        records_alerts_root = Path(records_alerts_root)
    retention_days = int(alert_settings["retention_days"])
    poll_status = load_poll_status(absolute_state_dir / "poll-enabled.json")
    if not poll_status["effective"]:
        return emit_ok(
            **_empty_counts(),
            skipped_poll_disabled=True,
            reason=poll_status["reason"],
        )

    os.chdir(repo_root)

    pack = load_env_pack(repo_root)
    config = _load_yaml(Path(args.config) if args.config else pack.config_file("alert-sources.yaml"))
    config["_state_dir"] = absolute_state_dir
    # 按来源的推送开关:关了只是不推送,照常接收、记时间线、自动排查
    config["_source_notify"] = {
        key: info["notify"] for key, info in source_notify(config, absolute_state_dir).items()
    }
    # Keep the established alert format until an operator explicitly saves a
    # custom template. Resource findings have their own built-in Markdown
    # template and are handled by resource_collect.
    config["_notify_template"] = load_notification_settings(
        absolute_state_dir,
    ).get("template") or None
    profiles = load_profiles(args.profiles or pack.config_file("alert-profiles.yaml"))
    state_path = absolute_state_dir / "alerts.json"
    lock_path = absolute_state_dir / "alert_poll.lock"

    if args.dry_run:
        state = AlertsState.load(state_path)
        _initialize_upgrade_gate(state, now_dt)
        config["_tick_now"] = now_dt
        routing = load_routing(routing_path)
        events, source_errors, successful_sources = _normalize_collect_result(collect_events(config, state))
        config["_successful_sources"] = successful_sources
        config["_rule_overrides"] = load_rule_overrides(absolute_state_dir)
        counts, actions, _ = _process_tick(
            events, source_errors, successful_sources, state, profiles, routing, config, now_dt,
            notifier=None, dry_run=True, records_alerts_root=Path(records_alerts_root),
        )
        return emit_ok(**counts, dry_run=True, actions=actions)

    locked = acquire_lock(lock_path)
    if not locked:
        try:
            _append_event_log(Path(records_alerts_root), now_dt, [
                _log(now_dt, "skipped_locked", "", "", "skipped_locked", {},
                     severity=None, service=None)
            ])
        except Exception:
            pass
        return emit_ok(**_empty_counts(skipped_locked=True))

    try:
        state = AlertsState.load(state_path)
        _initialize_upgrade_gate(state, now_dt)
        config["_tick_now"] = now_dt
        state.retrigger_suppress_minutes = int((config.get("poll") or {}).get("retrigger_suppress_minutes", 30))
        routing = load_routing(routing_path)
        notifier = build_notifier(config, absolute_state_dir)
        events, source_errors, successful_sources = _normalize_collect_result(collect_events(config, state))
        config["_successful_sources"] = successful_sources
        config["_rule_overrides"] = load_rule_overrides(absolute_state_dir)
        counts, _actions, event_log = _process_tick(
            events, source_errors, successful_sources, state, profiles, routing, config, now_dt,
            notifier=notifier, dry_run=False, records_alerts_root=Path(records_alerts_root),
        )
        _append_event_log(Path(records_alerts_root), now_dt, event_log)
        try:
            timeline_path = absolute_state_dir / "timeline.db"
            timeline.project(timeline_path, event_log)
            timeline.prune(timeline_path, now_dt - timedelta(days=retention_days))
            prune_event_logs(
                Path(records_alerts_root), now_dt - timedelta(days=retention_days),
                preserve_keys=set(state.active),
            )
        except Exception as exc:
            print(f"warning: timeline projection failed: {exc}", file=sys.stderr)
        try:
            _update_alert_catalog(config, profiles, absolute_state_dir, now_dt)
        except Exception as exc:
            print(f"warning: alert catalog update failed: {exc}", file=sys.stderr)
        state.save()
        return emit_ok(**counts)
    finally:
        release_lock(lock_path)


def collect_events(config: dict, state: AlertsState):
    events: List[AlertEvent] = []
    errors: List[SourceError] = []
    successful_sources = set()
    sources = config.get("sources") or {}
    runtime_sources = {}
    config["_runtime_sources"] = runtime_sources
    replay_clamped: List[str] = []
    config["_replay_clamped"] = replay_clamped
    platforms = _load_platforms()
    configure_no_proxy(_internal_endpoint_urls(config, platforms))
    nodes_by_platform = {
        name: {node: {"host": info.host} for node, info in plat.nodes.items()}
        for name, plat in platforms.items()
    }
    now_dt = _ensure_now(config.get("_tick_now"))
    query_end_ms = int(now_dt.timestamp() * 1000)
    for name, source_cfg in sources.items():
        if source_cfg.get("enabled") is False:
            continue
        source_id = _source_id(name, source_cfg)
        prior_end_ms = state.last_tick_ms.get(source_id)
        if prior_end_ms is None:
            prior_end_ms = query_end_ms - INITIAL_REPLAY_MS
        overlap_ms = PROMETHEUS_OVERLAP_MS if source_cfg.get("type") == "prometheus" else OVERLAP_MS
        query_start_ms = int(prior_end_ms) - overlap_ms
        replay_floor_ms = query_end_ms - MAX_REPLAY_MS
        if query_start_ms < replay_floor_ms:
            replay_clamped.append(source_id)
            query_start_ms = replay_floor_ms
        try:
            if source_cfg.get("type") == "prometheus":
                feed, label_clusters = _plugin_feed(config, source_cfg["plugin"], "alerts")
                source = PrometheusSource(
                    feed,
                    label_clusters,
                    nodes_by_platform,
                    query_start_ms=query_start_ms,
                    query_end_ms=query_end_ms,
                )
                runtime_sources[source_id] = source
                events.extend(source.fetch())
                successful_sources.add(source_id)
                state.last_tick_ms[source_id] = query_end_ms
            else:
                # 其余类型由环境包 alert_sources 扩展提供,按集群各一个
                platform = str(source_cfg.get("platform") or "")
                if platform not in platforms:
                    raise SourceError(source_id, f"扩展告警源须指定已登记的 managed 集群 platform: {platform!r}")
                factory = alert_source_factory(load_env_pack(Path(".")), str(source_cfg.get("type") or ""))
                source = factory(AlertSourceContext(
                    name=name,
                    source_id=source_id,
                    config=source_cfg,
                    platform=platform,
                    platform_config=platforms[platform],
                    nodes_by_platform=nodes_by_platform,
                    query_start_ms=query_start_ms,
                    query_end_ms=query_end_ms,
                    credentials=_credential_store(),
                    http_get=persistent_json_get,
                ))
                runtime_sources[source_id] = source
                events.extend(source.fetch())
                successful_sources.add(source_id)
                state.last_tick_ms[source_id] = query_end_ms
        except SourceError as exc:
            errors.append(exc)
        except Exception as exc:
            errors.append(SourceError(source_id, str(exc)))
    return events, errors, successful_sources


def _plugin_feed(config: dict, plugin_id: str, kind: str):
    """告警源经插件取数:返回 (feed(role, arguments), 标签值 → 集群旧短键)。地址取插件页,不看插件开关。"""
    repo_root = Path(__file__).resolve().parents[1]
    state_dir = config.get("_state_dir") or resolve_data_root(repo_root) / "state"
    service = PluginService(repo_root, Path(state_dir))
    plugin = service.registry.get(plugin_id)
    if kind not in plugin.feeds:
        raise ValueError(f"插件 {plugin.title} 未声明 feeds.{kind}")
    label_clusters = {value: key for key, value in plugin.scope.clusters.items()} if plugin.scope else {}
    return (lambda role, arguments: service.feed_call(kind, role, arguments, plugin=plugin)), label_clusters


def _internal_endpoint_urls(config: dict, platforms: dict) -> List[str]:
    urls = [
        source_cfg.get("url")
        for source_cfg in (config.get("sources") or {}).values()
        if source_cfg.get("url")
    ]
    for platform in platforms.values():
        endpoints = getattr(platform, "endpoints", {}) or {}
        for endpoint in endpoints.values():
            if isinstance(endpoint, dict) and endpoint.get("url"):
                urls.append(endpoint["url"])
    return urls


def _update_alert_catalog(config: dict, profiles, state_dir: Path, now_dt: datetime) -> None:
    path = Path(state_dir) / "alert-catalog.json"
    catalog = _load_alert_catalog(path)
    sources_out = dict(catalog.get("sources") or {})
    runtime_sources = config.get("_runtime_sources") or {}
    successful_sources = set(config.get("_successful_sources") or set())
    now_iso = now_dt.isoformat()

    configured_ids = {
        _source_id(name, source_cfg): source_cfg
        for name, source_cfg in (config.get("sources") or {}).items()
    }
    # Catalogs survive restarts and upgrades. Drop definitions for channels
    # removed from configuration so retired sources do not linger in settings.
    sources_out = {
        source_id: source
        for source_id, source in sources_out.items()
        if source_id in configured_ids
    }
    for source_id in configured_ids:
        previous = sources_out.get(source_id)
        if not isinstance(previous, dict):
            sources_out[source_id] = {"updated_at": None, "alerts": []}

    prometheus = runtime_sources.get("prometheus")
    if "prometheus" in successful_sources and prometheus is not None:
        alerts = [
            _catalog_alert(item, item.get("platform", "unknown"), profiles)
            for item in prometheus.last_definitions
        ]
        sources_out["prometheus"] = {"updated_at": now_iso, "alerts": alerts}

    # 扩展源的规则定义 60 分钟 TTL 刷新(源提供 fetch_definitions 才有)
    for source_id in configured_ids:
        if source_id in GLOBAL_SOURCE_TYPES:
            continue
        previous = sources_out[source_id]
        if not _catalog_source_expired(previous, now_dt):
            continue
        source = runtime_sources.get(source_id)
        if source is None or not hasattr(source, "fetch_definitions"):
            continue
        try:
            platform = source_id.split(":", 1)[1]
            alerts = [
                _catalog_alert({**item, "platform": platform}, platform, profiles)
                for item in source.fetch_definitions()
            ]
            sources_out[source_id] = {"updated_at": now_iso, "alerts": alerts}
        except Exception as exc:
            print(f"warning: {source_id} catalog refresh failed: {exc}", file=sys.stderr)

    atomic_write_json(path, {
        "version": 1,
        "updated_at": now_iso,
        "sources": sources_out,
    })


def _load_alert_catalog(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _catalog_source_expired(source: dict, now_dt: datetime) -> bool:
    updated_at = source.get("updated_at") if isinstance(source, dict) else None
    if not updated_at:
        return True
    try:
        return now_dt - _parse_iso(updated_at) > timedelta(minutes=60)
    except (TypeError, ValueError):
        return True


def _catalog_alert(item: dict, platform: str, profiles) -> dict:
    alert = dict(item)
    alert["class"] = baseline_class(AlertEvent(
        source="catalog",
        platform=str(platform),
        alertname=str(alert.get("alertname") or ""),
        severity=str(alert.get("severity") or "unknown"),
        host=None,
        host_raw="",
        service=str(alert.get("service") or ""),
        state=str(alert.get("state") or "firing"),
        detail="",
    ), profiles)
    return alert


def build_notifier(
    config: dict,
    state_dir: Path,
    *,
    repo_root: Path | None = None,
) -> MultiNotifier:
    state_dir = Path(state_dir)
    channels = load_channels(state_dir)
    if "wecom" not in channels:
        credentials = (
            _load_credentials()
            if repo_root is None else _load_credentials(repo_root)
        )
        legacy_key = (credentials.get("wecom:ops-alerts") or {}).get("webhook_key", "")
        if legacy_key:
            migrate_wecom(state_dir, legacy_key)
            channels = load_channels(state_dir)

    notifiers = []
    wecom = channels.get("wecom")
    if isinstance(wecom, dict) and wecom.get("enabled") and wecom.get("webhook_key"):
        notifiers.append(WecomNotifier(wecom["webhook_key"], state_dir / "wecom-queue.jsonl"))
    feishu = channels.get("feishu")
    if isinstance(feishu, dict) and feishu.get("enabled") and feishu.get("webhook"):
        notifiers.append(FeishuNotifier(
            feishu["webhook"], feishu.get("secret", ""), state_dir / "feishu-queue.jsonl",
        ))
    return MultiNotifier(notifiers)


def build_pool_lazy(platform: str):
    return build_pool(
        str(load_env_pack(Path(".")).source_path(platform, "hosts")),
        str(local_config_path(Path(__file__).resolve().parents[1], "local-dev.yaml")),
    )


def main() -> int:
    return run()


def _process_tick(events: List[AlertEvent], source_errors: List[SourceError],
                  successful_sources, state: AlertsState, profiles, routing: dict, config: dict, now_dt: datetime,
                  *, notifier, dry_run: bool, records_alerts_root: Path):
    _initialize_upgrade_gate(state, now_dt)
    counts = _empty_counts()
    actions: List[dict] = []
    event_log: List[dict] = []
    counts["source_errors"] = len(source_errors)
    counts["replay_clamped"] = list(config.get("_replay_clamped") or [])
    counts["source_status"] = _source_status(config, successful_sources, source_errors)
    counts["ok"] = not counts["source_status"] or any(
        status == "ok" for status in counts["source_status"].values()
    )
    overrides = config.get("_rule_overrides") or {}
    # 关了推送的来源:下面各类通知(含源异常)都跳过;时间线、分级、去重与自动排查照常
    muted_sources = {key for key, enabled in (config.get("_source_notify") or {}).items() if not enabled}
    for event in events:
        override = overrides.get(_rule_override_key(event))
        if isinstance(override, dict) and override.get("severity"):
            event.severity = str(override["severity"])
    replay_events = sorted(events, key=_event_source_sort_key)
    flap_max_seconds = int(
        ((profiles.class_config.get("flapping") or {}).get(
            "flap_max_seconds", 0,
        )) or 0
    )
    short_flapping_segments = _short_flapping_segments(
        replay_events, profiles, flap_max_seconds,
    )
    previous_active = json.loads(json.dumps(state.active, ensure_ascii=False))
    override_muted_keys = set()

    for source in successful_sources:
        state.source_fail.pop(source, None)
    for err in source_errors:
        state.source_fail[err.source] = int(state.source_fail.get(err.source, 0)) + 1
        event_log.append(_log(now_dt, "source_error", err.source, "", "source_error", str(err),
                              severity=None, service=None))

    suppressed_by = {}
    if not dry_run:
        try:
            dedup_window = int(
                (config.get("poll") or {}).get("dedup_window_seconds", 300)
            )
            suppressed_by = apply_dedup(
                load_dedup_rules(load_env_pack(Path(".")).config_file("alert-dedup.yaml")),
                replay_events,
                window_seconds=dedup_window,
            )
        except Exception as exc:
            print(f"warning: deduplication failed: {exc}", file=sys.stderr)

    final_by_key: Dict[str, AlertEvent] = {}
    for event in replay_events:
        final_by_key[event.key] = event
    active_events = [
        event for event in final_by_key.values()
        if str(event.state).lower() != "ok"
    ]
    failed_sources = {err.source for err in source_errors}
    delta = state.transition(active_events, now_dt, failed_sources=failed_sources)
    resurfaced_by_key = {item.key: item.resurfaced for item in delta.new}
    for entry in state.active.values():
        override = overrides.get(_entry_rule_override_key(entry))
        if isinstance(override, dict) and override.get("notify") is False:
            entry["muted"] = True
            entry["_muted_by_override"] = True
            override_muted_keys.add(_event_from_state(entry).key)
        elif (
            isinstance(override, dict) and override.get("notify") is True
        ) or entry.get("_muted_by_override"):
            entry.pop("muted", None)
            entry.pop("_muted_by_override", None)
        else:
            # A direct state-level mute is distinct from a rule override.
            # Preserve it so the auto-triage exclusion has stable semantics.
            pass
    seen_event_ids = _load_seen_event_ids(records_alerts_root, now_dt)
    logical_active = set(previous_active)
    explicit_ok_keys = set()
    transient_new: List[AlertEvent] = []
    new_count = 0
    resolved_count = 0
    for event in replay_events:
        is_ok = str(event.state).lower() == "ok"
        skip_by_gate = _should_skip_by_upgrade_gate(
            event, state.upgraded_at, previous_active,
        )
        if is_ok:
            explicit_ok_keys.add(event.key)
            if not skip_by_gate:
                replay_class = (
                    "flapping"
                    if _event_segment_key(event) in short_flapping_segments
                    else (previous_active.get(event.key) or {}).get("class", "")
                )
                row = _log(
                    now_dt,
                    "resolved",
                    event.key,
                    replay_class,
                    "resolved",
                    {},
                    severity=event.severity,
                    service=event.service,
                    source_ts=event.source_resolved_at,
                )
                winner = suppressed_by.get(event.key)
                if winner:
                    row["suppressed_by"] = winner
                if _append_unseen_event(event_log, row, seen_event_ids):
                    resolved_count += 1
            logical_active.discard(event.key)
            continue

        if event.key not in logical_active:
            if not skip_by_gate:
                resurfaced = bool(resurfaced_by_key.get(event.key, False))
                replay_class = (
                    "flapping"
                    if (
                        _event_segment_key(event) in short_flapping_segments
                        or _active_event_is_flapping(
                            event, profiles, flap_max_seconds, now_dt,
                        )
                    )
                    else ""
                )
                row = _log(
                    now_dt,
                    "new",
                    event.key,
                    replay_class,
                    "active",
                    {
                        "resurfaced": resurfaced,
                        "description": event.detail,
                        "source_active_at": event.source_active_at,
                    },
                    severity=event.severity,
                    service=event.service,
                    source_ts=event.source_active_at,
                )
                winner = suppressed_by.get(event.key)
                if winner:
                    row["suppressed_by"] = winner
                if _append_unseen_event(event_log, row, seen_event_ids):
                    new_count += 1
                    if event.key not in state.active:
                        transient_new.append(event)
            logical_active.add(event.key)

    for item in delta.resolved:
        if item.key in explicit_ok_keys:
            continue
        row = _log(
            now_dt,
            "resolved",
            item.key,
            item.entry.get("class", ""),
            "resolved",
            {},
            severity=item.event.severity,
            service=item.event.service,
        )
        winner = suppressed_by.get(item.key)
        if winner:
            row["suppressed_by"] = winner
        if _append_unseen_event(event_log, row, seen_event_ids):
            resolved_count += 1
    counts["new"] = new_count
    counts["resolved"] = resolved_count

    events = active_events
    host_counts = _active_host_counts(state.active, events)
    ready_watch = set()
    pending_chronic_transitions: set[str] = set()
    pending_host_additions: Dict[str, list[str]] = {}
    pending_cluster_wide: Dict[str, Dict[str, object]] = {}
    initialized_chronic_groups: set[str] = set()
    min_duration = int(((profiles.class_config.get("watch") or {}).get("min_duration_seconds", 0)) or 0)

    grouped_events: Dict[str, List[AlertEvent]] = {}
    for event in events:
        grouped_events.setdefault(_group_key(event), []).append(event)

    # v0.5 persisted per-host chronic classifications without group state. Treat
    # those as already-notified/manual when first observed by the new state model.
    for group_key, group_events in grouped_events.items():
        if group_key in state.chronic_groups:
            continue
        legacy = any(
            (previous_active.get(event.key) or {}).get("class") == "chronic"
            for event in group_events
        )
        manual = any(
            baseline_class(event, profiles) == "chronic"
            and classify(
                event,
                active_host_count=host_counts.get(
                    (event.platform, event.alertname), 0
                ),
                prior_severity=(previous_active.get(event.key) or {}).get("severity"),
                profiles=profiles,
            )[0] == "chronic"
            for event in group_events
        )
        if legacy or manual:
            state.chronic_groups[group_key] = _new_chronic_group(
                now_dt, group_events, notified_chronic_at="manual",
            )
            initialized_chronic_groups.add(group_key)

    # Duration is a group transition: one old-enough watch promotes the group
    # once, and every watch member receives the same persisted class this tick.
    for group_key, group_events in grouped_events.items():
        if group_key in state.chronic_groups:
            continue
        representative = None
        for event in group_events:
            prior_entry = previous_active.get(event.key) or {}
            cls, reason = classify(
                event,
                active_host_count=host_counts.get(
                    (event.platform, event.alertname), 0
                ),
                prior_severity=prior_entry.get("severity"),
                profiles=profiles,
            )
            first_seen = _parse_iso(state.active[event.key]["first_seen"])
            if (
                cls == "watch"
                and not reason.startswith("escalated:")
                and prior_entry.get("class") != "chronic"
                and (now_dt - first_seen).total_seconds() >= 24 * 60 * 60
            ):
                representative = event
                break
        if representative is None:
            continue
        now_iso = now_dt.isoformat()
        state.chronic_groups[group_key] = _new_chronic_group(
            now_dt, group_events, notified_chronic_at=now_iso,
        )
        pending_chronic_transitions.add(group_key)
        initialized_chronic_groups.add(group_key)
        event_log.append(_log(
            now_dt, "class_upgraded", representative.key, "chronic",
            "class_upgraded",
            {
                "from": "watch",
                "to": "chronic",
                "reason": "auto_chronic:duration",
                "group_key": group_key,
            },
            severity=representative.severity, service=representative.service,
        ))

    for event in events:
        entry = state.active[event.key]
        prior = (previous_active.get(event.key) or {}).get("severity")
        if event.maintenance:
            cls, reason = "ignore", "maint"
        else:
            cls, reason = classify(
                event,
                active_host_count=host_counts.get((event.platform, event.alertname), 0),
                prior_severity=prior,
                profiles=profiles,
            )
        previous_class = (previous_active.get(event.key) or {}).get("class")
        if (
            cls == "watch"
            and _dynamic_flap_candidate(event, profiles)
            and flap_max_seconds > 0
        ):
            age = _event_age_seconds(event, entry, now_dt)
            if age <= flap_max_seconds:
                cls, reason = "flapping", "auto_flapping:duration"
            else:
                cls, reason = "watch", "auto_watch:sustained"
        flap_escalated = (
            previous_class == "flapping"
            and cls == "watch"
            and reason == "auto_watch:sustained"
        )
        escalated = reason.startswith("escalated:")
        if flap_escalated:
            triage = entry.setdefault("triage", {})
            if triage.get("status") == "not_applicable":
                triage["status"] = "pending"
            triage.pop("defer_reason", None)
            event_log.append(_log(
                now_dt,
                "escalated",
                event.key,
                "watch",
                "escalated",
                {
                    "from": "flapping",
                    "to": "watch",
                    "reason": reason,
                },
                severity=event.severity,
                service=event.service,
            ))
        elif escalated and previous_class in {"chronic", None}:
            triage = entry.setdefault("triage", {})
            if previous_class == "chronic" and triage.get("status") in {"not_applicable", "failed"}:
                entry["triage"] = {
                    "status": "pending",
                    "attempts": 0,
                    "last_attempt_at": None,
                    "run_id": None,
                }
            if previous_class == "chronic":
                entry["notified_initial_at"] = None
                entry["notified_diagnosis_at"] = None
            event_log.append(_log(
                now_dt, "escalated", event.key, "watch", "escalated", reason,
                severity=event.severity, service=event.service,
            ))
        elif (
            not escalated
            and cls == "watch"
            and _group_key(event) in state.chronic_groups
        ):
            cls = "chronic"
            reason = (
                "auto_chronic:duration"
                if _group_key(event) in pending_chronic_transitions
                else "sustained_chronic"
            )
        entry["class"] = cls
        entry["class_reason"] = reason
        if cls == "flapping":
            entry["triage"]["status"] = "not_applicable"
            entry["triage"]["defer_reason"] = "flapping"
        elif cls == "watch":
            age = _event_age_seconds(event, entry, now_dt)
            if age < min_duration:
                counts["deferred"] += 1
                entry["triage"]["status"] = "not_applicable"
                entry["triage"]["defer_reason"] = "min_duration"
            else:
                pass
        else:
            entry["triage"]["status"] = "not_applicable"
        if dry_run and _is_supported_event(event):
            actions.append({
                "key": event.key,
                "class": cls,
                "reason": reason,
                "decision": _dry_decision(event, cls, event.key in ready_watch, routing),
            })

    for key, entry in state.active.items():
        if not _is_supported_entry(entry):
            continue
        if entry.get("class") != "watch":
            continue
        event = _event_from_state(entry)
        age = _event_age_seconds(event, entry, now_dt)
        if age < min_duration:
            continue
        triage = entry.setdefault("triage", {})
        if triage.get("status") == "not_applicable" \
                and triage.get("defer_reason") in {
                    "min_duration", "flapping", "muted", "override_muted",
                }:
            triage["status"] = "pending"
        triage.pop("defer_reason", None)
        ready_watch.add(key)

    if dry_run:
        for action in actions:
            action.update(_dry_action_fields(action["key"], state.active[action["key"]],
                                             action["key"] in ready_watch, routing, muted_sources))
        for key, entry in sorted(state.active.items(), key=lambda item: item[1].get("first_seen", "")):
            if any(action.get("key") == key for action in actions):
                continue
            if not _is_supported_entry(entry):
                continue
            if entry.get("class") != "watch" or key not in ready_watch:
                continue
            if entry.get("notified_initial_at") and (entry.get("triage") or {}).get("status") in {"done", "not_applicable", "failed"}:
                continue
            actions.append({
                "key": key,
                "class": entry.get("class"),
                "reason": entry.get("class_reason"),
                **_dry_action_fields(key, entry, True, routing, muted_sources),
            })

    for row in event_log:
        winner = suppressed_by.get(row.get("key"))
        if winner:
            row["suppressed_by"] = winner

    group_active = _active_chronic_group_entries(state)
    for group_key, group in list(state.chronic_groups.items()):
        active_items = group_active.get(group_key, [])
        if not active_items:
            group["cluster_wide_triggered"] = False
            continue
        current_hosts = sorted({
            event.host or event.host_raw
            for event, _entry in active_items
            if event.host or event.host_raw
        })
        history = group.setdefault("chronic_host_history", [])
        if group_key not in initialized_chronic_groups:
            known = set(history)
            new_hosts = [host for host in current_hosts if host not in known]
            if new_hosts:
                history.extend(new_hosts)
                pending_host_additions[group_key] = new_hosts

        parts = group_key.split(":", 2)
        if len(parts) != 3:
            continue
        representative, _entry = active_items[0]
        profile = match_profile(representative, profiles) or {}
        denominator = int(profile.get("baseline_host_count", 0) or 0)
        if denominator <= 0:
            denominator = _platform_denominator(
                representative.platform, config,
            )
        if denominator <= 0:
            group["cluster_wide_triggered"] = False
            continue
        threshold = denominator * 70 // 100
        active_count = len(current_hosts)
        was_triggered = bool(group.get("cluster_wide_triggered"))
        is_triggered = active_count >= threshold
        group["cluster_wide_triggered"] = is_triggered
        if (
            is_triggered
            and not was_triggered
            and group_key not in initialized_chronic_groups
        ):
            pending_cluster_wide[group_key] = {
                "active_count": active_count,
                "denominator": denominator,
                "threshold": threshold,
            }

    for key in ready_watch:
        entry = state.active.get(key)
        if not entry or not entry.get("muted"):
            continue
        triage = entry.setdefault("triage", {})
        triage["status"] = "not_applicable"
        triage["defer_reason"] = (
            "override_muted" if key in override_muted_keys else "muted"
        )

    triage_outcomes = {}
    auto_triage_enabled = bool((config.get("poll") or {}).get("auto_triage_enabled", False))
    if not dry_run and auto_triage_enabled:
        pools = {}
        for key in state.select_triage_batch(int((config.get("poll") or {}).get("triage_concurrency", 3))):
            if key not in ready_watch:
                continue
            event = _event_from_state(state.active[key])
            if event.platform not in pools:
                pools[event.platform] = build_pool_lazy(event.platform)
            pool = pools[event.platform]
            outcome = run_auto_triage(event, routing, pool)
            triage_outcomes[key] = outcome
            ok = outcome.status == "done"
            state.mark_triage_attempt(key, ok=ok, run_id=outcome.run_id, err=outcome.error)
            if ok:
                counts["triaged"] += 1
                logged_event = _event_from_state(state.active[key])
                event_log.append(_log(
                    now_dt, "triaged", key, state.active[key].get("class", ""),
                    "triaged", outcome.run_id, severity=logged_event.severity,
                    service=logged_event.service,
                ))
            elif outcome.status == "not_applicable":
                state.active[key]["triage"]["status"] = "not_applicable"
            else:
                counts["triage_failed"] += 1
                logged_event = _event_from_state(state.active[key])
                event_log.append(_log(
                    now_dt, "triage_failed", key, state.active[key].get("class", ""),
                    "triage_failed", outcome.error, severity=logged_event.severity,
                    service=logged_event.service,
                ))
    blocks = []
    delivery_ready = _notifier_has_delivery(notifier)
    transient_notified = set()
    for event in transient_new:
        if event.key in transient_notified or event.key in suppressed_by \
                or _event_source_id(event) in muted_sources:
            continue
        if _event_segment_key(event) in short_flapping_segments:
            continue
        override = overrides.get(_rule_override_key(event))
        if event.maintenance or (
            isinstance(override, dict) and override.get("notify") is False
        ):
            continue
        cls, reason = classify(
            event,
            active_host_count=host_counts.get((event.platform, event.alertname), 1),
            prior_severity=(previous_active.get(event.key) or {}).get("severity"),
            profiles=profiles,
        )
        if cls != "watch" or min_duration > 0:
            continue
        blocks.append(render_alert_notification(
            event,
            {
                "first_seen": event.source_active_at or now_dt.isoformat(),
                "class": cls,
                "class_reason": reason,
            },
            None,
            template=config.get("_notify_template"),
        ))
        transient_notified.add(event.key)
    for group_key in pending_chronic_transitions:
        active_items = group_active.get(group_key, [])
        if not active_items or _group_muted_or_suppressed(active_items, suppressed_by) \
                or _event_source_id(active_items[0][0]) in muted_sources:
            continue
        event, entry = active_items[0]
        blocks.append(_render_chronic_transition(
            event, entry, state.chronic_groups[group_key], config,
        ))
    for group_key, new_hosts in pending_host_additions.items():
        active_items = group_active.get(group_key, [])
        if not active_items or _group_muted_or_suppressed(active_items, suppressed_by) \
                or _event_source_id(active_items[0][0]) in muted_sources:
            continue
        event, entry = active_items[0]
        blocks.append(_render_host_added(
            event, entry, new_hosts, len(active_items), config,
        ))
    for group_key, cluster_info in pending_cluster_wide.items():
        active_items = group_active.get(group_key, [])
        if not active_items or _group_muted_or_suppressed(active_items, suppressed_by) \
                or _event_source_id(active_items[0][0]) in muted_sources:
            continue
        event, entry = active_items[0]
        blocks.append(_render_cluster_wide(
            event, entry, cluster_info, config,
        ))
        if delivery_ready:
            state.chronic_groups[group_key]["notified_cluster_wide_at"] = now_dt.isoformat()
    initial_notified_keys = set()
    for key, entry in sorted(state.active.items(), key=lambda item: item[1].get("first_seen", "")):
        if not _is_supported_entry(entry):
            continue
        if entry.get("class") != "watch":
            continue
        if key in suppressed_by:
            continue
        if entry.get("muted") or _event_source_id(_event_from_state(entry)) in muted_sources:
            continue
        if key not in ready_watch or entry.get("notified_initial_at"):
            continue
        blocks.append(render_alert_notification(
            _event_from_state(entry), entry, triage_outcomes.get(key),
            template=config.get("_notify_template"),
        ))
        if delivery_ready:
            entry["notified_initial_at"] = now_dt.isoformat()
        initial_notified_keys.add(key)
    for key, outcome in triage_outcomes.items():
        if key in suppressed_by:
            continue
        entry = state.active.get(key)
        if not entry or not _is_supported_entry(entry) \
                or outcome.status != "done" or entry.get("notified_diagnosis_at"):
            continue
        if entry.get("muted") or _event_source_id(_event_from_state(entry)) in muted_sources:
            continue
        if key in initial_notified_keys:
            continue
        blocks.append(render_alert_notification(
            _event_from_state(entry), entry, outcome,
            template=config.get("_notify_template"),
        ))
        if delivery_ready:
            entry["notified_diagnosis_at"] = now_dt.isoformat()

    threshold = int((config.get("poll") or {}).get("source_fail_notify_after", 3))
    for source, fail_count in sorted(state.source_fail.items()):
        if fail_count == threshold and source not in muted_sources:
            if config.get("_notify_template"):
                blocks.append(render_template(config["_notify_template"], {
                    "kind": "告警源异常",
                    "title": "告警源连接异常",
                    "severity": "warning",
                    "severity_upper": "WARNING",
                    "platform": "-",
                    "target": source,
                    "host": source,
                    "service": "告警采集",
                    "description": f"告警源连续失败 {fail_count} 次。",
                    "status": "active",
                    "source": source,
                    "alertname": "source_degraded",
                    "key": f"source_degraded:{source}",
                    "time": now_dt.isoformat(),
                }) )
            else:
                blocks.append(f"source degraded: {source} 连续失败 {fail_count} 次")

    if blocks and delivery_ready:
        notifier.dispatch(blocks)
        counts["notified"] = 1
        for block in blocks:
            event_log.append(_log(
                now_dt, "notified", "", "", "notified", block[:120],
                severity=None, service=None,
            ))
    elif notifier is not None and not dry_run:
        if hasattr(notifier, "flush_queue"):
            notifier.flush_queue()
        else:
            notifier.dispatch([])

    return counts, actions, event_log


def _notifier_has_delivery(notifier) -> bool:
    if notifier is None:
        return False
    channels = getattr(notifier, "notifiers", None)
    return channels is None or bool(channels)


def _render_chronic_transition(event, entry, group, config):
    template = config.get("_notify_template")
    if not template:
        return render_chronic_transition_block(event, entry, group)
    return render_alert_notification(
        event,
        entry,
        None,
        template=template,
        kind="告警转为常驻",
        title=f"{event.alertname or '告警'} 转为常驻",
        description="持续告警已降噪，不再自动排查；后续仅关注主机增量和集群级恶化。",
    )


def _render_host_added(event, entry, new_hosts, active_count, config):
    template = config.get("_notify_template")
    if not template:
        return render_host_added_block(event, entry, new_hosts, active_count)
    hosts = "、".join(str(host) for host in new_hosts if host) or "-"
    return render_alert_notification(
        event,
        entry,
        None,
        template=template,
        kind="常驻主机变化",
        title=f"{event.alertname or '告警'} 新增受影响主机",
        description=f"新增受影响主机：{hosts}；当前受影响 {active_count} 台。",
        target=hosts,
    )


def _render_cluster_wide(event, entry, cluster_info, config):
    template = config.get("_notify_template")
    if not template:
        return render_cluster_wide_block(event, entry, cluster_info)
    active_count = cluster_info.get("active_count", "-")
    denominator = cluster_info.get("denominator", "-")
    threshold = cluster_info.get("threshold", "-")
    return render_alert_notification(
        event,
        entry,
        None,
        template=template,
        kind="集群级告警",
        title=f"{event.alertname or '告警'} 疑似集群级问题",
        description=f"当前受影响 {active_count}/{denominator} 台，已达到集群级阈值 {threshold} 台。",
    )


def _group_key(event: AlertEvent) -> str:
    return f"{event.source}:{event.platform}:{event.alertname}"


def _event_segment_key(event: AlertEvent) -> Tuple[str, str]:
    return event.key, str(event.source_active_at or "")


def _dynamic_flap_candidate(event: AlertEvent, profiles) -> bool:
    # tick 内事件只来自已配置的告警源,来源不再另行筛选
    return (
        not event.maintenance
        and baseline_class(event, profiles) == "watch"
    )


def _active_event_is_flapping(
    event: AlertEvent,
    profiles,
    flap_max_seconds: int,
    now_dt: datetime,
) -> bool:
    if (
        flap_max_seconds <= 0
        or str(event.state).lower() == "ok"
        or not _dynamic_flap_candidate(event, profiles)
    ):
        return False
    return _event_age_seconds(event, None, now_dt) <= flap_max_seconds


def _event_age_seconds(
    event: AlertEvent,
    entry,
    now_dt: datetime,
) -> float:
    started_at = event.source_active_at
    if not started_at and isinstance(entry, dict):
        started_at = entry.get("first_seen")
    try:
        return max(0.0, (now_dt - _parse_iso(started_at)).total_seconds())
    except (TypeError, ValueError):
        return 0.0


def _short_flapping_segments(
    events: List[AlertEvent],
    profiles,
    flap_max_seconds: int,
) -> set[Tuple[str, str]]:
    if flap_max_seconds <= 0:
        return set()
    firing_by_segment = {
        _event_segment_key(event): event
        for event in events
        if str(event.state).lower() != "ok"
    }
    result = set()
    for event in events:
        if str(event.state).lower() != "ok":
            continue
        segment_key = _event_segment_key(event)
        firing = firing_by_segment.get(segment_key)
        if firing is None or not _dynamic_flap_candidate(firing, profiles):
            continue
        try:
            duration = (
                _parse_iso(event.source_resolved_at)
                - _parse_iso(event.source_active_at)
            ).total_seconds()
        except (TypeError, ValueError):
            continue
        if 0 <= duration <= flap_max_seconds:
            result.add(segment_key)
    return result


def _new_chronic_group(
    now_dt: datetime,
    events: List[AlertEvent],
    *,
    notified_chronic_at,
) -> dict:
    hosts = sorted({
        event.host or event.host_raw
        for event in events
        if event.host or event.host_raw
    })
    upgraded_at = None if notified_chronic_at == "manual" else now_dt.isoformat()
    return {
        "notified_chronic_at": notified_chronic_at,
        "notified_cluster_wide_at": None,
        "chronic_host_history": hosts,
        "cluster_wide_triggered": False,
        "first_upgraded_at": upgraded_at,
    }


def _active_chronic_group_entries(
    state: AlertsState,
) -> Dict[str, List[Tuple[AlertEvent, dict]]]:
    grouped: Dict[str, List[Tuple[AlertEvent, dict]]] = {}
    for entry in state.active.values():
        if entry.get("class") != "chronic":
            continue
        event = _event_from_state(entry)
        grouped.setdefault(_group_key(event), []).append((event, entry))
    for items in grouped.values():
        items.sort(key=lambda item: item[0].host or item[0].host_raw or "")
    return grouped


def _group_muted_or_suppressed(
    active_items: List[Tuple[AlertEvent, dict]],
    suppressed_by: dict,
) -> bool:
    if not active_items:
        return True
    all_muted = all(entry.get("muted") for _event, entry in active_items)
    all_suppressed = all(event.key in suppressed_by for event, _entry in active_items)
    return all_muted or all_suppressed


def _platform_denominator(platform: str, config: dict) -> int:
    injected = config.get("_nodes_by_platform")
    if isinstance(injected, dict):
        nodes = injected.get(platform) or []
        return len(nodes)
    try:
        platform_config = _load_platforms().get(platform)
        return len(platform_config.nodes) if platform_config is not None else 0
    except Exception:
        return 0


def _append_event_log(root: Path, now_dt: datetime, rows: List[dict]) -> None:
    if not rows:
        return
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{now_dt.date().isoformat()}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _normalize_collect_result(result):
    if len(result) == 3:
        events, errors, successes = result
        return events, errors, set(successes or set())
    events, errors = result
    failed = {err.source for err in errors}
    successes = {_event_source_id(event) for event in events} - failed
    return events, errors, successes


def _event_source_id(event: AlertEvent) -> str:
    return event_source_id(event.source, event.platform)


def _rule_override_key(event: AlertEvent) -> str:
    return f"{_event_source_id(event)}:{event.alertname}"


def _entry_rule_override_key(entry: dict) -> str:
    return _rule_override_key(_event_from_state(entry))


def _source_id(name: str, source_cfg: dict) -> str:
    return source_id(name, source_cfg)


def _source_status(config: dict, successful_sources, source_errors) -> dict:
    successes = set(successful_sources or set())
    failed = {err.source for err in source_errors}
    result = {}
    for name, source_cfg in (config.get("sources") or {}).items():
        if source_cfg.get("enabled") is False:
            continue
        source_id = _source_id(name, source_cfg)
        result[str(name)] = "ok" if source_id in successes and source_id not in failed else "fail"
    return result


def _active_host_counts(active: dict, events: List[AlertEvent]) -> Dict[tuple, int]:
    hosts = {}
    for entry in active.values():
        event = _event_from_state(entry)
        host = event.host or event.host_raw
        if host:
            hosts.setdefault((event.platform, event.alertname), set()).add(host)
    for event in events:
        host = event.host or event.host_raw
        if host:
            hosts.setdefault((event.platform, event.alertname), set()).add(host)
    return {key: len(value) for key, value in hosts.items()}


def _dry_decision(event: AlertEvent, cls: str, ready: bool, routing: dict) -> str:
    if cls == "ignore":
        return "ignored"
    if cls in {"chronic", "flapping"}:
        return cls
    if not ready:
        return "deferred"
    hit = match_alert(routing, event.alertname, event.platform)
    return "would_triage" if hit and hit.playbook else "bare_alert"


def _dry_action_fields(key: str, entry: dict, ready: bool, routing: dict, muted_sources=frozenset()) -> dict:
    event = _event_from_state(entry)
    triage = entry.get("triage") or {}
    would_notify = (
        entry.get("class") == "watch"
        and ready
        and not entry.get("notified_initial_at")
        and not entry.get("muted")
        and _event_source_id(event) not in muted_sources
    )
    hit = match_alert(routing, event.alertname, event.platform)
    would_triage = (
        entry.get("class") == "watch"
        and ready
        and triage.get("status") == "pending"
        and bool(hit and hit.playbook)
    )
    decision = _dry_decision(event, entry.get("class"), ready, routing)
    return {
        "decision": decision,
        "would_notify": would_notify,
        "would_triage": would_triage,
    }


def _parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", help="缺省取环境包 config/alert-sources.yaml")
    ap.add_argument("--profiles", help="缺省取环境包 config/alert-profiles.yaml")
    ap.add_argument("--routing", default="build/routing.json")
    ap.add_argument("--state-dir", default="state")
    ap.add_argument("--dry-run", action="store_true")
    return ap.parse_args(argv)


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def _load_platforms() -> Dict[str, object]:
    # 告警平台 = managed 集群;来源路径按 cwd 相对解析(约定从仓库根运行)
    out = {}
    pack = load_env_pack(Path("."))
    for platform in pack.cluster_keys("managed"):
        path = pack.source_path(platform, "hosts")
        if path is not None and path.exists():
            out[platform] = load_platform(path)
    return out


def _credential_store() -> CredentialStore:
    """账号页凭据(扩展告警源按凭据项取账号;旧 credentials.json 同名键兼容)。"""
    repo_root = Path(__file__).resolve().parents[1]
    return CredentialStore(
        local_config_path(repo_root, "auth-profiles.json"),
        legacy_credentials_path=local_config_path(repo_root, "credentials.json"),
    )


def _load_credentials(repo_root: Path | None = None) -> dict:
    merged: dict = {}
    local_root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[1]
    for path in (local_config_path(local_root, "credentials.json"), Path.home() / ".ops-agent" / "credentials.json"):
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                merged.update(data)
    return merged


def _empty_counts(skipped_locked=False):
    return {
        "ok": True,
        "new": 0,
        "resolved": 0,
        "triaged": 0,
        "triage_failed": 0,
        "notified": 0,
        "deferred": 0,
        "skipped_locked": bool(skipped_locked),
        "source_errors": 0,
        "source_status": {},
        "replay_clamped": [],
    }


def _ensure_now(now) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, datetime):
        dt = now
    else:
        dt = datetime.fromisoformat(str(now))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_iso(value) -> datetime:
    dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _event_from_state(entry: dict) -> AlertEvent:
    data = entry.get("event") or {}
    return AlertEvent(
        source=data.get("source", "prometheus"),
        platform=data.get("platform", "unknown"),
        alertname=data.get("alertname", ""),
        severity=data.get("severity") or entry.get("severity") or "unknown",
        host=data.get("host"),
        host_raw=data.get("host_raw", ""),
        service=data.get("service", ""),
        state=data.get("state", "firing"),
        maintenance=bool(data.get("maintenance", False)),
        detail=data.get("detail", ""),
        source_active_at=data.get("source_active_at"),
        source_last_seen_at=data.get("source_last_seen_at"),
        source_resolved_at=data.get("source_resolved_at"),
    )


def _is_supported_event(event: AlertEvent) -> bool:
    return event.source != "patrol"


def _is_supported_entry(entry: dict) -> bool:
    return _is_supported_event(_event_from_state(entry))


def _log(now_dt: datetime, type_: str, key: str, cls: str, action: str, detail,
         *, severity, service, source_ts=None):
    return {
        "ts": source_ts or now_dt.isoformat(),
        "type": type_,
        "key": key,
        "class": cls,
        "action": action,
        "detail": detail,
        "severity": severity,
        "service": service,
    }


def _initialize_upgrade_gate(state: AlertsState, now_dt: datetime) -> None:
    if state.upgraded_at is None:
        state.upgraded_at = now_dt.isoformat()


def _event_source_sort_key(event: AlertEvent):
    source_ts = (
        event.source_resolved_at
        if str(event.state).lower() == "ok"
        else event.source_active_at
    ) or event.source_last_seen_at
    try:
        parsed = _parse_iso(source_ts) if source_ts else datetime.max.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        parsed = datetime.max.replace(tzinfo=timezone.utc)
    return parsed, 1 if str(event.state).lower() == "ok" else 0, event.key


def _should_skip_by_upgrade_gate(
    event: AlertEvent,
    upgraded_at,
    previous_active: dict,
) -> bool:
    if event.key not in previous_active or not event.source_active_at or not upgraded_at:
        return False
    try:
        return _parse_iso(event.source_active_at) < _parse_iso(upgraded_at)
    except (TypeError, ValueError):
        return False


def _load_seen_event_ids(records_root: Path, now_dt: datetime) -> set:
    root = Path(records_root)
    seen = set()
    for date_value in (now_dt.date(), (now_dt - timedelta(days=1)).date()):
        path = root / f"{date_value.isoformat()}.jsonl"
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                seen.add(timeline._normalize_row(json.loads(line))[0])
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
    return seen


def _append_unseen_event(event_log: List[dict], row: dict, seen_event_ids: set) -> bool:
    event_id = timeline._normalize_row(row)[0]
    if event_id in seen_event_ids:
        return False
    seen_event_ids.add(event_id)
    event_log.append(row)
    return True


if __name__ == "__main__":
    sys.exit(main())
