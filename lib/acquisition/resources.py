"""经 Prometheus 插件(MCP)采集主机与服务资源快照,映射到中台资源模型。

插件按新标签(platform / system / component / node)给数:资源快照、采集目标、具名查询三个 feeds 角色。
平台名取 ``platform`` 标签值,登记过的 IP 归一到平台配置里的节点名;JVM 与服务目录按
``config/resource-metrics.yaml`` 的映射段换成中台服务与组件名。插件没给的字段一律缺失,不填 0。
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

import yaml

from lib.atomic_file import atomic_write_json
from lib.config import PlatformConfig, load_platform
from lib.data_root import resolve_data_root
from lib.envpack import env_timezone, load_env_pack
from lib.plugins import PluginCallError, PluginService

from .store import snapshot_id


Feed = Callable[[str, Dict[str, Any]], Mapping[str, Any]]
TARGETS_LIMIT = 500


class ResourceSourceError(RuntimeError):
    """插件不可用或返回失败:本次采集整体失败,不出零值快照。"""


def collect_resource_snapshots(
    repo_root: Path,
    *,
    now: datetime | None = None,
    mode: str = "live",
    feed_factory: Callable[[Path], Tuple[Feed, str, str]] | None = None,
) -> tuple[list[dict], dict]:
    repo_root = Path(repo_root)
    config = _load_yaml(load_env_pack(repo_root).config_file("resource-metrics.yaml"))
    platforms = _load_platforms(repo_root)
    feed, source, source_url = (feed_factory or _plugin_feed)(repo_root)

    now_dt = _as_datetime(now or datetime.now(timezone.utc))
    at = _cst(now_dt)
    scheduled = _floor_five_minutes(now_dt).astimezone(env_timezone()).isoformat()
    errors: list[dict] = []
    snapshot = _call(feed, "snapshot", {"at": at}, errors)
    hosts = [host for host in snapshot.get("hosts") or [] if isinstance(host, Mapping)]
    sample_at = _stamp_iso(snapshot.get("at"))
    node_cfg = config.get("node") or {}
    excluded = {_platform_name(value) for value in node_cfg.get("excluded_platforms") or []}
    topology = _topology(platforms, hosts, excluded)
    targets = _collect_targets(feed, config, topology, errors)

    node_rows = _node_rows(config, hosts, topology, sample_at)
    service_rows = _service_rows(config, hosts, topology, sample_at)
    business_rows = _business_rows(feed, config, topology, targets, at, errors)
    catalog = _build_catalog(config, platforms, targets, service_rows, collected_at=now_dt)

    snapshots = []
    for platform in sorted(name for name in topology if not name.startswith("_")):
        nodes = sorted((row for row in node_rows if row["platform"] == platform), key=lambda row: row["node"])
        services = sorted(
            (row for row in service_rows if row["platform"] == platform),
            key=lambda row: (row["node"], row["service"], row["component"], row.get("role") or ""),
        )
        expected_nodes = len(topology[platform]["nodes"])
        observed_nodes = sum(row["status"] == "ok" for row in nodes)
        platform_errors = [item for item in errors if item.get("platform") in {None, platform}]
        status = "ok"
        if observed_nodes == 0:
            status = "error"
        elif platform_errors or observed_nodes < expected_nodes:
            status = "partial"
        snapshots.append({
            "schema_version": 1,
            "snapshot_id": snapshot_id(platform, scheduled),
            "scheduled_at": scheduled,
            "collected_at": now_dt.astimezone(env_timezone()).isoformat(),
            "platform": platform,
            "source": source,
            "source_url": source_url,
            "mode": mode,
            "status": status,
            "coverage": {
                "expected_nodes": expected_nodes,
                "observed_nodes": observed_nodes,
                "expected_services": sum(1 for row in catalog["services"] if row["platform"] == platform),
                "observed_services": sum(row["status"] == "ok" for row in services),
            },
            "nodes": nodes,
            "services": services,
            "business_metrics": [row for row in business_rows if row["platform"] == platform],
            "missing": [
                _missing_service(row) for row in catalog["services"]
                if row["platform"] == platform and row["status"] != "ok"
            ],
            "errors": platform_errors,
        })
    return snapshots, catalog


def save_catalog(path: Path, catalog: dict) -> None:
    atomic_write_json(path, catalog)


def _plugin_feed(repo_root: Path) -> Tuple[Feed, str, str]:
    """声明了 feeds.resources 的插件;地址取插件页,不看插件开关。"""
    service = PluginService(repo_root, resolve_data_root(repo_root) / "state")
    plugin = service.registry.feed("resources")

    def feed(role: str, arguments: Dict[str, Any]) -> Mapping[str, Any]:
        return service.feed_call("resources", role, arguments, plugin=plugin)

    return feed, f"mcp:{plugin.id}", plugin.url


def _call(feed: Feed, role: str, arguments: Dict[str, Any], errors: list) -> Mapping[str, Any]:
    try:
        result = feed(role, arguments)
    except PluginCallError as exc:
        raise ResourceSourceError(f"{exc.code}: {exc.message}") from exc
    data = result.get("data")
    if not isinstance(data, Mapping):
        raise ResourceSourceError(f"插件 {role} 返回结构无效")
    errors.extend({"scope": "source", "role": role, "warning": str(text)} for text in result.get("warnings") or [])
    return data


# ---- 平台与节点 ----

def _topology(platforms: Mapping[str, PlatformConfig], hosts, excluded) -> dict:
    """平台配置里的节点 + 快照里新出现、且有主机采集目标的主机(按 platform 标签,节点名用 IP)。"""
    result = {
        name: {"nodes": {node: info.host for node, info in config.nodes.items()}}
        for name, config in platforms.items() if name not in excluded
    }
    ip_map = {
        str(ip): (platform, node)
        for platform, data in result.items() for node, ip in data["nodes"].items()
    }
    for host in hosts:
        ip = str(host.get("node") or "")
        platform = _platform_name(host.get("platform"))
        # 只有进程等其他采集、没有主机采集目标的不算一台受监控主机(与原先按主机采集目标发现平台一致)
        if not ip or ip in ip_map or platform in excluded or host.get("collector_up") is None:
            continue
        result.setdefault(platform, {"nodes": {}})["nodes"][ip] = ip
        ip_map[ip] = (platform, ip)
    result["_ip_map"] = ip_map
    return result


def _place(labels: Mapping[str, Any], topology) -> Optional[Tuple[str, str]]:
    ip = str(labels.get("node") or _instance_host(labels.get("instance")) or "")
    return topology["_ip_map"].get(ip)


# ---- 节点与文件系统 ----

def _node_rows(config, hosts, topology, sample_at) -> list[dict]:
    node_cfg = config.get("node") or {}
    by_node = {}
    for host in hosts:
        placed = _place(host, topology)
        if placed is not None:
            by_node[placed] = host
    filesystems = _filesystems(node_cfg.get("filesystem") or {}, by_node, sample_at)
    rows = []
    for platform in sorted(name for name in topology if not name.startswith("_")):
        for node, ip in sorted(topology[platform]["nodes"].items()):
            host = by_node.get((platform, node))
            metrics = _mapped(host or {}, node_cfg.get("fields") or {})
            collector_up = _number((host or {}).get("collector_up"))
            status = (
                "target_missing" if collector_up is None  # 没有主机采集目标(只有进程等其他采集)
                else "target_down" if collector_up != 1
                else "ok" if metrics
                else "no_data"
            )
            if status != "ok":
                metrics = {}
            _derive_node_metrics(metrics)
            rows.append({
                "node": node,
                "platform": platform,
                "instance": ip,
                "sample_at": sample_at if status == "ok" else None,
                "status": status,
                "metrics": metrics,
                "filesystems": sorted(filesystems.get((platform, node), []), key=lambda item: item["mountpoint"])
                if status == "ok" else [],
            })
    return rows


def _derive_node_metrics(metrics: dict) -> None:
    total, available = metrics.get("memory_total_bytes"), metrics.get("memory_available_bytes")
    if total and available is not None:
        metrics["memory_used_pct"] = _number(100 * (1 - available / total))
    swap_total, swap_used = metrics.get("swap_total_bytes"), metrics.get("swap_used_bytes")
    if swap_total is not None and swap_used is not None:
        metrics["swap_free_bytes"] = _number(swap_total - swap_used)
        metrics["swap_used_pct"] = _number(100 * swap_used / swap_total) if swap_total else 0.0
    elif metrics:
        metrics["swap_used_pct"] = None


def _filesystems(fs_cfg, by_node, sample_at) -> dict:
    shared_types = set(fs_cfg.get("shared_fstypes") or [])
    static_types = set(fs_cfg.get("static_fstypes") or [])
    allowed = set(fs_cfg.get("local_fstypes") or []) | shared_types | static_types
    result = defaultdict(list)
    shared_seen = set()
    for (platform, node), host in sorted(by_node.items()):
        for item in host.get("filesystems") or []:
            fstype = str(item.get("fstype") or "")
            if fstype not in allowed:
                continue
            metrics = _mapped(item, fs_cfg.get("fields") or {})
            size, available = metrics.get("size_bytes"), metrics.get("available_bytes")
            if not size or size <= 0:
                continue
            device, mountpoint = str(item.get("device") or ""), str(item.get("mountpoint") or "")
            if fstype in shared_types:
                if (platform, device, mountpoint) in shared_seen:
                    continue
                shared_seen.add((platform, device, mountpoint))
            files, files_free = metrics.get("files"), metrics.get("files_free")
            result[(platform, node)].append({
                "device": device,
                "mountpoint": mountpoint,
                "fstype": fstype,
                "kind": "shared" if fstype in shared_types else "static" if fstype in static_types else "local",
                "sample_at": sample_at,
                **metrics,
                "used_pct": _number(100 * (1 - available / size)) if available is not None else None,
                "inode_used_pct": _number(100 * (1 - files_free / files))
                if files and files_free is not None else None,
            })
    return result


# ---- JVM 服务 ----

def _service_rows(config, hosts, topology, sample_at) -> list[dict]:
    jvm_cfg = config.get("jvm") or {}
    components = config.get("jvm_components") or {}
    rows = []
    for host in hosts:
        placed = _place(host, topology)
        if placed is None:
            continue
        platform, node = placed
        processes = {str(item.get("groupname") or ""): item for item in host.get("processes") or []}
        jvms = [item for item in host.get("jvms") or [] if item.get("system") and item.get("component")]
        keys = [_jvm_key(item) for item in jvms]
        for item, key in zip(jvms, keys):
            mapped = components.get(key) or {}
            metrics = _mapped(item, jvm_cfg.get("fields") or {})
            metrics.update(_mapped(processes.get(key) or {}, jvm_cfg.get("process_fields") or {}))
            used, maximum = metrics.get("heap_used_bytes"), metrics.get("heap_max_bytes")
            metrics["heap_used_pct"] = _number(100 * used / maximum) \
                if used is not None and maximum and maximum > 0 else None
            old_gen = _number(item.get(jvm_cfg.get("old_gen_after_gc") or ""))
            rows.append({
                "platform": platform,
                "node": node,
                "service": str(mapped.get("service") or item["system"]),
                "component": str(mapped.get("component") or _component_name(key)),
                "job": key,
                # 同一节点同类 JVM 多实例时按实例区分
                "role": str(item.get("instance") or "") or None if keys.count(key) > 1 else None,
                "profile": "jvm",
                "status": "ok" if used is not None else "no_data",
                "sample_at": sample_at if used is not None else None,
                "metrics": metrics,
                "gc": {},
                "old_gen_after_gc_bytes": {"old_gen": old_gen} if old_gen is not None else {},
            })
    return rows


def _jvm_key(item: Mapping[str, Any]) -> str:
    return f"{item.get('system')}/{item.get('component')}"


def _component_name(key: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", key.upper()).strip("_")


# ---- 采集目标与服务目录 ----

def _collect_targets(feed, config, topology, errors) -> list[dict]:
    """按平台标签逐个取全部采集目标;端口探测类 target 不代表指标采集,判服务状态时不计。"""
    ignore = re.compile(str((config.get("targets") or {}).get("ignore_jobs") or r"(?!)"))
    rows = []
    for platform in sorted(name for name in topology if not name.startswith("_")):
        data = _call(feed, "targets", {"health": "all", "filters": {"platform": platform}, "limit": TARGETS_LIMIT},
                     errors)
        if data.get("truncated"):
            raise ResourceSourceError(f"平台 {platform} 的采集目标超过 {TARGETS_LIMIT} 条被截断")
        for target in data.get("targets") or []:
            labels = target.get("labels") or {}
            job = str(labels.get("job") or "")
            placed = _place(labels, topology)
            if placed is None or ignore.search(job):
                continue
            rows.append({
                "platform": placed[0],
                "node": placed[1],
                "job": job,
                "key": f"{labels.get('system')}/{labels.get('component')}",
                "labels": {str(key): str(value) for key, value in labels.items()},
                "status": "ok" if target.get("health") == "up" else "target_down",
            })
    return rows


def _build_catalog(config, platforms, targets, service_rows, *, collected_at):
    component_labels = config.get("component_labels") or {}
    jvm_components = config.get("jvm_components") or {}
    observed = {(row["platform"], row["node"], row["component"]): row for row in service_rows}
    target_status: dict[tuple, str] = {}
    for item in targets:
        identity = (item["platform"], item["node"], item["key"])
        if target_status.get(identity) != "ok":
            target_status[identity] = item["status"]
    services = []
    unmonitored = []
    for platform, platform_cfg in platforms.items():
        for node, node_info in platform_cfg.nodes.items():
            for component in node_info.components:
                key = component_labels.get(component)
                if not key:
                    if _server_component(component):
                        unmonitored.append({
                            "platform": platform, "node": node, "component": component, "status": "unsupported",
                        })
                    continue
                existing = observed.get((platform, node, component))
                status = "ok" if existing and existing.get("status") == "ok" else (
                    target_status.get((platform, node, key)) or "unsupported"
                )
                services.append({
                    "platform": platform,
                    "node": node,
                    "component": component,
                    "service": existing.get("service") if existing else str(
                        (jvm_components.get(key) or {}).get("service") or key.split("/", 1)[0]
                    ),
                    "job": key,
                    "profile": "jvm" if existing or key in jvm_components else "unsupported",
                    "status": status,
                })
    # 有 JVM 指标但拓扑里没有对应组件的业务服务,照样进目录
    identities = {(row["platform"], row["node"], row["job"], row.get("role")) for row in services}
    for row in service_rows:
        identity = (row["platform"], row["node"], row["job"], row.get("role"))
        if identity in identities:
            continue
        services.append({
            field: row.get(field)
            for field in ("platform", "node", "component", "service", "job", "profile", "role", "status")
        })
    return {
        "schema_version": 1,
        "updated_at": collected_at.astimezone(env_timezone()).isoformat(),
        "services": sorted(services, key=lambda row: (
            row["platform"], row["node"], str(row.get("service")), str(row.get("component")),
        )),
        "unmonitored_components": sorted(unmonitored, key=lambda row: (
            row["platform"], row["node"], row["component"],
        )),
    }


# ---- 业务指标 ----

def _business_rows(feed, config, topology, targets, at, errors) -> list[dict]:
    rows = []
    for profile, profile_cfg in (config.get("business_metrics") or {}).items():
        service = str(profile_cfg.get("service") or profile)
        selector = profile_cfg.get("targets") or {}
        profile_targets = [
            row for row in targets
            if all(row["labels"].get(str(key)) == str(value) for key, value in selector.items())
        ]
        for metric, metric_cfg in (profile_cfg.get("metrics") or {}).items():
            label = str(metric_cfg.get("label") or metric)
            unit = str(metric_cfg.get("unit") or "number")
            scale = float(metric_cfg.get("scale") or 1)
            try:
                data = _call(feed, "query", {"name": str(metric_cfg["query"]), "at": at}, errors)
            except ResourceSourceError as exc:
                # 只影响这一项指标:逐行标 source_error 并写明原因,不拖累整个平台快照的状态
                rows.extend(_business_row(
                    target["platform"], target["node"], service, target["job"], str(metric), label, unit,
                    value=None, status="source_error", severity="unknown", sample_at=None,
                    details={"error": str(exc)[:300]},
                ) for target in profile_targets)
                continue
            target_lookup = {(row["platform"], row["node"], row["job"]): row for row in profile_targets}
            observed = set()
            for series in data.get("series") or []:
                labels = series.get("labels") or {}
                placed = _place(labels, topology)
                job = str(labels.get("job") or "")
                if placed is None or (placed[0], placed[1], job) not in target_lookup:
                    continue
                identity = (placed[0], placed[1], job)
                observed.add(identity)
                value = _number(series.get("value"))
                value = None if value is None else _number(value * scale)
                status = "target_down" if target_lookup[identity]["status"] == "target_down" else (
                    "ok" if value is not None else "no_data"
                )
                rows.append(_business_row(
                    placed[0], placed[1], service, job, str(metric), label, unit,
                    value=None if status == "target_down" else value,
                    status=status,
                    severity=_business_metric_severity(value, metric_cfg) if status == "ok" else "unknown",
                    sample_at=_stamp_iso(data.get("at")) if status == "ok" else None,
                    details={
                        str(key): str(raw) for key, raw in labels.items()
                        if key not in {"__name__", "group", "instance", "job", "platform", "node",
                                       "system", "component"}
                    },
                ))
            if metric_cfg.get("optional"):
                continue
            for identity, target in target_lookup.items():
                if identity in observed:
                    continue
                rows.append(_business_row(
                    target["platform"], target["node"], service, target["job"], str(metric), label, unit,
                    value=None, status="target_down" if target["status"] == "target_down" else "no_data",
                    severity="unknown", sample_at=None,
                ))
    return sorted(rows, key=lambda row: (
        row["platform"], row["node"], row["service"], row["metric"], row["job"],
    ))


def _business_metric_severity(value, config) -> str:
    if value is None:
        return "unknown"
    critical = config.get("critical_above")
    warning = config.get("warning_above")
    if critical is not None and value >= float(critical):
        return "critical"
    if warning is not None and value >= float(warning):
        return "warning"
    return "healthy"


def _business_row(
    platform, node, service, job, metric, label, unit, *, value, status,
    severity, sample_at, details=None,
):
    return {
        "platform": platform,
        "node": node,
        "service": service,
        "job": job,
        "metric": metric,
        "label": label,
        "unit": unit,
        "value": value,
        "status": status,
        "severity": severity,
        "sample_at": sample_at,
        "details": details or {},
    }


# ---- 工具 ----

def _mapped(source: Mapping[str, Any], fields: Mapping[str, Any]) -> dict:
    """按映射表取字段:{中台字段: 插件字段} 或 {中台字段: {from, scale}};插件没给的字段不出现。"""
    out = {}
    for target, spec in fields.items():
        name, scale = (spec, 1.0) if isinstance(spec, str) else (spec.get("from"), float(spec.get("scale") or 1))
        value = _number(source.get(str(name)))
        if value is not None:
            out[str(target)] = _number(value * scale)
    return out


def _missing_service(row):
    return {field: row.get(field) for field in ("service", "component", "node", "job", "status")}


def _server_component(component: str) -> bool:
    value = str(component)
    if value.endswith("_CLIENT") or value in {"PIG", "SQOOP", "HCAT"}:
        return False
    return True


def _platform_name(value) -> str:
    raw = str(value or "ungrouped").strip().lower()
    normalized = "".join(
        character if character.isalnum() or character in {"-", "_"} else "-"
        for character in raw
    ).strip("-_")
    return normalized or "ungrouped"


def _instance_host(value) -> str:
    instance = str(value or "").strip()
    host, _, port = instance.rpartition(":")
    return host if host and port.isdigit() else instance


def _load_platforms(repo_root: Path) -> dict[str, PlatformConfig]:
    result = {}
    pack = load_env_pack(repo_root)
    for platform in pack.cluster_keys("managed"):
        path = pack.source_path(platform, "hosts")
        if path is not None and path.is_file():
            loaded = load_platform(path)
            result[loaded.platform] = loaded
    if not result:
        raise FileNotFoundError("未找到 managed 集群的平台配置")
    return result


def _load_yaml(path: Path) -> dict:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if int(data.get("version", 0)) != 2:
        raise ValueError("仅支持 resource-metrics version=2(插件映射)")
    return data


def _number(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _stamp_iso(stamp: Any) -> Optional[str]:
    """ops-mcp 时间双视图 → ISO8601;取 real,无时区字符串按环境时区(CST)解析。"""
    text = stamp.get("real") if isinstance(stamp, Mapping) else None
    try:
        parsed = datetime.strptime(str(text), "%Y-%m-%d %H:%M:%S").replace(tzinfo=env_timezone())
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(timezone.utc).isoformat()


def _cst(value: datetime) -> str:
    return value.astimezone(env_timezone()).strftime("%Y-%m-%d %H:%M:%S")


def _floor_five_minutes(value: datetime) -> datetime:
    minute = value.minute - value.minute % 5
    return value.replace(minute=minute, second=0, microsecond=0)


def _as_datetime(value) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
