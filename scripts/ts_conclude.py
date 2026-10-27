"""收口 finalize:conclusion JSON 从文件读(规避引号转义),出报告。stdout 纯 JSON。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from scripts._common import emit_err, emit_ok
except ImportError:  # direct script execution
    from _common import emit_err, emit_ok
from lib.datastore import open_datastore
from lib.playbook import SEVERITIES, VERDICTS
from lib.record import OplogMissing, TroubleshootSession

REQUIRED = {"verdict", "severity", "root_cause", "evidence", "remediation"}
def run(argv=None) -> int:
    # 排查记录经数据层定位,调用时解析(缺省布局随调用时 cwd 走,不在导入时固定)
    troubleshoot = open_datastore().troubleshoot
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--conclusion-json-file", required=True,
                    help="推荐放 records/troubleshoot/<run_id>/artifacts/conclusion.json")
    a = ap.parse_args(argv)

    session_dir = troubleshoot.root / a.run_id
    if not (session_dir / "session.json").exists():
        return emit_err("no_session", f"无此 session: {a.run_id}")

    try:
        c = json.loads(Path(a.conclusion_json_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return emit_err("bad_conclusion_file", str(e))
    if not isinstance(c, dict):
        return emit_err("bad_conclusion_file", f"结构损坏(非对象): {type(c).__name__}")

    missing = sorted(REQUIRED - set(c))
    if missing:
        return emit_err("missing_keys", f"conclusion 缺少键: {', '.join(missing)}")
    if c["verdict"] not in VERDICTS:
        return emit_err("bad_verdict", f"verdict 必须是 {sorted(VERDICTS)}")
    if c["severity"] not in SEVERITIES:
        return emit_err("bad_severity", f"severity 必须是 {sorted(SEVERITIES)}")
    for key in ("evidence", "remediation"):
        if not isinstance(c[key], list):
            return emit_err("bad_conclusion_file", f"{key} 必须是 list,得到 {type(c[key]).__name__}")
    if "oplogs" in c and c["oplogs"] is not None and not isinstance(c["oplogs"], list):
        return emit_err("bad_conclusion_file", f"oplogs 必须是 list,得到 {type(c['oplogs']).__name__}")
    if any(key in c for key in ("kb_doc", "kb_draft", "kb_doc_effective")):
        return emit_err(
            "knowledge_entry_closed",
            "ts_conclude 不再写入或晋升知识库；知识文档只允许由中台排查生成，"
            "或经 scripts/kb_import.py 手动导入。",
        )

    try:
        TroubleshootSession(dir=session_dir, run_id=a.run_id, data_root=troubleshoot.data_root).finalize(
            verdict=c["verdict"], severity=c["severity"],
            root_cause=c["root_cause"], evidence=list(c["evidence"]),
            remediation=list(c["remediation"]), oplogs=list(c.get("oplogs") or []),
        )
    except OplogMissing as e:
        return emit_err("oplog_missing", str(e))
    except Exception as e:
        return emit_err("record_error", f"finalize 失败,须人工核对 session: {type(e).__name__}: {e}")

    try:
        from lib.worklog import append_from_troubleshoot
        append_from_troubleshoot(session_dir)
    except Exception as e:
        print(f"WARN worklog auto-emit 失败: {e}", file=sys.stderr)

    return emit_ok(report=str(session_dir / "report.md"))


if __name__ == "__main__":
    sys.exit(run())
