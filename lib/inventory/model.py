"""资源清单:资源与关系的只读索引,查询与主机别名归一的唯一实现。

属性约定(来源适配器按此填写,核心只认这些键):
- cluster:``legacy_key``(旧平台短键,落盘数据仍用它)、``roles``、``display_name``
- host:``key``(登记的节点键,旧 node 值)、``address``、``aliases``

旧数据(告警、快照、任务)存 ``(legacy_key, node)``,与资源 ID 一一对应,
经 ``host_ref`` / ``legacy`` 双向换算。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple, Union

from .ids import ResourceRef, parse_ref

SCHEMA_VERSION = 1

# exporter 的 instance 标签形如 192.0.2.1:9104,端口不是主机身份的一部分
_HOST_PORT = re.compile(r"^([^:\s]+):\d{1,5}$")

RefLike = Union[str, ResourceRef]


class InventorySourceError(OSError):
    """某集群的清单来源不可读;访问该集群主机时显式失败,不静默成空集。"""


class UnknownCluster(ValueError):
    pass


@dataclass(frozen=True)
class Provenance:
    source: str
    collected_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"source": self.source, "collected_at": self.collected_at}


@dataclass(frozen=True, eq=False)
class Resource:
    ref: ResourceRef
    attrs: Mapping[str, Any] = field(default_factory=dict)
    provenance: Tuple[Provenance, ...] = ()

    @property
    def id(self) -> str:
        return str(self.ref)

    @property
    def kind(self) -> str:
        return self.ref.kind

    @property
    def name(self) -> str:
        return self.ref.name

    @property
    def parent(self) -> Optional[str]:
        parent = self.ref.parent()
        return None if parent is None else str(parent)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "parent": self.parent,
            "attrs": dict(self.attrs),
            "provenance": [item.to_dict() for item in self.provenance],
        }


@dataclass(frozen=True, eq=False)
class Relation:
    source: str
    type: str
    target: str
    provenance: Tuple[Provenance, ...] = ()
    attrs: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "type": self.type,
            "target": self.target,
            "attrs": dict(self.attrs),
            "provenance": [item.to_dict() for item in self.provenance],
        }


class Inventory:
    def __init__(
        self,
        resources: Iterable[Resource],
        relations: Iterable[Relation] = (),
        *,
        warnings: Iterable[str] = (),
        source_errors: Optional[Mapping[str, str]] = None,
        generated_at: Optional[str] = None,
    ):
        self._by_id: Dict[str, Resource] = {}
        self._children: Dict[str, List[Resource]] = {}
        envs: List[Resource] = []
        for resource in resources:
            if resource.id in self._by_id:
                raise ValueError(f"资源 ID 重复: {resource.id}")
            self._by_id[resource.id] = resource
            if resource.kind == "env":
                envs.append(resource)
        if len(envs) != 1:
            raise ValueError("清单必须恰好包含一个环境")
        self._env = envs[0]
        for resource in self._by_id.values():
            parent = resource.parent
            if parent is None:
                continue
            if parent not in self._by_id:
                raise ValueError(f"资源 {resource.id} 的父资源不存在: {parent}")
            self._children.setdefault(parent, []).append(resource)

        self._clusters: Dict[str, Resource] = {}
        for cluster in self.children(self._env.id, "cluster"):
            key = str(cluster.attrs.get("legacy_key") or cluster.name)
            if key in self._clusters:
                raise ValueError(f"集群旧短键重复: {key}")
            self._clusters[key] = cluster
        self._host_by_key: Dict[Tuple[str, str], Resource] = {
            (cluster.id, str(host.attrs.get("key") or host.name)): host
            for cluster in self._clusters.values()
            for host in self.children(cluster.id, "host")
        }

        self._out: Dict[str, List[Relation]] = {}
        self._in: Dict[str, List[Relation]] = {}
        self._relations: List[Relation] = []
        for relation in relations:
            for end in (relation.source, relation.target):
                if end not in self._by_id:
                    raise ValueError(f"关系端点不存在: {end}")
            self._relations.append(relation)
            self._out.setdefault(relation.source, []).append(relation)
            self._in.setdefault(relation.target, []).append(relation)

        self.warnings: Tuple[str, ...] = tuple(warnings)
        self.source_errors: Dict[str, str] = dict(source_errors or {})
        self.generated_at = generated_at

    # ---- 基本查询 ----

    @property
    def env(self) -> Resource:
        return self._env

    def get(self, ref: RefLike) -> Optional[Resource]:
        return self._by_id.get(str(ref))

    def resources(self, kind: Optional[str] = None) -> List[Resource]:
        return [item for item in self._by_id.values() if kind is None or item.kind == kind]

    def children(self, ref: RefLike, kind: Optional[str] = None) -> List[Resource]:
        return [
            item for item in self._children.get(str(ref), [])
            if kind is None or item.kind == kind
        ]

    def relations(
        self, ref: RefLike, type: Optional[str] = None, direction: str = "out",
    ) -> List[Relation]:
        index = self._out if direction == "out" else self._in
        return [item for item in index.get(str(ref), []) if type is None or item.type == type]

    # ---- 集群(旧平台短键桥) ----

    def clusters(self, role: Optional[str] = None) -> List[Resource]:
        return [
            cluster for cluster in self._clusters.values()
            if role is None or role in (cluster.attrs.get("roles") or ())
        ]

    def cluster_keys(self, role: Optional[str] = None) -> Tuple[str, ...]:
        """按声明顺序返回旧短键;替代代码里写死的平台集合。"""
        return tuple(
            key for key, cluster in self._clusters.items()
            if role is None or role in (cluster.attrs.get("roles") or ())
        )

    def cluster(self, legacy_key: str) -> Optional[Resource]:
        return self._clusters.get(str(legacy_key))

    def require_cluster(self, legacy_key: str) -> Resource:
        cluster = self.cluster(legacy_key)
        if cluster is None:
            raise UnknownCluster(f"未登记的集群: {legacy_key}")
        return cluster

    def hosts(self, legacy_key: str) -> List[Resource]:
        cluster = self.require_cluster(legacy_key)
        self._check_source(cluster)
        return self.children(cluster.id, "host")

    def instances(
        self, *, host: Optional[RefLike] = None, component: Optional[RefLike] = None,
    ) -> List[Resource]:
        if host is not None:
            found = self.children(host, "instance")
        else:
            found = self.resources("instance")
        if component is not None:
            owners = {item.source for item in self.relations(component, "instance_of", "in")}
            found = [item for item in found if item.id in owners]
        return found

    # ---- 主机归一 ----

    def resolve_host(self, raw: Any, legacy_key: Optional[str] = None) -> Optional[Resource]:
        """节点键 / IP / 别名 / FQDN(忽略 :端口)归一到登记主机。

        指定集群:集群内按登记顺序取第一个匹配。不指定:逐集群匹配,唯一命中才返回,
        多集群同名(如各集群共用 nodeN 命名)按歧义返回 None。
        """
        value = _strip_port(str(raw or "").strip())
        if not value:
            return None
        if legacy_key is not None:
            if self.cluster(legacy_key) is None:
                return None
            return _match_host(value, self.hosts(legacy_key))
        matches = [
            host for key in self._clusters
            for host in [_match_host(value, self.hosts(key))]
            if host is not None
        ]
        return matches[0] if len(matches) == 1 else None

    def host_ref(self, legacy_key: Optional[str], node: Optional[str]) -> Optional[str]:
        """旧 ``(platform, node)`` → 主机资源 ID;未登记返回 None,不做模糊归一。"""
        cluster = self.cluster(str(legacy_key)) if legacy_key else None
        if cluster is None or not node:
            return None
        host = self._host_by_key.get((cluster.id, str(node)))
        return None if host is None else host.id

    def cluster_ref(self, legacy_key: Optional[str]) -> Optional[str]:
        cluster = self.cluster(str(legacy_key)) if legacy_key else None
        return None if cluster is None else cluster.id

    def legacy(self, ref: RefLike) -> Tuple[Optional[str], Optional[str]]:
        """资源 ID → 旧 ``(platform, node)``;主机之下的资源带出所在主机。"""
        parsed = ref if isinstance(ref, ResourceRef) else parse_ref(str(ref))
        cluster_ref = parsed.cluster_ref()
        cluster = self.get(cluster_ref) if cluster_ref is not None else None
        if cluster is None:
            return None, None
        key = str(cluster.attrs.get("legacy_key") or cluster.name)
        for index, (kind, _name) in enumerate(parsed.path):
            if kind == "host":
                host_ref = ResourceRef(parsed.env, parsed.cluster, parsed.path[:index + 1])
                host = self.get(host_ref)
                return key, None if host is None else str(host.attrs.get("key") or host.name)
        return key, None

    # ---- 导出 ----

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "env": self._env.id,
            "generated_at": self.generated_at,
            "resources": [item.to_dict() for item in self._by_id.values()],
            "relations": [item.to_dict() for item in self._relations],
            "warnings": list(self.warnings),
            "source_errors": dict(self.source_errors),
        }

    def _check_source(self, cluster: Resource) -> None:
        error = self.source_errors.get(cluster.id)
        if error is not None:
            raise InventorySourceError(f"集群 {cluster.id} 的清单来源不可读: {error}")


def _strip_port(value: str) -> str:
    with_port = _HOST_PORT.match(value)
    return with_port.group(1) if with_port else value


def _match_host(raw: str, hosts: Iterable[Resource]) -> Optional[Resource]:
    folded = raw.casefold()
    folded_short = folded.split(".", 1)[0]
    for host in hosts:
        candidates = [
            host.attrs.get("key") or host.name,
            host.attrs.get("address"),
            *(host.attrs.get("aliases") or ()),
        ]
        for candidate in candidates:
            text = str(candidate or "").strip()
            text_folded = text.casefold()
            text_short = text_folded.split(".", 1)[0]
            if text and (
                text_folded == folded
                or (not text[0].isdigit() and text_short == folded)
                or (not raw[0].isdigit() and text_folded == folded_short)
            ):
                return host
    return None
