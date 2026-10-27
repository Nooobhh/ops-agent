"""record_kb_promotion:session.json 记录 + report 条件渲染 + lint 一致性。"""
import json
from pathlib import Path

from lib.record import TroubleshootSession, _render_report, lint_records, start_session


def _mk_session(tmp_path):
    root = tmp_path / "records" / "troubleshoot"
    s = start_session(root, run_id="ts-20260714-0003-9f00", node="node1",
                      trigger="demo_alert", platform="alpha", service="system")
    s.finalize(verdict="conclusive", severity="P2", root_cause="演示",
               evidence=["e1"], remediation=["r1"], oplogs=[])
    return root, s


def test_record_kb_promotion_renders_section(tmp_path):
    root, s = _mk_session(tmp_path)
    s.record_kb_promotion(doc="alpha/system/alpha-system-demo-case.md",
                          verified_at="2026-07-14")
    data = json.loads((s.dir / "session.json").read_text(encoding="utf-8"))
    assert data["kb_promotion"] == {"doc": "alpha/system/alpha-system-demo-case.md",
                                    "verified_at": "2026-07-14"}
    report = (s.dir / "report.md").read_text(encoding="utf-8")
    assert "知识库沉淀" in report and "alpha-system-demo-case" in report
    assert lint_records(root) == []      # 重渲染一致,lint 干净


def test_render_without_promotion_unchanged(tmp_path):
    root, s = _mk_session(tmp_path)
    report = (s.dir / "report.md").read_text(encoding="utf-8")
    assert "知识库沉淀" not in report     # 老形态不受影响
    assert lint_records(root) == []
