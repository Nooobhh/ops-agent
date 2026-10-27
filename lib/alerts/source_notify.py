"""按来源的中台推送开关。

关推送只是不告警:照常轮询、记时间线、分级与去重,自动排查不受影响(由全局开关与规则级静音决定)。
默认值是告警源配置里每个源的 ``notify``(缺省 true);本机切换存 ``state/alert-source-notify.json``,
只经本模块加锁原子写。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Mapping, Optional

import yaml

from lib.atomic_file import atomic_write_json
from lib.file_lock import FileLock
from lib.envpack import load_env_pack


FILE = "alert-source-notify.json"
_WRITE_LOCK = threading.Lock()


# 经插件 feed 取数的核心源全局一个;环境包扩展的告警源按集群各一个(配置须带 platform)
GLOBAL_SOURCE_TYPES = frozenset({"prometheus"})


def event_source_id(source: str, platform: Optional[str]) -> str:
    """告警事件 → 源 id:全局源为类型,按集群的源为「类型:集群」(与 ``source_id`` 一致)。"""
    source = str(source or "")
    return source if source in GLOBAL_SOURCE_TYPES else f"{source}:{platform or 'unknown'}"


def configured_source_ids(config: Mapping) -> list:
    """告警源配置里各源的 id,按声明顺序。"""
    return [
        source_id(name, source_cfg)
        for name, source_cfg in (config.get("sources") or {}).items()
        if isinstance(source_cfg, Mapping)
    ]


def source_id(name: str, source_cfg: Mapping) -> str:
    """告警源配置项 → 源 id(状态、覆盖、时间线里用的键):按集群各一个的源为「类型:集群」,全局源为类型。"""
    source_type = str(source_cfg.get("type") or "")
    if source_cfg.get("platform"):
        return f"{source_type}:{source_cfg['platform']}"
    return source_type or str(name)


def load_source_config(repo_root: Path) -> dict:
    """入库的告警源配置(只读);读不到按无来源处理。"""
    path = load_env_pack(Path(repo_root)).config_file("alert-sources.yaml")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def load_overrides(state_dir: Path) -> dict:
    """本机切换过的来源 → 是否推送;文件缺失或损坏按无覆盖处理。"""
    path = Path(state_dir) / FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        return {}
    sources = data.get("sources") if isinstance(data, dict) else None
    if not isinstance(sources, dict):
        return {}
    return {str(key): value for key, value in sources.items() if isinstance(value, bool)}


def source_notify(config: Mapping, state_dir: Path) -> dict:
    """告警源配置中每个源的推送状态:{源 id: {notify, default, overridden}}。"""
    overrides = load_overrides(state_dir)
    result = {}
    for name, source_cfg in (config.get("sources") or {}).items():
        if not isinstance(source_cfg, Mapping):
            continue
        key = source_id(name, source_cfg)
        default = source_cfg.get("notify") is not False
        result[key] = {
            "notify": overrides.get(key, default),
            "default": default,
            "overridden": key in overrides,
        }
    return result


def set_override(state_dir: Path, key: str, notify: Optional[bool]) -> None:
    """切换一个来源;notify=None 表示恢复默认。"""
    if notify is not None and not isinstance(notify, bool):
        raise ValueError("notify must be a boolean or null")
    state_dir = Path(state_dir)
    with _WRITE_LOCK:
        with FileLock(str(state_dir / "alert-source-notify.lock")):
            sources = load_overrides(state_dir)
            if notify is None:
                sources.pop(key, None)
            else:
                sources[key] = notify
            state_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(state_dir / FILE, {"version": 1, "sources": sources})
