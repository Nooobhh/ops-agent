"""日志目录清单(config/log-catalog.yaml)加载器。

清单由环境包的日志目录采集器生成(管理面 + 节点探测双通道,
manual 条目人工维护)。诊断注册表用它给 logs.* 动作生成 log_dir 枚举:
枚举值就是登记的绝对路径本身,无 key 间接层。

与 kb-recall 同一失败哲学:清单缺失/损坏显式抛错并提示采集命令,
不做懒构建、不静默降级。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Union

import yaml

_SAFE_PATH_PAT = re.compile(r"/[A-Za-z0-9._/-]*")

__all__ = ["LogCatalog", "LogCatalogError", "LogDirEntry"]

_REBUILD_HINT = (
    "请用环境包的日志目录采集脚本重新生成环境包 config/log-catalog.yaml"
)


class LogCatalogError(ValueError):
    pass


@dataclass(frozen=True)
class LogDirEntry:
    path: str
    service: str
    nodes: Union[str, tuple]   # "all" 或节点名元组
    source: str


class LogCatalog:
    def __init__(self, platforms: Dict[str, List[LogDirEntry]]):
        self._platforms = platforms

    @classmethod
    def load(cls, path: Path) -> "LogCatalog":
        try:
            root = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except OSError as exc:
            raise LogCatalogError(
                f"日志目录清单不可读: {path};{_REBUILD_HINT}"
            ) from exc
        except yaml.YAMLError as exc:
            raise LogCatalogError(
                f"日志目录清单不是合法 YAML: {path};{_REBUILD_HINT}"
            ) from exc
        if not isinstance(root, dict) or root.get("version") != 1:
            raise LogCatalogError(
                f"日志目录清单 version 必须是 1: {path};{_REBUILD_HINT}"
            )
        raw_platforms = root.get("platforms")
        if not isinstance(raw_platforms, dict) or not raw_platforms:
            raise LogCatalogError(
                f"日志目录清单 platforms 为空: {path};{_REBUILD_HINT}"
            )
        platforms: Dict[str, List[LogDirEntry]] = {}
        for platform, tree in raw_platforms.items():
            entries: List[LogDirEntry] = []
            services = (tree or {}).get("services")
            if not isinstance(services, dict):
                raise LogCatalogError(
                    f"平台 {platform} 缺 services 子树;{_REBUILD_HINT}"
                )
            seen = set()
            for service, items in services.items():
                if not isinstance(items, list):
                    raise LogCatalogError(
                        f"服务 {platform}/{service} 条目必须是列表"
                    )
                for item in items:
                    entry = _parse_entry(platform, str(service), item)
                    if entry.path in seen:
                        # 同路径多服务归属按先到先得,不视为致命
                        continue
                    seen.add(entry.path)
                    entries.append(entry)
            platforms[str(platform)] = entries
        return cls(platforms)

    def platforms(self) -> List[str]:
        return sorted(self._platforms)

    def entries(self, platform: str) -> List[LogDirEntry]:
        return list(self._platforms.get(platform) or [])

    def paths(self, platform: str) -> List[str]:
        return sorted(entry.path for entry in self.entries(platform))

    def describe(self, platform: str) -> List[Dict[str, Any]]:
        """给诊断 catalog 的紧凑视图:模型据此选 log_dir 与目标节点。"""
        return [
            {
                "path": entry.path,
                "service": entry.service,
                "nodes": (
                    entry.nodes if isinstance(entry.nodes, str)
                    else list(entry.nodes)
                ),
            }
            for entry in sorted(self.entries(platform), key=lambda e: e.path)
        ]


def _parse_entry(platform: str, service: str, item: Any) -> LogDirEntry:
    if not isinstance(item, dict):
        raise LogCatalogError(f"条目必须是 object: {platform}/{service}")
    path = item.get("path")
    if not isinstance(path, str) or not path.startswith("/") or ".." in path:
        raise LogCatalogError(
            f"条目 path 无效: {platform}/{service}: {path!r}"
        )
    # 路径会作为枚举值原样进入命令模板,字符集必须 shell 安全
    if not _SAFE_PATH_PAT.fullmatch(path):
        raise LogCatalogError(
            f"条目 path 含 shell 不安全字符: {platform}/{service}: {path!r}"
        )
    nodes = item.get("nodes", "all")
    if isinstance(nodes, str):
        if nodes != "all":
            raise LogCatalogError(
                f"条目 nodes 字符串只能是 all: {platform}/{service}/{path}"
            )
        packed: Union[str, tuple] = "all"
    elif isinstance(nodes, list) and all(isinstance(n, str) for n in nodes):
        packed = tuple(nodes)
    else:
        raise LogCatalogError(
            f"条目 nodes 无效: {platform}/{service}/{path}"
        )
    return LogDirEntry(
        path=path.rstrip("/") or "/",
        service=service,
        nodes=packed,
        source=str(item.get("source") or ""),
    )
