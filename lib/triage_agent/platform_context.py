from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

import yaml

from lib.envpack import load_env_pack
from lib.triage_agent.targets import target_config_path


class PlatformContextError(ValueError):
    pass


@dataclass(frozen=True)
class PlatformContext:
    _platform: str
    _endpoints: Dict[str, str]
    _cluster: str | None

    @classmethod
    def load(cls, repo_root: Path, platform: str) -> "PlatformContext":
        if platform not in load_env_pack(repo_root).cluster_keys("managed"):
            raise PlatformContextError(f"unsupported platform: {platform}")
        path = target_config_path(Path(repo_root), platform)
        try:
            root = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise PlatformContextError(
                f"platform 配置不可读: {path.name}"
            ) from exc
        if not isinstance(root, dict):
            raise PlatformContextError(f"platform 配置结构无效: {path.name}")

        raw_endpoints = root.get("endpoints") or {}
        if not isinstance(raw_endpoints, dict):
            raise PlatformContextError(f"platform endpoints 结构无效: {path.name}")
        endpoints = {
            name: entry["url"]
            for name, entry in raw_endpoints.items()
            if (
                isinstance(name, str)
                and isinstance(entry, dict)
                and isinstance(entry.get("url"), str)
                and bool(entry["url"].strip())
            )
        }
        cluster = root.get("cluster_name")
        if not isinstance(cluster, str) or not cluster.strip():
            cluster = None
        return cls(platform, endpoints, cluster)

    def endpoint(self, name: str) -> str:
        value = self._endpoints.get(name)
        if value is None:
            raise PlatformContextError(
                f"平台 {self._platform} 未登记可注入 endpoint: {name}"
            )
        return value

    def cluster_name(self) -> str:
        if self._cluster is None:
            raise PlatformContextError(
                f"平台 {self._platform} 未登记 cluster_name"
            )
        return self._cluster

    def endpoint_names(self) -> List[str]:
        return sorted(self._endpoints)
