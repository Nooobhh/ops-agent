"""排查 session 生命周期:start / status。stdout 纯 JSON。"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _common import REPO_ROOT, emit_err, emit_ok
from lib.datastore import open_datastore
from lib.envpack import env_timezone, load_env_pack
from lib.runner import _RUN_ID_RE


def _new_run_id() -> str:
    # 时间一律取环境时区(env.yaml timezone),不读宿主时区(mac 本地时钟曾致 run_id/worklog 错天)
    stamp = datetime.now(env_timezone()).strftime("%Y%m%d-%H%M")
    return f"ts-{stamp}-{secrets.token_hex(2)}"


def run(argv=None) -> int:
    # 排查记录经数据层定位,调用时解析(缺省布局随调用时 cwd 走,不在导入时固定)
    troubleshoot = open_datastore().troubleshoot
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="op", required=True)
    p = sub.add_parser("start")
    p.add_argument("--run-id", help="缺省自动生成 ts-YYYYMMDD-HHMM-xxxx")
    nodes_group = p.add_mutually_exclusive_group(required=True)
    nodes_group.add_argument("--node")
    nodes_group.add_argument("--nodes", help="逗号分隔的节点集")
    p.add_argument("--type", dest="session_type", choices=["triage", "survey"],
                   default="triage")
    p.add_argument("--trigger", required=True, help='告警名或 "manual:<症状>"')
    p.add_argument("--platform", required=True, choices=load_env_pack(REPO_ROOT).cluster_keys("managed"))
    p.add_argument("--service", default="")
    q = sub.add_parser("status")
    q.add_argument("--run-id", required=True)
    a = ap.parse_args(argv)

    if a.op == "start":
        parsed_nodes = None
        node = a.node
        if a.nodes is not None:
            parsed_nodes = [item.strip() for item in a.nodes.split(",") if item.strip()]
            if not parsed_nodes:
                return emit_err("bad_nodes", "--nodes 至少包含一个非空节点")
            node = parsed_nodes[0]
        run_id = a.run_id or _new_run_id()
        if not _RUN_ID_RE.fullmatch(run_id):
            return emit_err("bad_run_id", f"run_id 只允许 [A-Za-z0-9_.-]: {run_id!r}")
        if (troubleshoot.root / run_id).exists():
            return emit_err("exists", f"session 已存在: {run_id}")
        session_nodes = parsed_nodes \
            if a.session_type == "survey" or parsed_nodes and len(parsed_nodes) > 1 \
            else None
        s = troubleshoot.start(
            run_id, node=node, trigger=a.trigger,
            platform=a.platform, service=a.service, session_type=a.session_type,
            nodes=session_nodes,
        )
        return emit_ok(run_id=s.run_id, dir=str(s.dir))

    path = troubleshoot.root / a.run_id / "session.json"
    if not path.exists():
        return emit_err("not_found", f"无此 session: {a.run_id}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return emit_err("config_error", f"session.json 不可读: {e}")
    if not isinstance(data, dict):
        return emit_err("config_error", f"session.json 结构损坏(非对象): {type(data).__name__}")
    return emit_ok(run_id=a.run_id, verdict=data.get("verdict"),
                   severity=data.get("severity"),
                   playbook_results=len(data.get("playbook_results") or []),
                   steps=len(data.get("steps") or []))


if __name__ == "__main__":
    sys.exit(run())
