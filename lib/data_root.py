"""运行时数据根:state/records/build/knowledge 与本机私有配置的唯一定位入口。

``OPS_AGENT_DATA_ROOT`` 未设时数据根就是仓库根(源码开发态零变化);设了则全部运行时
写入落数据根,代码目录只读。入库配置(platform yaml、拓扑快照等)仍随代码走 ``<repo>/config``。
记录根(records)可经 ``paths.local.yaml`` 的 ``records_root`` 放到代码仓外,缺省 ``<数据根>/records``。
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

DATA_ROOT_ENV = "OPS_AGENT_DATA_ROOT"
RECORDS_ROOT_KEY = "records_root"
# 本机私有配置(gitignore):随数据根走,不随代码版本走
LOCAL_CONFIG_NAMES = frozenset({
    "model.local.yaml",
    "credentials.json",
    "auth-profiles.json",
    "vector.local.yaml",
    "paths.local.yaml",
    "local-dev.yaml",
    "user.local.yaml",
    "plugins.local.yaml",
    "weekly-report.local.yaml",
})


def resolve_data_root(repo_root: Path) -> Path:
    """环境变量 → 仓库根;相对路径按仓库根解析。"""
    value = os.environ.get(DATA_ROOT_ENV, "").strip()
    if not value:
        return Path(repo_root)
    path = Path(value).expanduser()
    return (path if path.is_absolute() else Path(repo_root) / path).resolve()


def resolve_records_root(repo_root: Path) -> Path:
    """记录根(worklog/排查记录/周报/工作项/告警与资源历史)唯一入口。"""
    return records_root_for(resolve_data_root(repo_root))


def records_root_for(data_root: Path) -> Path:
    """``<数据根>/config/paths.local.yaml`` 的 records_root → ``<数据根>/records``;相对路径按数据根解析。"""
    data_root = Path(data_root)
    config = data_config_path(data_root, "paths.local.yaml")
    value = ""
    if config.is_file():
        data = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{config}: 顶层必须是映射")
        value = str(data.get(RECORDS_ROOT_KEY) or "").strip()
    if not value:
        return data_root / "records"
    path = Path(value).expanduser()
    return (path if path.is_absolute() else data_root / path).resolve()


def ambient_data_root() -> Path:
    """拿不到仓库根的库函数/CLI 缺省:设了数据根落数据根,未设沿用 cwd 相对(约定从仓库根运行)。"""
    return resolve_data_root(Path("."))


def local_config_path(repo_root: Path, name: str) -> Path:
    """本机私有配置一律在 ``<数据根>/config/`` 下;只认登记的文件名。"""
    return data_config_path(resolve_data_root(repo_root), name)


def data_config_path(data_root: Path, name: str) -> Path:
    """已知数据根时定位其下的本机私有配置(数据层按自身数据根取用户配置)。"""
    if name not in LOCAL_CONFIG_NAMES:
        raise ValueError(f"{name!r} 不是登记的本机私有配置;入库配置请直接读 <repo>/config")
    return Path(data_root) / "config" / name
