"""免安装包首次运行与升级后的知识索引准备;源码运行(无 package-info.json)时什么都不做。

索引戳与包版本不一致 → 重建 build/ 派生索引(新版本可能改了索引 schema)。包内不带知识库:
知识库是应用数据,由各机放进数据目录(缺省 <数据根>/knowledge);没有时照常重建出空索引、照常起中台。
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Callable, Optional

from lib.data_root import resolve_data_root

PACKAGE_INFO = "package-info.json"
INDEX_STAMP = Path("build") / ".package-version"
CREATE_NO_WINDOW = 0x08000000


def package_version(repo_root: Path) -> Optional[str]:
    try:
        info = json.loads((Path(repo_root) / PACKAGE_INFO).read_text(encoding="utf-8"))
        return f"{info['version']}+{info['commit']}"
    except (OSError, ValueError, KeyError, TypeError):
        return None


def prepare_package(repo_root: Path, python: str, log_path: Path,
                    *, run: Callable = subprocess.run) -> Optional[str]:
    """返回错误说明;None 表示无需准备或准备成功。"""
    repo_root = Path(repo_root)
    version = package_version(repo_root)
    if version is None:
        return None
    stamp = resolve_data_root(repo_root) / INDEX_STAMP     # 索引戳随 build/ 落数据根
    try:
        if stamp.read_text(encoding="utf-8").strip() == version:
            return None
    except OSError:
        pass
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    kwargs = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}
    # build_wiki 按数据根定位 knowledge/ 与 build/(子进程继承 OPS_AGENT_DATA_ROOT)
    with open(log_path, "ab") as log:
        result = run(
            [python, str(repo_root / "scripts" / "build_wiki.py")],
            cwd=str(repo_root), env=env, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, **kwargs,
        )
    if result.returncode != 0:
        return "知识索引重建失败,详见日志"
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(version, encoding="utf-8")
    return None
