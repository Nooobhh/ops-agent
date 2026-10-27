"""旧 (platform, node) → 资源 ID 的只读派生;清单不可用时一律返回 None,不拖垮页面。"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable, Optional

from lib.inventory import load_inventory

HostRef = Callable[[Optional[str], Optional[str]], Optional[str]]


def host_ref_resolver(repo_root: Path) -> HostRef:
    try:
        inventory = load_inventory(repo_root)
    except (OSError, ValueError):
        return lambda _platform, _node: None
    return inventory.host_ref


def attach_host_refs(rows: Iterable[dict], resolve: HostRef, *, node_field: str = "node") -> None:
    for row in rows:
        if isinstance(row, dict):
            row["host_ref"] = resolve(row.get("platform"), row.get(node_field))
