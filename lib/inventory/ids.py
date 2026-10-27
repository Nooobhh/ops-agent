"""资源 ID:``<env>[/<cluster>(/<kind>/<name>)*]``。

env、cluster 两级不写种类词;其下每段是「已注册种类 + 名称」。规范化(按种类
统一大小写)后 ID 大小写敏感且确定性:同名必同 ID,不需要 ID 分配表。种类可注册
扩展(如二阶段的容器编排资源),ID 语法与查询不变。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Literal, Optional, Tuple

ENV_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
CLUSTER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

# env/cluster 是 ID 前两级的固定层级,不能当作路径种类注册
LEVEL_KINDS = ("env", "cluster")

Case = Literal["keep", "lower", "upper"]


class InvalidResourceRef(ValueError):
    pass


@dataclass(frozen=True)
class KindSpec:
    name: str
    parents: Tuple[str, ...]
    case: Case = "keep"

    def normalize(self, name: str) -> str:
        if self.case == "lower":
            return name.lower()
        if self.case == "upper":
            return name.upper()
        return name


KINDS: Dict[str, KindSpec] = {}


def register_kind(name: str, parents: Tuple[str, ...], *, case: Case = "keep") -> KindSpec:
    """登记一种路径资源;已登记且定义一致时幂等,定义冲突显式失败。"""
    if not KIND_RE.match(name) or name in LEVEL_KINDS:
        raise ValueError(f"非法资源种类名: {name}")
    for parent in parents:
        if parent not in LEVEL_KINDS and parent not in KINDS and parent != name:
            raise ValueError(f"资源种类 {name} 的父种类未登记: {parent}")
    spec = KindSpec(name, tuple(parents), case)
    existing = KINDS.get(name)
    if existing is not None and existing != spec:
        raise ValueError(f"资源种类 {name} 已登记为不同定义")
    KINDS[name] = spec
    return spec


# 核心种类:主机挂集群,服务挂集群,组件挂服务,实例(组件@主机)挂主机
register_kind("host", ("cluster",), case="lower")
register_kind("service", ("cluster",), case="lower")
register_kind("component", ("service",))
register_kind("instance", ("host",))


@dataclass(frozen=True)
class ResourceRef:
    env: str
    cluster: Optional[str] = None
    path: Tuple[Tuple[str, str], ...] = ()

    @property
    def kind(self) -> str:
        if self.cluster is None:
            return "env"
        if not self.path:
            return "cluster"
        return self.path[-1][0]

    @property
    def name(self) -> str:
        if self.cluster is None:
            return self.env
        if not self.path:
            return self.cluster
        return self.path[-1][1]

    def parent(self) -> Optional["ResourceRef"]:
        if self.cluster is None:
            return None
        if not self.path:
            return ResourceRef(self.env)
        return ResourceRef(self.env, self.cluster, self.path[:-1])

    def cluster_ref(self) -> Optional["ResourceRef"]:
        return None if self.cluster is None else ResourceRef(self.env, self.cluster)

    def child(self, kind: str, name: str) -> "ResourceRef":
        if self.cluster is None:
            raise InvalidResourceRef("环境之下只能是集群,用 with_cluster()")
        spec = _kind(kind)
        if self.kind not in spec.parents:
            raise InvalidResourceRef(f"{kind} 不能挂在 {self.kind} 之下")
        canonical = spec.normalize(str(name))
        if not NAME_RE.match(canonical):
            raise InvalidResourceRef(f"非法资源名: {name!r}")
        return ResourceRef(self.env, self.cluster, self.path + ((kind, canonical),))

    def with_cluster(self, cluster: str) -> "ResourceRef":
        if self.cluster is not None:
            raise InvalidResourceRef("只有环境引用可以派生集群")
        if not CLUSTER_RE.match(cluster):
            raise InvalidResourceRef(f"非法集群 ID: {cluster!r}")
        return ResourceRef(self.env, cluster)

    def __str__(self) -> str:
        parts = [self.env]
        if self.cluster is not None:
            parts.append(self.cluster)
        for kind, name in self.path:
            parts.extend((kind, name))
        return "/".join(parts)


def env_ref(env: str) -> ResourceRef:
    if not ENV_RE.match(str(env)):
        raise InvalidResourceRef(f"非法环境 ID: {env!r}")
    return ResourceRef(str(env))


def parse_ref(text: str) -> ResourceRef:
    """解析并规范化资源 ID;语法、种类或父子关系不合法一律抛 InvalidResourceRef。"""
    raw = str(text or "").strip()
    parts = raw.split("/") if raw else []
    if not parts or any(not part for part in parts):
        raise InvalidResourceRef(f"非法资源 ID: {text!r}")
    ref = env_ref(parts[0])
    if len(parts) == 1:
        return ref
    ref = ref.with_cluster(parts[1])
    rest = parts[2:]
    if len(rest) % 2:
        raise InvalidResourceRef(f"资源 ID 的种类与名称须成对出现: {text!r}")
    for index in range(0, len(rest), 2):
        ref = ref.child(rest[index], rest[index + 1])
    return ref


def _kind(kind: str) -> KindSpec:
    spec = KINDS.get(kind)
    if spec is None:
        raise InvalidResourceRef(f"未登记的资源种类: {kind}")
    return spec
