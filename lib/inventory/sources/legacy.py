"""过渡来源适配器:由现有平台配置与集群拓扑快照生成清单(以后可改由插件拓扑提供)。

读的文件全部由环境包声明:
- 集群 ``sources.hosts``:平台/拓扑 yaml(platform、cluster_name、collected_at、endpoints、nodes)
- 集群 ``sources.topology``:集群拓扑快照 json(v3:services、component_ports、各类依赖)
- ``inventory.manual_dependencies``:人工服务依赖(按旧短键分组)

主机来源不可读记为该集群的 source error(访问时显式失败);快照缺失只记 warning。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from lib.config import NodeInfo, PlatformConfig, load_platform
from lib.envpack import ClusterDecl, EnvPack

from ..ids import InvalidResourceRef, ResourceRef, env_ref
from ..model import Inventory, Provenance, Relation, Resource

_HOST_EXTRAS = ("node_type", "role", "status", "cluster_group")
_LOAD_ERRORS = (
    OSError, UnicodeError, yaml.YAMLError, KeyError, TypeError, ValueError, AttributeError,
)


def source_paths(pack: EnvPack) -> List[Path]:
    """全部声明来源的绝对路径(清单缓存按它们的 mtime 失效)。"""
    specs = [spec for cluster in pack.clusters for spec in cluster.sources.values()]
    specs.extend(pack.inventory.values())
    return [pack.resolve(spec) for spec in specs]


def build_inventory(pack: EnvPack) -> Inventory:
    builder = _Builder(pack)
    for cluster in pack.clusters:
        builder.add_cluster(cluster)
    builder.add_manual_dependencies()
    return builder.finish()


def to_platform_config(inventory: Inventory, legacy_key: str) -> PlatformConfig:
    """清单 → 旧 ``PlatformConfig``(与 ``load_platform`` 读同一文件的结果逐字段一致)。"""
    hosts = inventory.hosts(legacy_key)
    cluster = inventory.require_cluster(legacy_key)
    return PlatformConfig(
        platform=str(cluster.attrs.get("source_platform") or legacy_key),
        nodes={
            str(host.attrs["key"]): NodeInfo(
                host=str(host.attrs.get("address") or ""),
                components=list(host.attrs.get("components") or ()),
                aliases=list(host.attrs.get("aliases") or ()),
            )
            for host in hosts
        },
        endpoints=dict(cluster.attrs.get("endpoints") or {}),
        collected_at=cluster.attrs.get("collected_at"),
    )


class _Builder:
    def __init__(self, pack: EnvPack):
        self.pack = pack
        self.env = env_ref(pack.id)
        self.attrs: Dict[str, Dict[str, Any]] = {
            str(self.env): {"display_name": pack.display_name, "timezone": pack.timezone},
        }
        self.refs: Dict[str, ResourceRef] = {str(self.env): self.env}
        self.provenance: Dict[str, Tuple[Provenance, ...]] = {}
        self.relations: Dict[Tuple[str, str, str], Relation] = {}
        self.components: Dict[Tuple[str, str], ResourceRef] = {}
        self.warnings: List[str] = []
        self.source_errors: Dict[str, str] = {}

    # ---- 集群 ----

    def add_cluster(self, decl: ClusterDecl) -> None:
        cref = self.env.with_cluster(decl.id)
        self._add(cref, {
            "legacy_key": decl.legacy_key,
            "display_name": decl.display_name,
            "roles": decl.roles,
            "sources": dict(decl.sources),
        })
        spec = decl.sources.get("hosts")
        if spec:
            try:
                self._add_hosts(cref, spec)
            except (*_LOAD_ERRORS, InvalidResourceRef) as exc:
                self._drop_children(cref)
                self.source_errors[str(cref)] = f"{type(exc).__name__}: {exc}"
                return
        spec = decl.sources.get("topology")
        if spec:
            self._add_topology(cref, spec)
        self._add_instances(cref)

    def _add_hosts(self, cref: ResourceRef, spec: str) -> None:
        path = self.pack.resolve(spec)
        config = load_platform(path)
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        provenance = (Provenance(spec, config.collected_at),)
        cluster_attrs = self.attrs[str(cref)]
        cluster_attrs.update({
            "source_platform": config.platform,
            "native_name": raw.get("cluster_name"),
            "endpoints": dict(config.endpoints),
            "collected_at": config.collected_at,
        })
        self.provenance[str(cref)] = provenance
        raw_nodes = raw.get("nodes") or {}
        for name, info in config.nodes.items():
            href = cref.child("host", str(name))
            if str(href) in self.attrs:
                raise ValueError(f"主机名规范化后重复: {name}")
            extras = raw_nodes.get(name) if isinstance(raw_nodes.get(name), dict) else {}
            attrs = {
                "key": str(name),
                "address": info.host,
                "components": tuple(info.components),
                "aliases": tuple(info.aliases),
            }
            attrs.update({key: extras[key] for key in _HOST_EXTRAS if key in extras})
            self._add(href, attrs, provenance)

    def _add_topology(self, cref: ResourceRef, spec: str) -> None:
        path = self.pack.resolve(spec)
        if not path.is_file():
            self.warnings.append(f"拓扑快照不存在: {spec}")
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeError, ValueError) as exc:
            self.warnings.append(f"拓扑快照不可读: {spec}: {exc}")
            return
        provenance = (Provenance(spec, data.get("collected_at")),)
        for item in data.get("services") or []:
            service = self._ensure_service(cref, str(item.get("service") or ""), provenance)
            if service is None:
                continue
            if item.get("native_service"):
                self.attrs[str(service)]["native_name"] = item["native_service"]
            for component in item.get("components") or {}:
                self._ensure_component(service, str(component), provenance)
        for item in data.get("component_ports") or []:
            component = self._component(cref, str(item.get("component") or ""))
            if component is None:
                self.warnings.append(f"端口登记的组件不在快照服务中: {item.get('component')}")
                continue
            ports = self.attrs[str(component)].setdefault("ports", [])
            entry = {"port": item.get("port"), "purpose": item.get("purpose")}
            if entry not in ports:
                ports.append(entry)
        for key, basis in (("stack_dependencies", "stack"), ("config_relations", "config")):
            for item in data.get(key) or []:
                self._relate(
                    self._service(cref, item.get("source_service")),
                    str(item.get("relation") or "depends_on"),
                    self._service(cref, item.get("target_service")),
                    {"basis": basis, "purpose": (item.get("properties") or {}).get("purpose")},
                    provenance, f"{key}: {item.get('source_service')}→{item.get('target_service')}",
                )
        for item in data.get("component_dependencies") or []:
            self._relate(
                self._component(cref, str(item.get("source_component") or "")),
                str(item.get("relation") or "depends_on"),
                self._component(cref, str(item.get("target_component") or "")),
                {"basis": "stack"},
                provenance,
                f"component_dependencies: {item.get('source_component')}→{item.get('target_component')}",
            )

    def _add_instances(self, cref: ResourceRef) -> None:
        for href in [ref for ref in self.refs.values() if ref.parent() == cref and ref.kind == "host"]:
            attrs = self.attrs[str(href)]
            for component in attrs.get("components") or ():
                iref = href.child("instance", str(component))
                if str(iref) in self.attrs:
                    continue
                self._add(iref, {}, self.provenance.get(str(href), ()))
                target = self._component(cref, str(component))
                if target is None:
                    target = self._fallback_component(cref, str(component))
                if target is None:
                    self.warnings.append(f"未映射组件: {cref.cluster}/{component}")
                    continue
                self._relate(iref, "instance_of", target, {}, (), "")

    def add_manual_dependencies(self) -> None:
        spec = self.pack.inventory.get("manual_dependencies")
        if not spec:
            return
        path = self.pack.resolve(spec)
        if not path.is_file():
            self.warnings.append(f"人工依赖登记不存在: {spec}")
            return
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            self.warnings.append(f"人工依赖登记不可读: {spec}: {exc}")
            return
        provenance = (Provenance(spec),)
        for legacy_key, items in data.items():
            decl = self.pack.cluster(str(legacy_key))
            if decl is None:
                self.warnings.append(f"人工依赖登记了未声明的集群: {legacy_key}")
                continue
            cref = self.env.with_cluster(decl.id)
            for item in items or []:
                self._relate(
                    self._service(cref, item.get("source")),
                    "depends_on",
                    self._service(cref, item.get("target")),
                    {"basis": "manual", "purpose": item.get("purpose")},
                    provenance, f"manual: {legacy_key} {item.get('source')}→{item.get('target')}",
                )

    def finish(self) -> Inventory:
        resources = [
            Resource(ref, self.attrs[key], self.provenance.get(key, ()))
            for key, ref in self.refs.items()
        ]
        return Inventory(
            resources,
            self.relations.values(),
            warnings=self.warnings,
            source_errors=self.source_errors,
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    # ---- 内部 ----

    def _add(self, ref: ResourceRef, attrs: Dict[str, Any],
             provenance: Tuple[Provenance, ...] = ()) -> None:
        key = str(ref)
        self.refs[key] = ref
        self.attrs[key] = attrs
        if provenance:
            self.provenance[key] = provenance

    def _drop_children(self, cref: ResourceRef) -> None:
        prefix = f"{cref}/"
        for key in [key for key in self.refs if key.startswith(prefix)]:
            del self.refs[key]
            del self.attrs[key]
            self.provenance.pop(key, None)
        self.components = {
            key: ref for key, ref in self.components.items() if key[0] != str(cref)
        }

    def _ensure_service(self, cref: ResourceRef, name: str,
                        provenance: Tuple[Provenance, ...]) -> Optional[ResourceRef]:
        try:
            sref = cref.child("service", name)
        except InvalidResourceRef:
            self.warnings.append(f"非法服务名: {cref.cluster}/{name!r}")
            return None
        if str(sref) not in self.attrs:
            self._add(sref, {}, provenance)
        return sref

    def _ensure_component(self, sref: ResourceRef, name: str,
                          provenance: Tuple[Provenance, ...]) -> Optional[ResourceRef]:
        try:
            cref = sref.child("component", name)
        except InvalidResourceRef:
            self.warnings.append(f"非法组件名: {sref}/{name!r}")
            return None
        if str(cref) not in self.attrs:
            self._add(cref, {}, provenance)
        self.components.setdefault((str(sref.cluster_ref()), name), cref)
        return cref

    def _service(self, cref: ResourceRef, name: Any) -> Optional[ResourceRef]:
        try:
            sref = cref.child("service", str(name or ""))
        except InvalidResourceRef:
            return None
        return sref if str(sref) in self.attrs else None

    def _component(self, cref: ResourceRef, name: str) -> Optional[ResourceRef]:
        return self.components.get((str(cref), name))

    def _fallback_component(self, cref: ResourceRef, component: str) -> Optional[ResourceRef]:
        # 无快照的集群(MySQL、独立主机组)按知识库服务词表归类组件;延迟导入避免与 lib.kb 成环
        from lib.kb.topology import component_service

        service = component_service(component)
        if not service:
            return None
        sref = self._ensure_service(cref, service, ())
        return None if sref is None else self._ensure_component(sref, component, ())

    def _relate(self, source: Optional[ResourceRef], type_: str, target: Optional[ResourceRef],
                attrs: Dict[str, Any], provenance: Tuple[Provenance, ...], label: str) -> None:
        if source is None or target is None:
            if label:
                self.warnings.append(f"关系端点不在清单中: {label}")
            return
        if source == target:
            return
        key = (str(source), type_, str(target))
        existing = self.relations.get(key)
        if existing is None:
            self.relations[key] = Relation(
                key[0], type_, key[2], provenance, {k: v for k, v in attrs.items() if v},
            )
            return
        bases = sorted({*str(existing.attrs.get("basis") or "").split(","), str(attrs.get("basis") or "")} - {""})
        merged = dict(existing.attrs)
        if bases:
            merged["basis"] = ",".join(bases)
        self.relations[key] = Relation(
            key[0], type_, key[2], existing.provenance + provenance, merged,
        )
