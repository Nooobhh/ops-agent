"""Build a deterministic, provenance-carrying operations topology graph.

The platform YAML files are the source of truth for observed nodes,
components, and endpoints.  Knowledge-base service indexes add documented
service dependencies; knowledge documents are attached to the graph so a
later RAG layer can expand from infrastructure entities to evidence.

This module only builds an offline artifact.  It does not connect to a
cluster, probe a host, or make the graph part of retrieval ranking.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

import yaml

from lib.data_root import resolve_data_root
from lib.envpack import KNOWLEDGE_ENV_SCOPE, active_env_pack, load_env_pack

from .bases import discover_bases, iter_documents, resolve_knowledge_root, split_doc_path
from .frontmatter import FrontmatterError, SERVICES, load_doc


SCHEMA_VERSION = 1


def knowledge_clusters() -> tuple[str, ...]:
    """按集群分目录的知识库平台(knowledge 角色)的知识库键。"""
    return tuple(item.knowledge_key for item in active_env_pack().clusters if "knowledge" in item.roles)


def graph_platforms() -> tuple[str, ...]:
    """拓扑图平台:knowledge 集群 + 环境级范围。"""
    return active_env_pack().knowledge_platforms()


def platform_config_specs(config_root: Path) -> list[tuple[str, Path, bool]]:
    """(图平台, 主机来源, 是否必需):knowledge 集群各成平台;其余集群并入环境级,缺失即跳过。"""
    pack = active_env_pack()
    pack_root = Path(config_root).parent  # 来源路径相对环境包根;调用方给的是该包的 config/
    specs = []
    for decl in pack.clusters:
        spec = decl.sources.get("hosts")
        if spec is None:
            continue
        path = pack_root / spec
        if "knowledge" in decl.roles:
            specs.append((decl.knowledge_key, path, True))
        else:
            specs.append((KNOWLEDGE_ENV_SCOPE, path, False))
    return specs


# 组件 → 服务、endpoint 名提示与服务别名是环境数据(环境包 config/component-services.yaml)。
# 管理面组件名比服务词表细,映射保持显式:未映射的组件仍出现在图里并报数据缺口,不猜。
SERVICE_MAP_FILE = "component-services.yaml"
_SERVICE_MAP_CACHE: dict[str, Any] = {}


def _service_map() -> dict[str, Any]:
    """读环境包的组件/服务映射(按 mtime 缓存);文件缺失即空映射,格式错显式失败。"""
    path = active_env_pack().config_file(SERVICE_MAP_FILE)
    try:
        stat = path.stat()
    except OSError:
        return {"components": {}, "endpoint_hints": (), "service_aliases": {}}
    signature = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _SERVICE_MAP_CACHE.get("value")
    if cached is not None and cached[0] == signature:
        return cached[1]
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict) or data.get("version") != 1:
        raise TopologyError(f"{path}: 仅支持 version: 1")
    value = {
        "components": {str(key).upper(): str(item) for key, item in (data.get("components") or {}).items()},
        "endpoint_hints": tuple((str(marker).lower(), str(service))
                                for marker, service in (data.get("endpoint_hints") or [])),
        "service_aliases": {str(key).lower(): str(item) for key, item in (data.get("service_aliases") or {}).items()},
    }
    _SERVICE_MAP_CACHE["value"] = (signature, value)
    return value


_H2_RE = re.compile(r"^##\s+(.+?)\s*$")


class TopologyError(ValueError):
    """The topology source is present but structurally invalid."""


def _source(kind: str, path: str, collected_at: Any = None) -> dict[str, Any]:
    result: dict[str, Any] = {"kind": kind, "path": path}
    if collected_at not in (None, ""):
        result["collected_at"] = str(collected_at)
    return result


def _clean_label(value: Any) -> str:
    text = str(value or "").strip().strip("`")
    text = re.sub(r"\s+", " ", text)
    return text


def _service_slug(value: Any) -> Optional[str]:
    text = _clean_label(value)
    text = re.split(r"\s*[(（]", text, maxsplit=1)[0].strip()
    text = text.rstrip("：:")
    lower = text.lower()
    aliases = _service_map()["service_aliases"]
    if lower in aliases:
        return aliases[lower]
    if lower in SERVICES:
        return lower
    # Dependency tables normally use product-cased names.  Keep this
    # conservative so a free-form sentence cannot create a fake service.
    for service in sorted(SERVICES, key=len, reverse=True):
        if lower == service.lower():
            return service
    return None


def component_service(component: str) -> Optional[str]:
    """Map a management-plane component name to the KB service vocabulary."""
    return _service_map()["components"].get(component.upper())


def _node_id(platform: str, name: str) -> str:
    return f"node:{platform}:{name}"


def _service_id(platform: str, service: str) -> str:
    return f"service:{platform}:{service}"


class _Graph:
    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: dict[tuple[str, str, str, str], dict[str, Any]] = {}

    def node(
        self,
        node_id: str,
        kind: str,
        name: str,
        platform: Optional[str],
        *,
        properties: Optional[dict[str, Any]] = None,
        provenance: Optional[dict[str, Any]] = None,
    ) -> None:
        current = self.nodes.get(node_id)
        if current is None:
            current = {
                "id": node_id,
                "kind": kind,
                "name": name,
                "platform": platform,
                "properties": {},
                "provenance": [],
            }
            self.nodes[node_id] = current
        if properties:
            current["properties"].update(properties)
        if provenance and provenance not in current["provenance"]:
            current["provenance"].append(provenance)

    def edge(
        self,
        source: str,
        target: str,
        relation: str,
        *,
        properties: Optional[dict[str, Any]] = None,
        provenance: Optional[dict[str, Any]] = None,
    ) -> None:
        key = (source, target, relation, "")
        current = self.edges.get(key)
        if current is None:
            current = {
                "source": source,
                "target": target,
                "relation": relation,
                "properties": properties or {},
                "provenance": [],
            }
            self.edges[key] = current
        elif properties:
            for name, value in properties.items():
                if name not in current["properties"]:
                    current["properties"][name] = value
                elif current["properties"][name] != value:
                    existing = current["properties"][name]
                    values = existing if isinstance(existing, list) else [existing]
                    if value not in values:
                        current["properties"][name] = [*values, value]
        if provenance and provenance not in current["provenance"]:
            current["provenance"].append(provenance)


def _endpoint_parts(entry: Any) -> tuple[Optional[str], Optional[int], dict[str, Any]]:
    if isinstance(entry, str):
        parsed = urlparse(entry)
        return parsed.hostname, parsed.port, {"url": entry}
    if not isinstance(entry, dict):
        return None, None, {}
    url = entry.get("url")
    host = entry.get("host")
    port = entry.get("port")
    properties = {
        str(key): value for key, value in entry.items()
        if key not in {"source", "collected_at"}
    }
    if isinstance(url, str) and url.strip():
        properties["url"] = url
        parsed = urlparse(url)
        host = parsed.hostname or host
        if port in (None, ""):
            port = parsed.port
    try:
        parsed_port = int(port) if port not in (None, "") else None
    except (TypeError, ValueError):
        parsed_port = None
    return (str(host).strip() if host not in (None, "") else None), parsed_port, properties


def _infer_endpoint_service(name: str) -> Optional[str]:
    lower = name.lower()
    for marker, service in _service_map()["endpoint_hints"]:
        if marker in lower:
            return service
    return None


def _normalise_related(value: Any) -> str:
    target = _clean_label(value)
    if target.startswith("[[") and target.endswith("]]" ):
        target = target[2:-2]
    target = target.split("|", 1)[0].split("#", 1)[0]
    return target.strip().replace("\\", "/")


def _index_service(path: Path, knowledge_root: Path, doc: Any) -> Optional[str]:
    """Recover the service slug from legacy service index paths."""
    if not doc.is_index or doc.service:
        return doc.service
    base, inner = split_doc_path(path.relative_to(knowledge_root), discover_bases(knowledge_root))
    if base is not None and base.layout == "flat" and inner.parts == ("_index.md",):
        return base.service
    if base is not None and len(inner.parts) == 3 and inner.name == "_index.md":
        candidate = _service_slug(inner.parts[1])
        if candidate:
            return candidate
    return None


def _table_rows(path: Path, section_title: str) -> list[dict[str, str]]:
    """Read simple Markdown table rows under one H2 section."""
    lines = path.read_text(encoding="utf-8").splitlines()
    in_section = False
    headers: Optional[list[str]] = None
    rows: list[dict[str, str]] = []
    for line in lines:
        heading = _H2_RE.match(line)
        if heading:
            title = heading.group(1).strip()
            if title == section_title:
                in_section = True
                continue
            if in_section:
                break
        if not in_section or not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not cells or all(set(cell) <= {"-", ":", " "} for cell in cells):
            continue
        if headers is None:
            headers = cells
            continue
        if len(cells) < len(headers):
            cells.extend([""] * (len(headers) - len(cells)))
        rows.append(dict(zip(headers, cells)))
    return rows


def _table_rows_any(path: Path, section_titles: Iterable[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for title in section_titles:
        rows.extend(_table_rows(path, title))
    return rows


def _port_tokens(value: str) -> list[str]:
    return sorted(set(re.findall(r"(?<!\d)\d{2,5}(?!\d)", value)))


def _parse_baseline(path: Path) -> dict[str, Any]:
    """Extract stable component/config facts from generated service baselines."""
    components: dict[str, dict[str, Any]] = {}
    for row in _table_rows_any(path, ("组件分布", "组件与节点")):
        component = _clean_label(row.get("组件"))
        if not component or component == "—":
            continue
        nodes = [
            item.strip() for item in re.split(r"[,，]", row.get("节点", ""))
            if item.strip() and item.strip() != "—"
        ]
        count = row.get("节点数", "")
        components[component] = {
            "baseline_nodes": nodes,
            "baseline_node_count": int(count) if count.isdigit() else None,
            "observed_ports": [],
            "processes": [],
            "process_statuses": [],
            "observed_state": [],
        }
    configs = []
    for row in _table_rows(path, "关键配置"):
        if row.get("键"):
            configs.append({
                "file": row.get("配置文件", ""),
                "key": row.get("键", ""),
                "value": row.get("值", ""),
            })
    for row in _table_rows_any(path, ("实测状态", "最新观测", "实测结果")):
        component = _clean_label(row.get("组件"))
        if not component or component == "—":
            continue
        item = {
            "node": row.get("节点", ""),
            "component": component,
            "process": row.get("进程", ""),
            "port": row.get("端口", ""),
        }
        entry = components.setdefault(component, {
            "baseline_nodes": [],
            "baseline_node_count": None,
            "observed_ports": [],
            "processes": [],
            "process_statuses": [],
            "observed_state": [],
        })
        entry["observed_state"].append(item)
        entry["observed_ports"].extend(_port_tokens(row.get("端口", "")))
        process = row.get("进程", "").strip()
        if process in {"✓", "✗", "未采集"}:
            entry["process_statuses"].append(process)
        elif process and process != "—":
            entry["processes"].append(process)
    for entry in components.values():
        entry["observed_ports"] = sorted(set(entry["observed_ports"]))
        entry["processes"] = sorted(set(entry["processes"]))
        entry["process_statuses"] = sorted(set(entry["process_statuses"]))
    return {"components": components, "configs": configs}


def _relation_ref(value: Any, platform: Optional[str], service: Optional[str]) -> Optional[str]:
    """Resolve an explicit v2 relation endpoint without inferring edges."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("\\", "/")
    if raw.startswith(("service:", "node:", "component:", "endpoint:", "document:")):
        return raw
    if raw[:2] == "[[" and raw[-2:] == "]]":
        raw = raw[2:-2].split("|", 1)[0].split("#", 1)[0].strip()
    parts = [part for part in raw.split("/") if part]
    platforms = graph_platforms()
    if len(parts) >= 3 and parts[0] not in platforms and parts[1] in platforms:
        parts = parts[1:]           # 文档 ID 写法带库前缀(cluster/iot/...),按库内路径解析
    if len(parts) >= 2 and parts[0] in platforms:
        return _service_id(parts[0], parts[1])
    if len(parts) == 1 and platform and raw in SERVICES:
        return _service_id(platform, raw)
    if raw in SERVICES and platform:
        return _service_id(platform, raw)
    if service and raw == service and platform:
        return _service_id(platform, service)
    return None


def _document_relations(
    doc: Any,
    *,
    platform: Optional[str],
    service: Optional[str],
    source_path: str,
) -> Iterable[tuple[str, str, str, dict[str, Any], dict[str, Any]]]:
    """Yield only explicitly declared v2 relations with document provenance."""
    for relation in doc.relations:
        relation_name = str(relation.get("relation") or "").strip()
        target = _relation_ref(
            relation.get("target") or relation.get("target_service"), platform, service,
        )
        source = _relation_ref(
            relation.get("source") or relation.get("source_service"), platform, service,
        ) or (_service_id(platform, service) if platform and service else None)
        if not source or not target or not relation_name:
            continue
        properties = relation.get("properties")
        if not isinstance(properties, dict):
            properties = {}
        for key in ("purpose", "confidence", "evidence_ref", "observed_at", "valid_until"):
            if key in relation and key not in properties:
                properties[key] = relation[key]
        yield source, target, relation_name, properties, _source("knowledge", source_path)


def _fingerprint(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted({Path(item).resolve() for item in paths if Path(item).is_file()}):
        digest.update(str(path).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def build_topology(
    config_root: Path,
    knowledge_root: Path,
    snapshot_root: Optional[Path] = None,
) -> dict[str, Any]:
    """Compile platform configuration, cluster topology snapshots, and KB topology."""
    config_root = Path(config_root).resolve()
    knowledge_root = Path(knowledge_root).resolve()
    graph = _Graph()
    warnings: list[str] = []
    warning_keys: set[str] = set()
    source_paths: list[Path] = []
    platform_nodes: dict[str, str] = {}
    cluster_nodes: dict[str, str] = {}
    host_lookup: dict[tuple[str, str], str] = {}
    service_platforms: set[tuple[str, str]] = set()
    unmapped_components: set[tuple[str, str]] = set()

    def warn(message: str, key: Optional[str] = None) -> None:
        dedupe_key = key or message
        if dedupe_key not in warning_keys:
            warning_keys.add(dedupe_key)
            warnings.append(message)

    def ensure_platform(platform: str, provenance: Optional[dict[str, Any]] = None) -> str:
        node_id = f"platform:{platform}"
        graph.node(node_id, "platform", platform, platform, provenance=provenance)
        platform_nodes[platform] = node_id
        return node_id

    def ensure_service(
        platform: str,
        service: str,
        provenance: Optional[dict[str, Any]] = None,
    ) -> str:
        if service not in SERVICES:
            warn(f"未映射服务 {platform}/{service}", f"service:{platform}:{service}")
        node_id = _service_id(platform, service)
        graph.node(node_id, "service", service, platform, provenance=provenance)
        service_platforms.add((platform, service))
        platform_id = ensure_platform(platform)
        parent_id = (
            platform_id
            if platform == KNOWLEDGE_ENV_SCOPE
            else cluster_nodes.get(platform, platform_id)
        )
        graph.edge(parent_id, node_id, "contains_service", provenance=provenance)
        return node_id

    config_specs = [
        (platform, path)
        for platform, path, required in platform_config_specs(config_root)
        if required or path.is_file()
    ]

    cluster_ids: set[str] = set()
    for platform, config_path in config_specs:
        if not config_path.is_file():
            warn(f"平台配置不存在，跳过节点拓扑: {config_path}", str(config_path))
            continue
        source_paths.append(config_path)
        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise TopologyError(f"读取平台配置失败: {config_path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise TopologyError(f"平台配置顶层必须是 mapping: {config_path}")
        if raw.get("platform") not in (None, platform):
            raise TopologyError(
                f"平台配置字段与文件名不一致: {config_path} -> {raw.get('platform')!r}"
            )
        collected_at = raw.get("collected_at")
        config_source = _source(
            "platform_config", config_path.relative_to(config_root.parent).as_posix(), collected_at
        )
        platform_id = ensure_platform(platform, config_source)
        cluster_name = str(raw.get("cluster_name") or platform)
        cluster_id = f"cluster:{platform}:{cluster_name}"
        cluster_nodes[platform] = cluster_id
        cluster_ids.add(cluster_id)
        graph.node(
            cluster_id,
            "cluster",
            cluster_name,
            platform,
            properties={"cluster_name": cluster_name},
            provenance=config_source,
        )
        graph.edge(platform_id, cluster_id, "contains_cluster", provenance=config_source)

        raw_nodes = raw.get("nodes") or {}
        if not isinstance(raw_nodes, dict):
            raise TopologyError(f"nodes 必须是 mapping: {config_path}")
        for raw_name, raw_info in sorted(raw_nodes.items(), key=lambda item: str(item[0])):
            node_name = str(raw_name).strip()
            if not node_name or not isinstance(raw_info, dict):
                warn(f"跳过无效节点条目: {config_path}:{raw_name}")
                continue
            host = str(raw_info.get("host") or node_name).strip()
            components = sorted({str(item).strip() for item in raw_info.get("components") or [] if str(item).strip()})
            node_id = _node_id(platform, node_name)
            node_source = _source(
                "platform_config", config_path.relative_to(config_root.parent).as_posix(), collected_at
            )
            node_properties = {"host": host, "components": components}
            for key in ("node_type", "role", "cluster_group", "status", "notes", "aliases"):
                if raw_info.get(key) not in (None, ""):
                    node_properties[key] = raw_info[key]
            graph.node(
                node_id,
                "node",
                node_name,
                platform,
                properties=node_properties,
                provenance=node_source,
            )
            graph.edge(cluster_id, node_id, "contains_node", provenance=node_source)
            lookup_names = {node_name.lower(), host.lower()}
            if "." in node_name:
                lookup_names.add(node_name.split(".", 1)[0].lower())
            if "." in host:
                lookup_names.add(host.split(".", 1)[0].lower())
            for alias in raw_info.get("aliases") or []:
                alias_text = str(alias).strip()
                if alias_text:
                    lookup_names.add(alias_text.lower())
            for lookup in lookup_names:
                host_lookup[(platform, lookup)] = node_id
            for component in components:
                component_id = f"component:{platform}:{component}"
                graph.node(
                    component_id,
                    "component",
                    component,
                    platform,
                    properties={"component_name": component},
                    provenance=node_source,
                )
                graph.edge(
                    node_id, component_id, "hosts_component", provenance=node_source
                )
                service = component_service(component)
                if service is None:
                    unmapped_components.add((platform, component))
                    warn(
                        f"组件尚未映射到服务: {platform}/{component}",
                        f"component:{platform}:{component}",
                    )
                    continue
                service_id = ensure_service(platform, service, node_source)
                graph.edge(
                    component_id, service_id, "component_of", provenance=node_source
                )

        endpoints = raw.get("endpoints") or {}
        if not isinstance(endpoints, dict):
            raise TopologyError(f"endpoints 必须是 mapping: {config_path}")
        for raw_name, raw_entry in sorted(endpoints.items(), key=lambda item: str(item[0])):
            endpoint_name = str(raw_name).strip()
            if not endpoint_name:
                continue
            endpoint_id = f"endpoint:{platform}:{endpoint_name}"
            host, port, properties = _endpoint_parts(raw_entry)
            if port is not None:
                properties["port"] = port
            if host:
                properties["host"] = host
            if isinstance(raw_entry, dict):
                if raw_entry.get("source"):
                    properties["source"] = raw_entry["source"]
                if raw_entry.get("collected_at"):
                    properties["collected_at"] = raw_entry["collected_at"]
            graph.node(
                endpoint_id,
                "endpoint",
                endpoint_name,
                platform,
                properties=properties,
                provenance=config_source,
            )
            graph.edge(
                cluster_id, endpoint_id, "contains_endpoint", provenance=config_source
            )
            service = _infer_endpoint_service(endpoint_name)
            if service:
                graph.edge(
                    ensure_service(platform, service, config_source),
                    endpoint_id,
                    "exposes_endpoint",
                    provenance=config_source,
                )
            if host:
                target_node = host_lookup.get((platform, host.lower()))
                if target_node is None and "." in host:
                    target_node = host_lookup.get(
                        (platform, host.split(".", 1)[0].lower())
                    )
                if target_node:
                    graph.edge(
                        endpoint_id,
                        target_node,
                        "served_by",
                        provenance=config_source,
                    )

    # 集群拓扑快照由环境包的采集器离线采集(管理面 API → 主机/组件矩阵与脱敏的配置
    # 依赖证据,不含口令);在入库主机清单之后合并,让快照刷新事实而知识库构建不依赖网络。
    if snapshot_root is not None:
        snapshot_root = Path(snapshot_root).resolve()
        pack = active_env_pack()
        for platform in knowledge_clusters():
            declared = pack.knowledge_source_path(platform, "topology")
            if declared is None:
                continue
            snapshot_path = snapshot_root / declared.name
            if not snapshot_path.is_file():
                continue
            source_paths.append(snapshot_path)
            try:
                snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                warn(f"拓扑快照无法解析，未接入: {snapshot_path}: {exc}", str(snapshot_path))
                continue
            if (
                not isinstance(snapshot, dict)
                or snapshot.get("schema_version") != 3
                or snapshot.get("platform") != platform
            ):
                warn(f"拓扑快照版本或平台不兼容(须 schema_version 3)，未接入: {snapshot_path}", str(snapshot_path))
                continue

            collected_at = snapshot.get("collected_at")
            snapshot_source = _source(
                "topology_snapshot", f"{snapshot_root.parent.name}/{snapshot_root.name}/{snapshot_path.name}",
                collected_at,
            )
            cluster_name = str(snapshot.get("cluster_name") or platform)
            cluster_id = f"cluster:{platform}:{cluster_name}"
            cluster_nodes[platform] = cluster_id
            cluster_ids.add(cluster_id)
            platform_id = ensure_platform(platform, snapshot_source)
            graph.node(
                cluster_id,
                "cluster",
                cluster_name,
                platform,
                properties={"cluster_name": cluster_name},
                provenance=snapshot_source,
            )
            graph.edge(platform_id, cluster_id, "contains_cluster", provenance=snapshot_source)

            raw_nodes = snapshot.get("nodes") or {}
            if not isinstance(raw_nodes, dict):
                warn(f"拓扑快照 nodes 不是 mapping: {snapshot_path}", str(snapshot_path))
                raw_nodes = {}
            for raw_name, raw_info in sorted(raw_nodes.items(), key=lambda item: str(item[0])):
                if not isinstance(raw_info, dict):
                    continue
                node_name = str(raw_name).strip()
                if not node_name:
                    continue
                host = str(raw_info.get("host") or node_name).strip()
                fqdn = str(raw_info.get("fqdn") or "").strip()
                components = sorted({
                    str(item).strip()
                    for item in raw_info.get("components") or []
                    if str(item).strip()
                })
                node_id = _node_id(platform, node_name)
                graph.node(
                    node_id,
                    "node",
                    node_name,
                    platform,
                    properties={"host": host, "fqdn": fqdn, "components": components},
                    provenance=snapshot_source,
                )
                graph.edge(cluster_id, node_id, "contains_node", provenance=snapshot_source)
                for lookup in {node_name.lower(), host.lower(), fqdn.lower()} - {""}:
                    host_lookup[(platform, lookup)] = node_id
                for component in components:
                    component_id = f"component:{platform}:{component}"
                    graph.node(
                        component_id,
                        "component",
                        component,
                        platform,
                        properties={"component_name": component},
                        provenance=snapshot_source,
                    )
                    graph.edge(node_id, component_id, "hosts_component", provenance=snapshot_source)
                    service = component_service(component)
                    if service is None:
                        unmapped_components.add((platform, component))
                        warn(
                            f"组件尚未映射到服务: {platform}/{component}",
                            f"component:{platform}:{component}",
                        )
                        continue
                    service_id = ensure_service(platform, service, snapshot_source)
                    graph.edge(component_id, service_id, "component_of", provenance=snapshot_source)

            raw_services = snapshot.get("services") or []
            if not isinstance(raw_services, list):
                raw_services = []
            declared_services: set[str] = set()
            preexisting_service_ids = {
                node_id for node_id, node in graph.nodes.items()
                if node.get("kind") == "service" and node.get("platform") == platform
            }
            for raw_service in raw_services:
                if not isinstance(raw_service, dict):
                    continue
                service = str(raw_service.get("service") or "").strip()
                if not service:
                    native_service = str(raw_service.get("native_service") or "").strip()
                    warn(
                        f"快照服务未映射到知识库词表: {platform}/{native_service}",
                        f"native-service:{platform}:{native_service}",
                    )
                    continue
                declared_services.add(service)
                ensure_service(platform, service, snapshot_source)

            raw_relations = snapshot.get("config_relations") or []
            if not isinstance(raw_relations, list):
                raw_relations = []
            for raw_relation in raw_relations:
                if not isinstance(raw_relation, dict):
                    continue
                source_service = str(raw_relation.get("source_service") or "").strip()
                target_service = str(raw_relation.get("target_service") or "").strip()
                relation = str(raw_relation.get("relation") or "").strip()
                if not source_service or not target_service or not relation:
                    continue
                source_id = ensure_service(platform, source_service, snapshot_source)
                target_id = ensure_service(platform, target_service, snapshot_source)
                if (
                    target_service not in declared_services
                    and target_id not in preexisting_service_ids
                ):
                    graph.node(
                        target_id,
                        "service",
                        target_service,
                        platform,
                        properties={
                            "external_reference": True,
                            "reference_only": True,
                        },
                        provenance=snapshot_source,
                    )
                    warn(
                        f"配置引用服务未出现在快照服务清单: {platform}/{target_service}",
                        f"external-service:{platform}:{target_service}",
                    )
                properties = raw_relation.get("properties")
                if not isinstance(properties, dict):
                    properties = {}
                provenance = raw_relation.get("provenance")
                if not isinstance(provenance, list) or not provenance:
                    provenance = [snapshot_source]
                for evidence in provenance:
                    if isinstance(evidence, dict):
                        graph.edge(
                            source_id,
                            target_id,
                            relation,
                            properties=properties,
                            provenance=evidence,
                        )

            # v2 快照:栈声明的服务/组件依赖与组件端口,与配置推断、文档声明的同名边合并来源
            for raw_relation in snapshot.get("stack_dependencies") or []:
                if not isinstance(raw_relation, dict):
                    continue
                source_service = str(raw_relation.get("source_service") or "").strip()
                target_service = str(raw_relation.get("target_service") or "").strip()
                if not source_service or not target_service:
                    continue
                source_id = ensure_service(platform, source_service, snapshot_source)
                target_id = ensure_service(platform, target_service, snapshot_source)
                for evidence in raw_relation.get("provenance") or [snapshot_source]:
                    graph.edge(
                        source_id, target_id, "depends_on",
                        properties=raw_relation.get("properties") or {},
                        provenance=evidence,
                    )
            for raw_relation in snapshot.get("component_dependencies") or []:
                if not isinstance(raw_relation, dict):
                    continue
                source = str(raw_relation.get("source_component") or "").strip()
                target = str(raw_relation.get("target_component") or "").strip()
                source_id = f"component:{platform}:{source}"
                target_id = f"component:{platform}:{target}"
                if not source or not target or source_id not in graph.nodes or target_id not in graph.nodes:
                    continue
                for evidence in raw_relation.get("provenance") or [snapshot_source]:
                    graph.edge(
                        source_id, target_id, "depends_on",
                        properties=raw_relation.get("properties") or {},
                        provenance=evidence,
                    )
            ports_by_component: dict[str, list[dict[str, Any]]] = {}
            for raw_port in snapshot.get("component_ports") or []:
                if not isinstance(raw_port, dict) or not raw_port.get("component"):
                    continue
                ports_by_component.setdefault(str(raw_port["component"]), []).append({
                    "port": raw_port.get("port"),
                    "purpose": raw_port.get("purpose"),
                    "provenance": raw_port.get("provenance") or [],
                })
            for component, ports in ports_by_component.items():
                component_id = f"component:{platform}:{component}"
                if component_id in graph.nodes:
                    graph.nodes[component_id]["properties"]["ports"] = sorted(
                        ports, key=lambda item: (int(item.get("port") or 0), str(item.get("purpose") or "")),
                    )

    # Read every KB Markdown file once.  Index files are represented as
    # `index` nodes, while normal documents remain `document` nodes.
    docs: list[tuple[Path, Any]] = []
    if knowledge_root.is_dir():
        for item in iter_documents(knowledge_root):
            path = item.path
            try:
                doc = load_doc(path)
            except FrontmatterError as exc:
                warn(f"知识文档无法解析，未接入拓扑: {path}: {exc}", str(path))
                continue
            docs.append((path, doc))
            source_paths.append(path)

    doc_ids: dict[str, str] = {}
    relative_paths: dict[str, str] = {}
    stems: dict[str, list[str]] = {}
    for path, doc in docs:
        relative = path.relative_to(knowledge_root).as_posix()
        service = _index_service(path, knowledge_root, doc)
        relative_paths[relative] = relative
        relative_paths[relative[:-3]] = relative
        stems.setdefault(path.stem, []).append(relative)
        kind = "index" if doc.is_index else "document"
        node_id = f"{kind}:{relative}"
        doc_ids[relative] = node_id
        properties: dict[str, Any] = {
            "path": relative,
            "title": doc.title,
            "type": doc.type if not doc.is_index else "index",
            "service": service,
            "problem": doc.problem,
            "status": doc.status,
            "symptoms": list(doc.symptoms),
            "alerts": list(doc.alerts),
            "playbook": doc.playbook,
            "playbooks": list(doc.playbooks),
            "diagnostic_paths": list(doc.diagnostic_paths),
            "knowledge_layer": doc.knowledge_layer,
            "entrypoints": list(doc.entrypoints),
            "profile": doc.profile,
            "execution_scope": doc.execution_scope,
            "confidence": doc.confidence,
            "observed_at": doc.observed_at,
            "valid_until": doc.valid_until,
        }
        graph.node(
            node_id,
            kind,
            str(doc.title or path.stem),
            doc.platform,
            properties=properties,
            provenance=_source("knowledge", relative),
        )
        if doc.platform and service:
            service_id = ensure_service(doc.platform, service, _source("knowledge", relative))
            graph.edge(
                node_id,
                service_id,
                "documents",
                provenance=_source("knowledge", relative),
            )
            if not doc.is_index and doc.type == "knowledge" and doc.problem == "baseline":
                baseline = _parse_baseline(path)
                graph.node(
                    service_id,
                    "service",
                    service,
                    doc.platform,
                    properties={
                        "baseline_documents": [relative],
                        "baseline_configs": baseline["configs"],
                    },
                    provenance=_source("knowledge", relative),
                )
                for component, facts in baseline["components"].items():
                    component_id = f"component:{doc.platform}:{component}"
                    graph.node(
                        component_id,
                        "component",
                        component,
                        doc.platform,
                        properties=facts,
                        provenance=_source("knowledge", relative),
                    )
                    graph.edge(component_id, service_id, "component_of")
                    for raw_node in facts["baseline_nodes"]:
                        node_id = host_lookup.get((doc.platform, raw_node.lower()))
                        if node_id is None and "." in raw_node:
                            node_id = host_lookup.get(
                                (doc.platform, raw_node.split(".", 1)[0].lower())
                            )
                        if node_id:
                            graph.edge(
                                node_id,
                                component_id,
                                "hosts_component",
                                provenance=_source("knowledge", relative),
                            )
                    for state in facts["observed_state"]:
                        raw_node = str(state.get("node") or "")
                        node_id = host_lookup.get((doc.platform, raw_node.lower()))
                        if node_id is None and "." in raw_node:
                            node_id = host_lookup.get(
                                (doc.platform, raw_node.split(".", 1)[0].lower())
                            )
                        if node_id:
                            graph.edge(
                                node_id,
                                component_id,
                                "hosts_component",
                                properties={"observation": "baseline_status"},
                                provenance=_source("knowledge", relative),
                            )
        for alert in doc.alerts:
            alert_name = str(alert).strip()
            if not alert_name:
                continue
            alert_id = f"alert:{doc.platform}:{alert_name}"
            graph.node(
                alert_id,
                "alert",
                alert_name,
                doc.platform,
                properties={"alertname": alert_name},
                provenance=_source("knowledge", relative),
            )
            graph.edge(
                node_id,
                alert_id,
                "handles_alert",
                provenance=_source("knowledge", relative),
            )
        for playbook_ref in doc.playbooks:
            playbook_path = str(playbook_ref.get("path") or "").strip()
            if not playbook_path:
                continue
            playbook_id = f"playbook:{playbook_path}"
            graph.node(
                playbook_id,
                "playbook",
                playbook_path,
                doc.platform,
                properties={"path": playbook_path, **{
                    key: value for key, value in playbook_ref.items()
                    if key != "path"
                }},
                provenance=_source("knowledge", relative),
            )
            graph.edge(
                node_id,
                playbook_id,
                "uses_playbook",
                provenance=_source("knowledge", relative),
            )
        if doc.relations and doc.platform and service:
            for source, target, relation, properties, provenance in _document_relations(
                doc,
                platform=doc.platform,
                service=service,
                source_path=relative,
            ):
                for ref in (source, target):
                    parts = ref.split(":", 2)
                    if len(parts) == 3 and parts[0] == "service":
                        ensure_service(parts[1], parts[2], provenance)
                graph.edge(
                    source,
                    target,
                    relation,
                    properties=properties,
                    provenance=provenance,
                )

    for path, doc in docs:
        if doc.is_index or not doc.raw.get("related"):
            continue
        source_relative = path.relative_to(knowledge_root).as_posix()
        source_id = doc_ids[source_relative]
        related = doc.raw.get("related")
        if not isinstance(related, list):
            related = [related]
        for raw_target in related:
            target = _normalise_related(raw_target)
            if target.endswith(".md"):
                candidate = relative_paths.get(target)
            else:
                candidate = relative_paths.get(target)
            if candidate is None and target in stems and len(stems[target]) == 1:
                candidate = stems[target][0]
            if candidate is None:
                warn(f"related 目标未解析: {source_relative} -> {target}", f"related:{source_relative}:{target}")
                continue
            graph.edge(
                source_id,
                doc_ids[candidate],
                "related_to",
                properties={"raw_target": str(raw_target)},
                provenance=_source("knowledge", source_relative),
            )

    # 采集不到的服务依赖只认 config/topology/manual-dependencies.yaml 的人工登记,
    # 不再从服务概览正文解析:正文表格会与采集结果各说各话(见 _index lint)。
    manual_path = config_root / "topology" / "manual-dependencies.yaml"
    if manual_path.is_file():
        source_paths.append(manual_path)
        manual_source = _source("manual", f"config/topology/{manual_path.name}")
        manual = yaml.safe_load(manual_path.read_text(encoding="utf-8")) or {}
        for platform, items in manual.items():
            for item in items or []:
                source = str((item or {}).get("source") or "").strip()
                target = str((item or {}).get("target") or "").strip()
                if not source or not target:
                    warn(f"人工依赖登记缺 source/target: {platform}: {item}", f"manual:{platform}:{item}")
                    continue
                graph.edge(
                    ensure_service(str(platform), source, manual_source),
                    ensure_service(str(platform), target, manual_source),
                    "depends_on",
                    properties={"confidence": "manual", "purpose": str(item.get("purpose") or "")},
                    provenance=manual_source,
                )

    nodes = []
    for node_id in sorted(graph.nodes):
        node = graph.nodes[node_id]
        node["provenance"] = sorted(
            node["provenance"], key=lambda item: (item.get("path", ""), item.get("kind", ""))
        )
        nodes.append(node)
    edges = []
    for index, key in enumerate(sorted(graph.edges), start=1):
        edge = graph.edges[key]
        edge = {"id": f"edge-{index:05d}", **edge}
        edge["provenance"] = sorted(
            edge["provenance"], key=lambda item: (item.get("path", ""), item.get("kind", ""))
        )
        edges.append(edge)

    node_counts: dict[str, int] = {}
    for node in nodes:
        node_counts[node["kind"]] = node_counts.get(node["kind"], 0) + 1
    edge_counts: dict[str, int] = {}
    for edge in edges:
        edge_counts[edge["relation"]] = edge_counts.get(edge["relation"], 0) + 1

    dependency_edges = [
        edge for edge in edges
        if edge["relation"] == "depends_on"
        and edge["source"].startswith("service:") and edge["target"].startswith("service:")
    ]
    component_dependency_edges = [
        edge for edge in edges
        if edge["relation"] == "depends_on" and edge["source"].startswith("component:")
    ]
    config_evidence_edges = [
        edge for edge in edges
        if any(item.get("kind") == "snapshot_config" for item in edge["provenance"])
    ]
    config_relation_counts: dict[str, int] = {}
    for edge in config_evidence_edges:
        relation = edge["relation"]
        config_relation_counts[relation] = config_relation_counts.get(relation, 0) + 1
    platforms = graph_platforms()
    operational_services = [
        node for node in nodes
        if node["kind"] == "service" and node["platform"] in platforms
    ]
    dependency_service_ids = {
        service_id
        for edge in dependency_edges
        for service_id in (edge["source"], edge["target"])
    }
    dependency_gaps = sorted(
        f"{node['platform']}/{node['name']}"
        for node in operational_services
        if node["id"] not in dependency_service_ids
    )
    baseline_service_ids = {
        node["id"] for node in operational_services
        if node["properties"].get("baseline_documents")
    }
    config_component_ids = {
        edge["target"]
        for edge in edges
        if edge["relation"] == "hosts_component"
        and any(item.get("kind") == "platform_config" for item in edge["provenance"])
    }
    component_ids = {
        node["id"] for node in nodes if node["kind"] == "component"
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "source_fingerprint": _fingerprint(source_paths),
        "nodes": nodes,
        "edges": edges,
        "warnings": sorted(warnings),
        "coverage": {
            "component_service_mapping": {
                "mapped": len({
                    edge["source"] for edge in edges
                    if edge["relation"] == "component_of"
                }),
                "unmapped": [
                    f"{platform}/{component}"
                    for platform, component in sorted(unmapped_components)
                ],
            },
            "component_observation": {
                "component_entities": len(component_ids),
                "observed_in_platform_config": len(config_component_ids),
                "baseline_only": sorted(
                    node["name"] for node in nodes
                    if node["kind"] == "component" and node["id"] not in config_component_ids
                ),
            },
            "service_baseline": {
                "operational_service_count": len(operational_services),
                "with_baseline": len(baseline_service_ids),
                "without_baseline": sorted(
                    f"{node['platform']}/{node['name']}"
                    for node in operational_services
                    if node["id"] not in baseline_service_ids
                ),
            },
            "service_dependencies": {
                "documented_edge_count": len(dependency_edges),
                "config_declared_edge_count": sum(
                    1 for edge in dependency_edges
                    if any(item.get("kind") == "snapshot_config" for item in edge["provenance"])
                ),
                "stack_declared_edge_count": sum(
                    1 for edge in dependency_edges
                    if any(item.get("kind") == "snapshot_stack" for item in edge["provenance"])
                ),
                "manual_declared_edge_count": sum(
                    1 for edge in dependency_edges
                    if any(item.get("kind") == "manual" for item in edge["provenance"])
                ),
                "component_dependency_edge_count": len(component_dependency_edges),
                "components_with_ports": sum(
                    1 for node in nodes if node["kind"] == "component" and node["properties"].get("ports")
                ),
                "config_relation_counts": dict(sorted(config_relation_counts.items())),
                "services_with_evidence": sorted(
                    f"{node['platform']}/{node['name']}"
                    for node in operational_services
                    if node["id"] in dependency_service_ids
                ),
                "services_without_evidence": dependency_gaps,
            },
        },
        "stats": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "node_kinds": dict(sorted(node_counts.items())),
            "edge_relations": dict(sorted(edge_counts.items())),
            "platforms": sorted(platform_nodes),
            "clusters": len(cluster_ids),
            "services": len(service_platforms),
            "warning_count": len(warnings),
        },
    }


def write_topology(graph: dict[str, Any], output: Path) -> Path:
    """Atomically write a topology graph artifact."""
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(fd)
    temporary = Path(raw_temp)
    try:
        temporary.write_text(
            json.dumps(graph, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def build_topology_artifact(repo_root: Path, output: Optional[Path] = None) -> Path:
    """Build `build/kb-topology.json` for a repository."""
    json_output, _ = build_topology_artifacts(repo_root, output=output)
    return json_output


def build_topology_artifacts(
    repo_root: Path,
    *,
    output: Optional[Path] = None,
    html_output: Optional[Path] = None,
    snapshot_root: Optional[Path] = None,
) -> tuple[Path, Path]:
    """Build the JSON graph and its standalone HTML viewer."""
    repo_root = Path(repo_root).resolve()
    config_dir = load_env_pack(repo_root).config_dir
    snapshot_root = (
        Path(snapshot_root).resolve()
        if snapshot_root is not None
        else config_dir / "topology"
    )
    graph = build_topology(
        config_dir,
        resolve_knowledge_root(repo_root),
        snapshot_root if snapshot_root.is_dir() else None,
    )
    build_dir = resolve_data_root(repo_root) / "build"
    json_path = write_topology(graph, output or build_dir / "kb-topology.json")
    from .topology_view import write_topology_html

    html_path = write_topology_html(graph, html_output or build_dir / "kb-topology.html")
    return json_path, html_path
