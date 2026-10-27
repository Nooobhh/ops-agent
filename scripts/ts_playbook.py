"""跑 routing 登记的 playbook 并记入 session。未登记路径拒绝。stdout 纯 JSON。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _common import REPO_ROOT, build_pool, emit_err, emit_ok, env_config
from lib.data_root import ambient_data_root, local_config_path
from lib.envpack import EnvPackError, load_env_pack
from lib.datastore import open_datastore
from lib.record import TroubleshootSession
from lib.runner import PlaybookTimeout, run_playbook
from lib.triage import iter_alert_entries, load_routing


def registered_playbooks(routing: dict) -> set:
    out = set()
    for e in iter_alert_entries(routing):
        if e.get("playbook"):
            out.add(e["playbook"])
    for d in routing.get("docs") or []:
        if d.get("playbook"):
            out.add(d["playbook"])
    return out


def run(argv=None, pool=None) -> int:
    # 排查记录经数据层定位,调用时解析(缺省布局随调用时 cwd 走,不在导入时固定)
    troubleshoot = open_datastore().troubleshoot
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--node", required=True)
    ap.add_argument("--playbook", required=True, help="必须是 routing 登记的路径")
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--routing", default=str(ambient_data_root() / "build" / "routing.json"))
    ap.add_argument("--platform-config", help="缺省取环境包 config/platform-hd.yaml")
    ap.add_argument("--local-dev", default=str(local_config_path(REPO_ROOT, "local-dev.yaml")))
    a = ap.parse_args(argv)

    session_dir = troubleshoot.root / a.run_id
    if not (session_dir / "session.json").exists():
        return emit_err("no_session", f"先 ts_session.py start: {a.run_id}")
    try:
        routing = load_routing(Path(a.routing))
    except (OSError, ValueError) as e:
        return emit_err("config_error", f"routing 索引不可读(先跑 scripts/build_routing.py?): {e}")
    if a.playbook not in registered_playbooks(routing):
        return emit_err("unregistered_playbook",
                        f"路径未在 routing 登记,拒绝执行: {a.playbook}")
    try:
        playbook_path = load_env_pack(REPO_ROOT).playbook_path(a.playbook)
    except EnvPackError as e:
        return emit_err("playbook_not_found", str(e))
    if not playbook_path.exists():
        return emit_err("playbook_not_found", a.playbook)

    if pool is None:
        try:
            pool = build_pool(a.platform_config or env_config("platform-hd.yaml"), a.local_dev)
        except Exception as e:
            return emit_err("config_error", f"pool 构建失败(检查 platform/credentials 配置): {e}")
    try:
        result = run_playbook(pool, a.node, playbook_path,
                              run_id=a.run_id, timeout=a.timeout)
    except PlaybookTimeout as e:
        return emit_err("playbook_timeout", str(e))
    except Exception as e:
        return emit_err("playbook_execution_error", f"{type(e).__name__}: {e}")

    try:
        TroubleshootSession(dir=session_dir, run_id=a.run_id) \
            .record_playbook_result(result.to_session_dict())
    except Exception as e:
        return emit_err("record_error",
                        f"playbook 已执行但落档失败,须人工核对 session: {type(e).__name__}: {e}",
                        verdict=result.verdict, severity=result.severity,
                        conclusion_hint=result.conclusion_hint)
    return emit_ok(
        verdict=result.verdict, severity=result.severity,
        conclusion_hint=result.conclusion_hint,
        steps=[{"name": s.name, "pass": s.pass_, "expect": s.expect,
                "actual": s.actual} for s in result.steps],
        metrics=result.metrics, next_playbook=result.next_playbook,
    )


if __name__ == "__main__":
    sys.exit(run())
