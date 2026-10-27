"""动作脚本共享件:JSON 输出契约 + SSH pool 构建。

契约:stdout 只输出一个 JSON 对象。
  成功 {"ok": true, ...}                       exit 0
  失败 {"ok": false, "error": "...", "message": "..."}  exit 1
  参数非法由 argparse 处理(exit 2, stderr)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from lib.data_root import local_config_path, resolve_data_root

REPO_ROOT = Path(__file__).resolve().parents[1]


def emit_ok(**payload) -> int:
    print(json.dumps({"ok": True, **payload}, ensure_ascii=False))
    return 0


def emit_err(error: str, message: str, **payload) -> int:
    print(json.dumps({"ok": False, "error": error, "message": message, **payload},
                     ensure_ascii=False))
    return 1


def build_pool(platform_path: str, local_dev_path: str,
               credentials_path: Optional[str] = None):
    """与 patrol_runner 同构的 pool 构建(local-dev 存在=跳板,否则直连)。

    配置文件缺失/格式错会抛异常;调用方(动作脚本)应捕获并转
    emit_err("config_error", ...)保持 stdout JSON 契约。
    credentials_path 缺省为数据根下 config/credentials.json;local-dev 里的
    相对凭证路径同样按数据根解析,不随进程 cwd 漂移。
    """
    from lib.cmd_filter import CmdFilter
    from lib.config import load_local_dev, load_platform
    from lib.credentials import FileCredentials
    from lib.ssh_pool import SSHPool

    plat = load_platform(Path(platform_path))
    dev = load_local_dev(Path(local_dev_path))
    if dev:
        path = dev.credentials_file.expanduser()
        creds_path = path if path.is_absolute() else resolve_data_root(REPO_ROOT) / path
    else:
        creds_path = Path(credentials_path or local_config_path(REPO_ROOT, "credentials.json"))
    creds = FileCredentials(creds_path, merge_home=True)
    return SSHPool(plat, creds, CmdFilter.default(),
                   jump=dev.jump if dev else None)


def env_config(name: str) -> str:
    """环境包 config/ 下的文件路径(脚本参数缺省值用;不在模块级调用)。"""
    from lib.envpack import load_env_pack

    return str(load_env_pack(REPO_ROOT).config_file(name))


def build_pool_for_platform(platform: str, repo_root: Path):
    from lib.triage_agent.targets import target_config_path

    root = Path(repo_root)
    return build_pool(
        str(target_config_path(root, platform)),
        str(local_config_path(root, "local-dev.yaml")),
        str(local_config_path(root, "credentials.json")),
    )
