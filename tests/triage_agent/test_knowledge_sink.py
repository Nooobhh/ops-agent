import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from lib.kb.lint import lint_doc
from lib.triage_agent.knowledge_sink import (
    KbSinkError,
    KnowledgeSink,
)
from lib.triage_agent.paths import AppPaths


def _write_session(paths, run_id="ts-1"):
    run_dir = paths.records_root / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "session.json").write_text(json.dumps({
        "run_id": run_id,
        "node": "node1",
        "trigger": "MemoryHigh",
        "platform": "alpha",
        "service": "system",
        "verdict": "conclusive",
        "root_cause": "memory pressure",
        "evidence": ["memory actual"],
        "remediation": ["observe"],
        "steps": [{
            "cmd": "free -m",
            "stdout": "memory actual",
            "exit_code": 0,
        }],
        "playbook_results": [],
    }, ensure_ascii=False), encoding="utf-8")
    (run_dir / "report.md").write_text("# report\n", encoding="utf-8")


def _proposal(**updates):
    value = {
        "mode": "new",
        "title": "Alpha system memory pressure",
        "platform": "alpha",
        "service": "system",
        "problem": "memory-pressure",
        "symptoms": ["memory high"],
        "alerts": ["MemoryHigh"],
        "mechanism": "memory pressure",
        "evidence": ["memory actual"],
        "remediation": ["observe"],
        "doc": None,
    }
    value.update(updates)
    return value


@pytest.fixture
def paths(tmp_path):
    value = AppPaths.from_repo_root(tmp_path)
    value.knowledge_root.mkdir(parents=True)
    value.state_dir.mkdir(parents=True)
    _write_session(value)
    return value


@pytest.fixture
def rebuild_calls():
    return []


@pytest.fixture
def sink(paths, rebuild_calls):
    return KnowledgeSink(
        paths,
        validate=lambda: None,
        rebuild=lambda: rebuild_calls.append("rebuild"),
        now=lambda: datetime(
            2026, 7, 25, 9, 0,
            tzinfo=ZoneInfo("Etc/GMT-8"),
        ),
    )


def test_new_deep_dive_writes_draft_and_rebuilds(paths, sink, rebuild_calls):
    result = sink.persist("ts-1", _proposal())

    text = result.path.read_text(encoding="utf-8")
    assert result.path == (
        paths.knowledge_root / "alpha" / "system"
        / "alpha-system-memory-pressure.md"
    )
    assert text.startswith("---\n")
    assert "status: draft" in text
    assert "memory actual" in text
    assert "`records/troubleshoot/ts-1/report.md`" in text
    assert "platform: example/alpha\n" in text and "author:" not in text   # session 无作者:不写
    assert lint_doc(
        result.path,
        kb_root=paths.knowledge_root,
        repo_root=paths.repo_root,
    ) == []
    assert rebuild_calls == ["rebuild"]


def test_existing_doc_only_gets_a_fixed_history_case(paths, sink):
    target = paths.knowledge_root / "alpha" / "system" / "existing.md"
    target.parent.mkdir(parents=True)
    original = (
        "---\ntitle: Existing\ntype: troubleshoot\nplatform: alpha\n"
        "service: system\nproblem: existing\nsymptoms: []\nalerts: []\n"
        "playbook: \nrelated: []\nstatus: draft\n---\n\n"
        "## 现象\n\nold\n\n## 历史案例\n\n- old-case\n"
    )
    target.write_text(original, encoding="utf-8")

    result = sink.persist("ts-1", _proposal(
        mode="append_case",
        doc="alpha/system/existing.md",
    ))

    changed = target.read_text(encoding="utf-8")
    case = (
        "- 2026-07-25 · `ts-1` · conclusive · "
        "`records/troubleshoot/ts-1/report.md`"
    )
    assert result.path == target
    assert changed.startswith(original.rstrip() + "\n")
    assert case in changed
    assert "Alpha system memory pressure" in changed
    assert "[[report:ts-1|查看完整排查报告]]" in changed
    assert changed.count("2026-07-25 · `ts-1`") == 1
    assert "2026-07-25" in changed


def test_append_case_stays_inside_history_before_following_section(
        paths, sink):
    target = paths.knowledge_root / "alpha" / "system" / "existing.md"
    target.parent.mkdir(parents=True)
    target.write_text(
        "---\ntitle: Existing\ntype: troubleshoot\nplatform: alpha\n"
        "service: system\nproblem: existing\nsymptoms: []\nalerts: []\n"
        "playbook: \nrelated: []\nstatus: draft\n---\n\n"
        "## 历史案例\n\n- old-case\n\n## 补充说明\n\nkeep me\n",
        encoding="utf-8",
    )

    sink.persist("ts-1", _proposal(
        mode="append_case",
        doc="alpha/system/existing.md",
    ))
    changed = target.read_text(encoding="utf-8")
    assert changed.index("`ts-1`") < changed.index("## 补充说明")
    assert changed.endswith("## 补充说明\n\nkeep me\n")


def test_validation_failure_restores_original_file(paths):
    target = paths.knowledge_root / "alpha" / "system" / "existing.md"
    target.parent.mkdir(parents=True)
    target.write_text(
        "original\n\n## 历史案例\n\n- old-case\n",
        encoding="utf-8",
    )
    original = target.read_text(encoding="utf-8")
    sink = KnowledgeSink(
        paths,
        validate=lambda: (_ for _ in ()).throw(KbSinkError("bad lint")),
        rebuild=lambda: None,
    )

    with pytest.raises(KbSinkError, match="bad lint"):
        sink.persist("ts-1", _proposal(
            mode="append_case",
            doc="alpha/system/existing.md",
        ))

    assert target.read_text(encoding="utf-8") == original


def test_new_file_is_removed_when_rebuild_fails(paths):
    calls = []

    def fail_rebuild():
        calls.append("rebuild")
        if len(calls) == 1:
            raise RuntimeError("build failed")

    sink = KnowledgeSink(paths, validate=lambda: None, rebuild=fail_rebuild)

    with pytest.raises(KbSinkError, match="build failed"):
        sink.persist("ts-1", _proposal())

    target = (
        paths.knowledge_root / "alpha" / "system"
        / "alpha-system-memory-pressure.md"
    )
    assert not target.exists()
    assert calls == ["rebuild", "rebuild"]


def test_append_to_legacy_doc_without_history_section_creates_it(paths, sink):
    target = paths.knowledge_root / "alpha" / "system" / "legacy.md"
    target.parent.mkdir(parents=True)
    target.write_text(
        "---\ntitle: Legacy\ntype: troubleshoot\nplatform: alpha\n"
        "service: system\nproblem: legacy\nsymptoms: []\nalerts: []\n"
        "playbook: \nrelated: []\nstatus: draft\n---\n\n"
        "## 现象\n\nold\n\n## 排查记录\n\n| 日期 | 结论 |\n|---|---|\n",
        encoding="utf-8",
    )

    sink.persist("ts-1", _proposal(
        mode="append_case",
        doc="alpha/system/legacy.md",
    ))

    changed = target.read_text(encoding="utf-8")
    assert "## 历史案例" in changed
    assert "`ts-1`" in changed


def test_evidence_matching_tolerates_whitespace(paths, sink):
    # session fixture 的 stdout 含 "memory actual";引用带多余空白也应命中
    result = sink.persist("ts-1", _proposal(
        evidence=["memory   actual"],
    ))
    assert result.path.exists()


def test_evidence_with_command_context_and_selected_raw_lines_is_accepted(paths, sink):
    session_path = paths.records_root / "ts-1" / "session.json"
    value = json.loads(session_path.read_text(encoding="utf-8"))
    value["steps"].append({
        "cmd": "[node1] jstat -gc 8159 10000 4",
        "stdout": (
            "S0C S1C FGC GCT\n"
            "0.0 196608.0 3 58195.049\n"
            "0.0 196608.0 3 58195.049\n"
        ),
    })
    session_path.write_text(json.dumps(value), encoding="utf-8")

    preview = sink.preview("ts-1", _proposal(evidence=[
        "jstat -gc 8159 10000 4\n"
        "0.0 196608.0 3 58195.049",
        "S0C S1C FGC GCT\n"
        "0.0 196608.0 3 58195.049\n"
        "0.0 196608.0 3 58195.049",
    ]))

    assert "jstat -gc 8159 10000 4" in preview.markdown


def test_root_cause_text_is_not_accepted_as_raw_evidence(paths, sink):
    from lib.triage_agent.errors import KbProposalRejected
    with pytest.raises(KbProposalRejected, match="未记录证据"):
        sink.persist("ts-1", _proposal(evidence=["memory pressure"]))


def test_preview_surfaces_evidence_issues_but_human_approval_can_persist(paths, sink):
    proposal = _proposal(evidence=["memory pressure"])
    preview = sink.preview("ts-1", proposal)

    assert preview.evidence_issues
    assert "memory pressure" in preview.evidence_issues[0]
    result = sink.persist(
        "ts-1",
        proposal,
        allow_incomplete_evidence=True,
    )
    assert result.path.exists()
    assert "证据与结果" in result.path.read_text(encoding="utf-8")


def test_proposal_cannot_invent_evidence_or_escape_knowledge_root(paths, sink):
    from lib.triage_agent.errors import KbProposalRejected

    with pytest.raises(KbProposalRejected, match="未记录证据"):
        sink.persist("ts-1", _proposal(evidence=["invented"]))
    with pytest.raises(KbProposalRejected):
        sink.persist("ts-1", _proposal(
            mode="append_case",
            doc="../../config/model.local.yaml",
        ))


def test_new_doc_and_oplog_take_author_from_session(paths, sink):
    session_path = paths.records_root / "ts-1" / "session.json"
    session = json.loads(session_path.read_text(encoding="utf-8"))
    session_path.write_text(json.dumps({**session, "author": "alice"}), encoding="utf-8")
    text = sink.persist("ts-1", _proposal()).path.read_text(encoding="utf-8")
    assert "author: alice\n" in text
    ref = sink.write_oplog(
        "ts-1", platform="alpha", service="system", problem="2026-07-25-restart-agent",
        title="重启 agent", summary="重启采集 agent。",
        sections={"操作步骤": "1. 重启", "验证过程": "进程存活", "回滚": "无"},
    )
    oplog = paths.knowledge_root / ref.removeprefix("knowledge/")
    assert "author: alice\n" in oplog.read_text(encoding="utf-8")
