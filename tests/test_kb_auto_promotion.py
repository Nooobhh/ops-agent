import json
import shutil
from pathlib import Path

import pytest

from lib.kb.contract import ImportManifest, render_document
from lib.kb.frontmatter import load_doc
from lib.kb.promote import PromoteError, promote_auto
from lib.kb.promotion import evaluate_auto_promotion
from lib.triage_agent.knowledge_sink import KnowledgeSink
from lib.triage_agent.paths import AppPaths
from tests.envpack_helpers import write_redlines


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOC_REL = "alpha/system/alpha-system-memory-pressure-auto.md"
ORIGIN = "ts-20260801-0101-origin"
LATER = "ts-20260802-0101-later"


def _repo(tmp_path: Path, *, include_later_case: bool = True) -> Path:
    (tmp_path / "knowledge/shared/procedures").mkdir(parents=True)
    (tmp_path / "lib/data").mkdir(parents=True)
    write_redlines(tmp_path / "knowledge/shared/procedures/redlines.md")
    shutil.copy2(PROJECT_ROOT / "lib/data/filter_rules.yaml",
                 tmp_path / "lib/data/filter_rules.yaml")
    manifest = ImportManifest.model_validate({
        "title": "Alpha 主机内存压力自动晋升测试",
        "type": "troubleshoot",
        "platform": "alpha",
        "service": "system",
        "problem": "memory-pressure-auto",
        "summary": "swap 高位但没有活跃换页时的判断方法。",
        "symptoms": ["swap 使用率持续高位"],
        "alerts": [], "playbook": None, "related": [],
        "sections": {
            "适用现象": "- swap 使用率高",
            "关键判据": "- swap 55.8% 且 si=0 so=0",
            "机理": "历史高峰换出的页面仍留在 swap。",
            "排查步骤": "1. 运行 free 和 vmstat。",
            "处置建议": "- 低峰确认可用内存后由人工回收。",
            "历史案例": f"- 2026-08-01 · `{ORIGIN}` · conclusive" + (
                f"\n- 2026-08-02 · `{LATER}` · conclusive"
                if include_later_case else ""
            ),
        },
    })
    target = tmp_path / "knowledge" / DOC_REL
    target.parent.mkdir(parents=True)
    target.write_text(
        render_document(manifest, source="triage", source_ref=ORIGIN),
        encoding="utf-8",
    )
    run = tmp_path / "records/troubleshoot" / LATER
    run.mkdir(parents=True)
    (run / "session.json").write_text(json.dumps({
        "run_id": LATER, "platform": "alpha", "service": "system",
        "verdict": "conclusive", "trigger": "swap 使用率持续高位",
        "root_cause": "历史高峰 swap 残留，没有活跃换页",
        "evidence": ["swap 55.8%", "si=0 so=0"],
        "remediation": [f"参考 {DOC_REL} 低峰人工回收"],
        "steps": [{
            "cmd": "vmstat 1 3", "exit_code": 0,
            "stdout": "swap 55.8%\nsi=0 so=0", "stderr": "",
        }],
    }, ensure_ascii=False), encoding="utf-8")
    (run / "report.md").write_text(
        f"# 报告\n\n`{DOC_REL}`\n\nswap 55.8%，si=0 so=0，没有活跃换页。\n",
        encoding="utf-8",
    )
    return tmp_path


def test_shared_policy_and_auto_promoter_accept_later_conclusive_run(tmp_path):
    repo = _repo(tmp_path)
    decision = evaluate_auto_promotion(repo, DOC_REL, LATER)
    assert decision.eligible is True
    info = promote_auto(DOC_REL, run_id=LATER, repo_root=repo)
    assert info["verified_by"] == LATER
    doc = load_doc(repo / "knowledge" / DOC_REL)
    assert doc.status == "verified"
    assert "后续中台排查" in doc.raw["verified_reason"]


def test_origin_run_cannot_verify_its_own_draft(tmp_path):
    repo = _repo(tmp_path)
    decision = evaluate_auto_promotion(repo, DOC_REL, ORIGIN)
    assert decision.eligible is False
    assert any("不能验证自己" in reason for reason in decision.reasons)
    with pytest.raises(PromoteError, match="条件不满足"):
        promote_auto(DOC_REL, run_id=ORIGIN, repo_root=repo)


def test_console_append_case_uses_shared_policy_and_auto_promotes(tmp_path):
    repo = _repo(tmp_path, include_later_case=False)
    sink = KnowledgeSink(AppPaths.from_repo_root(repo))
    result = sink.persist(LATER, {
        "mode": "append_case",
        "title": "Alpha 主机内存压力自动晋升测试",
        "platform": "alpha", "service": "system",
        "problem": "memory-pressure-auto",
        "symptoms": ["swap 使用率持续高位"], "alerts": [],
        "mechanism": "历史高峰 swap 残留，没有活跃换页",
        "evidence": ["swap 55.8%", "si=0 so=0"],
        "remediation": ["低峰人工回收"], "doc": DOC_REL,
    })
    assert result.promotion is not None
    assert result.promotion["verified_by"] == LATER
    assert load_doc(repo / "knowledge" / DOC_REL).status == "verified"
