"""测试用环境包辅助。"""
from __future__ import annotations

from pathlib import Path

import yaml

from lib.data_root import local_config_path

REPO = Path(__file__).resolve().parents[1]
EXAMPLE_PACK = REPO / "envs" / "example"


def as_env_pack(repo_root: Path, *, extensions: bool = False, source: Path = EXAMPLE_PACK) -> Path:
    """让自建的临时仓根同时充当环境包:放一份真实 env.yaml,并经 paths.local.yaml 指向自己。

    夹具照旧写在 ``<临时仓>/config/``、``<临时仓>/playbooks/`` 即落在环境包内;返回临时仓根。
    env.yaml 缺省取示例环境包(``source`` 可换);缺省不带 Python 扩展,``extensions=True`` 时软链源包的扩展目录。
    """
    root = Path(repo_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    env = yaml.safe_load((Path(source) / "env.yaml").read_text(encoding="utf-8"))
    if extensions:
        (root / env["python"]).symlink_to(Path(source) / env["python"], target_is_directory=True)
    else:
        env.pop("python", None)
        env.pop("extensions", None)
    (root / "env.yaml").write_text(yaml.safe_dump(env, allow_unicode=True, sort_keys=False), encoding="utf-8")
    local = local_config_path(root, "paths.local.yaml")
    local.parent.mkdir(parents=True, exist_ok=True)
    data = (yaml.safe_load(local.read_text(encoding="utf-8")) or {}) if local.is_file() else {}
    data["env_pack"] = str(root)
    local.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return root


REDLINES_TEMPLATE = Path(__file__).resolve().parent / "fixtures" / "kb" / "redlines.md"


def write_redlines(path: Path, pack=None) -> Path:
    """写一份与当前有效红线规则(核心 + 环境包补充)sha 对得上的 redlines.md,代替拷真实知识库。"""
    from lib.cmd_filter import redline_sources
    from lib.kb.redlines import rules_sha256

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = REDLINES_TEMPLATE.read_text(encoding="utf-8")
    path.write_text(text.replace("{filter_rules_sha256}", rules_sha256(redline_sources(pack))), encoding="utf-8")
    return path


def example_data_root(root: Path) -> dict:
    """子进程看不到测试里强制挂的示例包:把 root 当数据根,paths.local.yaml 指示例包,返回子进程环境变量。"""
    import os

    root = Path(root).resolve()
    local = root / "config" / "paths.local.yaml"
    local.parent.mkdir(parents=True, exist_ok=True)
    data = (yaml.safe_load(local.read_text(encoding="utf-8")) or {}) if local.is_file() else {}
    data["env_pack"] = str(EXAMPLE_PACK)
    local.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return {**os.environ, "OPS_AGENT_DATA_ROOT": str(root)}
