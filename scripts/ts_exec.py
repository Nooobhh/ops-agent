"""深挖单条命令:经 SSHPool+红线执行,自动记入 session。stdout 纯 JSON。

WRITE 级命令未带 --approved 时返回 {"error": "write_needs_approval"}:
skill 必须停下向人展示 命令+影响+回滚,得到明确批准后才允许带 --approved 重试。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _common import REPO_ROOT, build_pool, emit_err, emit_ok
from lib.envpack import load_env_pack
from lib.data_root import local_config_path
from lib.datastore import open_datastore
from lib.cmd_filter import CommandDenied
from lib.record import TroubleshootSession
from lib.sanitize import redact_secrets


def run(argv=None, pool=None) -> int:
    # 排查记录经数据层定位,调用时解析(缺省布局随调用时 cwd 走,不在导入时固定)
    troubleshoot = open_datastore().troubleshoot
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--node", required=True)
    ap.add_argument("--cmd", required=True)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--approved", action="store_true",
                    help="仅人在环明确批准 WRITE 后使用")
    ap.add_argument("--platform-config", default=None,
                    help="缺省从 session.json 的 platform 推导(hd/iot 节点名重叠,禁止猜)")
    ap.add_argument("--local-dev", default=str(local_config_path(REPO_ROOT, "local-dev.yaml")))
    a = ap.parse_args(argv)

    session_dir = troubleshoot.root / a.run_id
    if not (session_dir / "session.json").exists():
        return emit_err("no_session", f"先 ts_session.py start: {a.run_id}")

    if pool is None:
        platform_config = a.platform_config
        if platform_config is None:
            try:
                session_data = json.loads(
                    (session_dir / "session.json").read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                return emit_err("config_error", f"session.json 不可读: {e}")
            platform = session_data.get("platform")
            pack = load_env_pack(REPO_ROOT)
            if platform not in pack.cluster_keys("managed"):
                return emit_err("config_error",
                                f"session platform 非法({platform!r}),显式给 --platform-config")
            platform_config = str(pack.source_path(platform, "hosts"))
        try:
            pool = build_pool(platform_config, a.local_dev)
        except Exception as e:
            return emit_err("config_error", f"pool 构建失败(检查 platform/credentials 配置): {e}")
    try:
        r = pool.run(a.node, a.cmd, timeout=a.timeout, approved=a.approved)
    except CommandDenied as e:
        kind = "forbidden" if e.verdict.level.value == "forbidden" \
            else "write_needs_approval"
        return emit_err(kind, e.verdict.reason, cmd=a.cmd)
    except Exception as e:
        return emit_err("exec_error", f"{type(e).__name__}: {e}")

    session = TroubleshootSession(dir=session_dir, run_id=a.run_id)
    try:
        session.record_deep_dive_step(
            cmd=a.cmd, exit_code=r.exit_code if r.exit_code is not None else -1,
            stdout=r.stdout, stderr=r.stderr, approved=a.approved,
        )
    except Exception as e:
        return emit_err("record_error",
                        f"命令已执行但落档失败,须人工核对 session: {type(e).__name__}: {e}",
                        cmd=a.cmd, exit_code=r.exit_code,
                        stdout=redact_secrets(r.stdout),
                        stderr=redact_secrets(r.stderr), approved=a.approved)
    return emit_ok(exit_code=r.exit_code, stdout=redact_secrets(r.stdout),
                   stderr=redact_secrets(r.stderr),
                   duration=round(r.duration, 3), timed_out=r.timed_out)


if __name__ == "__main__":
    sys.exit(run())
