"""标准档执行:playbook 推送到节点一次执行,解析 JSON 契约返回。"""
from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import List, Optional

from .playbook import PlaybookResult, parse_result

PLAYBOOK_LIB = Path(__file__).resolve().parents[1] / "playbooks" / "_lib" / "playbook_lib.sh"

# run_id 必须是文件名安全字符集(字母数字/下划线/连字符/点),防注入与穿越
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


class PlaybookTimeout(Exception):
    pass


def run_playbook(pool, node: str, playbook_path: Path, run_id: str,
                 args: Optional[List[str]] = None, timeout: float = 180.0,
                 cleanup: bool = True) -> PlaybookResult:
    if not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError(
            f"run_id 只允许 [A-Za-z0-9_.-],得到 {run_id!r} — 防命令注入/路径穿越"
        )
    playbook_path = Path(playbook_path)
    remote_dir = f"/tmp/opsagent/{run_id}"
    remote_lib = f"{remote_dir}/playbook_lib.sh"
    remote_pb = f"{remote_dir}/{playbook_path.name}"

    pool.run(node, f"mkdir -p {remote_dir}")
    pool.push(node, str(PLAYBOOK_LIB), remote_lib)
    pool.push(node, str(playbook_path), remote_pb)

    # 部署清单注入:playbook 的 step_opt 据此区分「组件未部署」与「组件挂了」。
    # 取不到时留空,step_opt 会退回严格判定,宁可误报不可漏报。
    try:
        components = ",".join(pool.components(node))
    except Exception:
        components = ""

    cmd = (f"OPSAGENT_RUN_DIR={remote_dir} OPSAGENT_NODE={node} "
           f"OPSAGENT_COMPONENTS={shlex.quote(components)} "
           f"PLAYBOOK_LIB={remote_lib} bash {remote_pb}")
    if args:
        cmd += " " + " ".join(shlex.quote(a) for a in args)
    res = pool.run(node, cmd, timeout=timeout)
    try:
        if res.timed_out:
            raise PlaybookTimeout(f"{playbook_path.name} 在 {node} 超时({timeout}s)")
        return parse_result(res.stdout)
    finally:
        if cleanup:
            try:
                # run_id 已过 _RUN_ID_RE 白名单,路径固定前缀;清理属既定回收,approved 放行 WRITE 级 rm
                pool.run(node, f"rm -rf /tmp/opsagent/{run_id}", approved=True)
            except Exception:
                pass  # 清理尽力而为,不影响排查结果
