"""平台/本地开发配置加载。平台配置无凭证可入库;local-dev(含跳板)gitignore。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from lib.data_root import local_config_path, resolve_data_root


@dataclass
class NodeInfo:
    host: str
    components: List[str] = field(default_factory=list)
    aliases: List[str] = field(default_factory=list)


@dataclass
class PlatformConfig:
    platform: str
    nodes: Dict[str, NodeInfo]
    endpoints: dict = field(default_factory=dict)
    collected_at: Optional[str] = None


@dataclass
class JumpConfig:
    host: str
    user: str
    key_file: Optional[str] = None
    port: int = 22


@dataclass
class LocalDev:
    jump: Optional[JumpConfig]
    credentials_file: Path


def load_platform(path: Path) -> PlatformConfig:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    nodes = {
        name: NodeInfo(
            host=str(n["host"]),
            components=list(n.get("components", [])),
            aliases=[str(item) for item in n.get("aliases", []) if str(item).strip()],
        )
        for name, n in (data.get("nodes") or {}).items()
    }
    return PlatformConfig(
        platform=data["platform"],
        nodes=nodes,
        endpoints=dict(data.get("endpoints") or {}),
        collected_at=data.get("collected_at"),
    )


def load_local_dev(path: Path) -> Optional[LocalDev]:
    path = Path(path)
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    jump = None
    if data.get("jump"):
        j = data["jump"]
        jump = JumpConfig(
            host=str(j["host"]), user=str(j["user"]),
            key_file=str(Path(j["key_file"]).expanduser()) if j.get("key_file") else None,
            port=int(j.get("port", 22)),
        )
    return LocalDev(jump=jump, credentials_file=Path(data["credentials_file"]))


def resolve_credentials_path(repo_root: Path) -> Path:
    """Resolve the same dynamic SSH cache path used by local pool builders.

    local-dev.yaml 与 credentials.json 都是本机私有配置,随数据根走;local-dev 里的
    相对凭证路径同样按数据根解析。
    """
    local_dev = load_local_dev(local_config_path(repo_root, "local-dev.yaml"))
    if local_dev is None:
        return local_config_path(repo_root, "credentials.json")
    path = local_dev.credentials_file.expanduser()
    return path if path.is_absolute() else resolve_data_root(repo_root) / path
