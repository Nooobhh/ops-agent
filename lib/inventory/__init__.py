"""资源模型(清单):环境 → 集群 → 主机 → 服务 → 组件 → 实例 及其关系。

核心只定义资源种类、ID 语法、查询与别名归一,不出现任何具体环境的名字;
清单内容由环境包声明的来源生成。
"""
from __future__ import annotations

from .ids import (
    KINDS, InvalidResourceRef, KindSpec, ResourceRef, env_ref, parse_ref, register_kind,
)
from .loader import load_inventory
from .model import (
    Inventory, InventorySourceError, Provenance, Relation, Resource, UnknownCluster,
)

__all__ = [
    "KINDS", "InvalidResourceRef", "Inventory", "InventorySourceError", "KindSpec",
    "Provenance", "Relation", "Resource", "ResourceRef", "UnknownCluster", "env_ref",
    "load_inventory", "parse_ref", "register_kind",
]
