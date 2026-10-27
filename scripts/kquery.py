"""通用取数 CLI:多节点单命令,stdout 纯 JSON。

调查型排查的标准取数通道,替代每次现写 probe 脚本。只读定位:
WRITE 级命令一律拒,写操作走 ts_exec --approved 人在环。
命令仍经 SSHPool.run() 唯一入口过红线;输出经 lib.sanitize 脱敏。

用法:
  kquery.py --platform <集群> --nodes node1,node2 --cmd "ls /opt"
MySQL 查询不走这里:一律经 MySQL 插件(插件页按连接配置)。
可选 --run-id:结果逐节点落入该排查 session 的 steps(cmd 带 [node] 前缀)。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _common import REPO_ROOT, build_pool_for_platform, emit_err, emit_ok
from lib.envpack import load_env_pack
from lib.datastore import open_datastore
from lib.cmd_filter import CommandDenied
from lib.network_env import configure_direct_network
from lib.sanitize import redact_secrets
from lib.ssh_pool import SSHAuthFailed


def run(argv=None, pool=None) -> int:
    # 排查记录经数据层定位,调用时解析(缺省布局随调用时 cwd 走,不在导入时固定)
    troubleshoot = open_datastore().troubleshoot
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--platform", required=True, choices=load_env_pack(REPO_ROOT).cluster_keys("managed"))
    ap.add_argument("--nodes", required=True, help="逗号分隔,如 node3,node4,node16")
    ap.add_argument("--cmd", required=True, help="只读 shell 命令,原样在每个节点执行")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--run-id", help="可选:结果落入 records/troubleshoot/<run-id> 的 steps")
    a = ap.parse_args(argv)

    nodes = [n.strip() for n in a.nodes.split(",") if n.strip()]
    if not nodes:
        return emit_err("bad_args", "--nodes 为空")
    cmd = a.cmd

    session = None
    if a.run_id:
        session_dir = troubleshoot.root / a.run_id
        if not (session_dir / "session.json").exists():
            return emit_err("no_session", f"先 ts_session.py start: {a.run_id}")
        from lib.record import TroubleshootSession
        session = TroubleshootSession(dir=session_dir, run_id=a.run_id)

    configure_direct_network()
    if pool is None:
        try:
            pool = build_pool_for_platform(a.platform, REPO_ROOT)
        except Exception as e:
            return emit_err("config_error", f"pool 构建失败(检查 platform/credentials 配置): {e}")

    results = []
    for node in nodes:
        entry = {"node": node}
        try:
            r = pool.run(node, cmd, timeout=a.timeout)
            entry.update(exit_code=r.exit_code, stdout=redact_secrets(r.stdout),
                         stderr=redact_secrets(r.stderr),
                         duration=round(r.duration, 3), timed_out=r.timed_out)
        except CommandDenied as e:
            kind = "forbidden" if e.verdict.level.value == "forbidden" else "write_denied"
            return emit_err(kind,
                            f"{e.verdict.reason};kquery 只读,写操作走 ts_exec --approved 人在环",
                            cmd=cmd)
        except SSHAuthFailed as e:
            entry.update(error="auth_failed",
                         message=f"{e};请在设置页手动生成凭证，或显式运行 scripts/node_credentials.py --replace-existing")
        except Exception as e:
            entry.update(error="exec_error", message=f"{type(e).__name__}: {e}")
        results.append(entry)
        if session is not None and "exit_code" in entry:
            session.record_deep_dive_step(
                cmd=f"[{node}] {cmd}", exit_code=entry["exit_code"],
                stdout=entry["stdout"], stderr=entry["stderr"])
    return emit_ok(cmd=cmd, results=results)


if __name__ == "__main__":
    sys.exit(run())
