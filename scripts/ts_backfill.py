#!/usr/bin/env python
"""从事后命令清单生成 open 排查 session。stdout 纯 JSON。"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from scripts._common import emit_err, emit_ok
except ImportError:  # direct script execution
    from _common import emit_err, emit_ok
from lib.datastore import open_datastore
from lib.envpack import active_env_pack, env_timezone
from lib.sanitize import REDACTED, redact_secrets

_MYSQL_PASSWORD = re.compile(
    r"(?<!\S)(-p)(?:'[^']*'|\"[^\"]*\"|\S+)"
)


class ManifestError(ValueError):
    pass


def _new_run_id() -> str:
    stamp = datetime.now(env_timezone()).strftime("%Y%m%d-%H%M")
    return f"bf-{stamp}-{secrets.token_hex(2)}"


def _require_nonempty_string(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{field} 必须是非空字符串")
    return value


def _normalize_manifest(value) -> dict:
    if not isinstance(value, dict):
        raise ManifestError(f"manifest 必须是对象,得到 {type(value).__name__}")

    platform = value.get("platform")
    managed = active_env_pack().cluster_keys("managed")
    if platform not in managed:
        raise ManifestError(f"platform 必须是 {' 或 '.join(managed)}")
    trigger = _require_nonempty_string(value.get("trigger"), "trigger")

    service = value.get("service", "")
    if not isinstance(service, str):
        raise ManifestError("service 必须是字符串")
    session_type = value.get("session_type", "triage")
    if session_type not in {"triage", "survey"}:
        raise ManifestError("session_type 必须是 triage 或 survey")

    nodes = value.get("nodes")
    if nodes is not None:
        if not isinstance(nodes, list) or not nodes:
            raise ManifestError("nodes 必须是非空 list")
        if not all(isinstance(item, str) and item.strip() for item in nodes):
            raise ManifestError("nodes 元素必须全部是非空字符串")
        nodes = list(nodes)
        supplied_node = value.get("node")
        if supplied_node is not None:
            node = _require_nonempty_string(supplied_node, "node")
            if node != nodes[0]:
                raise ManifestError("node 必须缺省或等于 nodes[0]")
        else:
            node = nodes[0]
    else:
        node = _require_nonempty_string(value.get("node"), "node")

    raw_steps = value.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ManifestError("steps 必须是非空 list")
    steps = []
    for index, raw in enumerate(raw_steps):
        field = f"steps[{index}]"
        if not isinstance(raw, dict):
            raise ManifestError(f"{field} 必须是对象")
        cmd = _require_nonempty_string(raw.get("cmd"), f"{field}.cmd")
        exit_code = raw.get("exit_code")
        if type(exit_code) is not int:
            raise ManifestError(f"{field}.exit_code 必须是 int")
        stdout = raw.get("stdout", "")
        stderr = raw.get("stderr", "")
        approved = raw.get("approved", False)
        if not isinstance(stdout, str):
            raise ManifestError(f"{field}.stdout 必须是 str")
        if not isinstance(stderr, str):
            raise ManifestError(f"{field}.stderr 必须是 str")
        if type(approved) is not bool:
            raise ManifestError(f"{field}.approved 必须是 bool")
        steps.append({
            "cmd": cmd, "exit_code": exit_code, "stdout": stdout,
            "stderr": stderr, "approved": approved,
        })

    return {
        "platform": platform, "trigger": trigger, "service": service,
        "session_type": session_type, "node": node, "nodes": nodes,
        "steps": steps,
    }


def _redact_command(cmd: str) -> str:
    cmd = _MYSQL_PASSWORD.sub(lambda match: f"{match.group(1)}{REDACTED}", cmd)
    return redact_secrets(cmd)


def run(argv=None) -> int:
    # 排查记录经数据层定位,调用时解析(缺省布局随调用时 cwd 走,不在导入时固定)
    troubleshoot = open_datastore().troubleshoot
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args(argv)

    try:
        raw = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        manifest = _normalize_manifest(raw)
    except (OSError, ValueError) as exc:
        return emit_err("bad_manifest", str(exc))

    run_id = _new_run_id()
    session_dir = troubleshoot.root / run_id
    if session_dir.exists():
        return emit_err("exists", f"session 已存在: {run_id}")

    try:
        session = troubleshoot.start(
            run_id, node=manifest["node"],
            trigger=manifest["trigger"], platform=manifest["platform"],
            service=manifest["service"], session_type=manifest["session_type"],
            nodes=manifest["nodes"],
        )
        for step in manifest["steps"]:
            session.record_deep_dive_step(
                cmd=_redact_command(step["cmd"]),
                exit_code=step["exit_code"], stdout=step["stdout"],
                stderr=step["stderr"], approved=step["approved"],
            )
    except Exception as exc:
        return emit_err(
            "record_error", f"补录 session 失败,须人工核对: {type(exc).__name__}: {exc}",
        )

    return emit_ok(
        run_id=run_id, dir=str(session.dir), steps=len(manifest["steps"]),
        next="ts_conclude.py 收口；需入库时在中台生成或走 kb_import.py",
    )


if __name__ == "__main__":
    raise SystemExit(run())
