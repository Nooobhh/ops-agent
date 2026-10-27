import json
import shutil
from pathlib import Path

from lib.record import (
    REPORT_RENDERER_VERSION, _render_report, lint_records, migrate_legacy_records,
    start_session,
)



def test_root_json_dump_fails_even_with_valid_session(tmp_path):
    records = tmp_path / "records"
    s = start_session(
        records, run_id="r1", node="node12", trigger="manual",
        platform="alpha", service="system",
    )
    s.finalize("conclusive", "P1", "root", ["evidence"], ["fix"])
    (s.dir / "diag.json").write_text("{}", encoding="utf-8")

    errors = lint_records(records)

    assert ("r1", "run 目录根部禁止 session.json 之外的 JSON 文件: diag.json") in errors


def test_handwritten_or_title_only_report_fails(tmp_path):
    records = tmp_path / "records"
    s = start_session(
        records, run_id="r2", node="node12", trigger="manual",
        platform="alpha", service="system",
    )
    s.finalize("conclusive", "P1", "root", ["evidence"], ["fix"])
    report = s.dir / "report.md"
    report.write_text("# 排查报告 · r2\n", encoding="utf-8")

    errors = lint_records(records)

    assert ("r2", "report.md 与 session.json 重渲染不一致") in errors


def test_report_renderer_version_mismatch_has_actionable_error(tmp_path):
    records = tmp_path / "records"
    s = start_session(
        records, run_id="r2b", node="node12", trigger="manual",
        platform="alpha", service="system",
    )
    s.finalize("conclusive", "P1", "root", ["evidence"], ["fix"])
    session_path = s.dir / "session.json"
    data = json.loads(session_path.read_text(encoding="utf-8"))
    assert data["report_renderer_version"] == REPORT_RENDERER_VERSION
    data["report_renderer_version"] = "old-renderer"
    session_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    errors = lint_records(records)

    assert ("r2b", "report 渲染器版本不符,需重渲染迁移") in errors


def test_migrate_legacy_records_adds_schema_fields_without_changing_findings(tmp_path):
    records = tmp_path / "records"
    session = start_session(
        records, run_id="legacy-1", node="node12", trigger="manual:legacy",
        platform="alpha", service="system",
    )
    session.finalize("conclusive", "P1", "root", ["evidence"], ["fix"])
    session_path = records / "legacy-1/session.json"
    data = json.loads(session_path.read_text(encoding="utf-8"))
    data.pop("amend_count")
    data.pop("last_amended_at")
    data.pop("superseded_evidence")
    data["report_renderer_version"] = "record-report-v1"
    session_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    migrated = migrate_legacy_records(records)
    assert migrated == ["legacy-1"]
    data = json.loads((records / "legacy-1/session.json").read_text(encoding="utf-8"))
    assert data["report_renderer_version"] == REPORT_RENDERER_VERSION
    assert data["amend_count"] == 0
    assert data["superseded_evidence"] == []
    assert lint_records(records) == []


def test_artifact_only_staging_directory_is_not_a_run(tmp_path):
    records = tmp_path / "records"
    (records / "survey-staging/artifacts").mkdir(parents=True)
    (records / "survey-staging/artifacts/platform.yaml").write_text(
        "platform: other\n", encoding="utf-8",
    )
    assert lint_records(records) == []


def test_render_report_is_deterministic(tmp_path):
    records = tmp_path / "records"
    s = start_session(
        records, run_id="r3", node="node12", trigger="manual",
        platform="alpha", service="system",
    )
    s.record_deep_dive_step("cat /proc/meminfo", 0, "MemTotal: 1\n")
    s.finalize("conclusive", "P2", "root", ["evidence"], ["fix"])
    data = json.loads((s.dir / "session.json").read_text(encoding="utf-8"))

    assert _render_report(data).encode("utf-8") == _render_report(data).encode("utf-8")


def test_survey_report_shows_type_and_node_set(tmp_path):
    records = tmp_path / "records"
    s = start_session(
        records, run_id="survey-1", node="node1", trigger="survey:memory",
        platform="alpha", service="yarn", session_type="survey",
        nodes=["node1", "node3", "node16"],
    )
    s.finalize("insufficient", "ok", "n/a", [], [])

    report = (s.dir / "report.md").read_text(encoding="utf-8")

    assert "类型: `survey`" in report
    assert "节点集: `node1`, `node3`, `node16`" in report


def test_legacy_report_rendering_remains_byte_for_byte_compatible():
    data = {
        "run_id": "legacy-1", "node": "node12", "trigger": "manual:x",
        "platform": "alpha", "service": "system", "playbook_results": [],
        "steps": [], "verdict": "insufficient", "severity": "ok",
        "root_cause": "现场无异常", "evidence": [], "remediation": ["观察"],
        "oplogs": [], "report_renderer_version": REPORT_RENDERER_VERSION,
    }
    expected = """# 排查报告 · legacy-1

- 节点: `node12`  · 平台/服务: `alpha/system`
- 触发: manual:x
- Verdict: **insufficient** · Severity: ok

## 根因

现场无异常

## 证据


## 处置建议

- 观察
"""

    assert _render_report(data).encode("utf-8") == expected.encode("utf-8")


def test_lint_validates_optional_survey_fields(tmp_path):
    records = tmp_path / "records"
    cases = {
        "bad-type": {"session_type": "bad"},
        "bad-nodes": {"nodes": "node1"},
        "mismatch": {"nodes": ["node2", "node1"]},
    }
    for run_id, changes in cases.items():
        s = start_session(
            records, run_id=run_id, node="node1", trigger="manual:x",
            platform="alpha", service="system",
        )
        path = s.dir / "session.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(changes)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    errors = dict(lint_records(records))

    assert "session_type" in errors["bad-type"]
    assert "nodes 必须是 list" in errors["bad-nodes"]
    assert "nodes[0]" in errors["mismatch"]


def test_lint_accepts_survey_and_legacy_sessions(tmp_path):
    records = tmp_path / "records"
    start_session(
        records, run_id="survey", node="node1", trigger="survey:memory",
        platform="alpha", service="yarn", session_type="survey",
        nodes=["node1", "node3"],
    )
    start_session(
        records, run_id="legacy", node="node1", trigger="manual:x",
        platform="alpha", service="system",
    )

    assert lint_records(records) == []


def test_lint_rejects_non_string_session_type_without_crashing(tmp_path):
    records = tmp_path / "records"
    s = start_session(
        records, run_id="bad-type-shape", node="node1", trigger="manual:x",
        platform="alpha", service="system",
    )
    path = s.dir / "session.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["session_type"] = ["survey"]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    assert lint_records(records) == [
        ("bad-type-shape", "session.json session_type 必须是 triage 或 survey"),
    ]
