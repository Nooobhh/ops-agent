import json
from datetime import datetime

import pytest

from lib.worklog import (
    WorklogEntry,
    _atomic_append,
    _render_entry,
    append_entry,
    append_from_alert,
    append_from_patrol,
    append_from_troubleshoot,
    set_weekly_report_excluded,
)


NOW = datetime(2026, 7, 17, 12, 0)


def entry(source="manual:alice"):
    return WorklogEntry(
        status="DONE", category="OPS", title="测试", source=source,
        output="-", related="-", process=["第一步", "第二步", "第三步"], ts=NOW,
    )


def test_render_entry_matches_skill_template_byte_for_byte():
    assert _render_entry(entry()) == (
        "## [DONE] [OPS] 测试\n"
        "- **来源**: manual:alice\n"
        "- **产出**: -\n"
        "- **关联**: -\n"
        "- **过程**: 1. 第一步  2. 第二步  3. 第三步\n"
    )


def test_render_entry_uses_dash_when_process_is_empty():
    empty_process = entry()
    empty_process.process = []
    assert "- **过程**: -\n" in _render_entry(empty_process)


def test_append_creates_header_then_appends_without_duplicate(tmp_path):
    root = tmp_path / "worklog"
    path = append_entry(entry(), root)
    assert path.read_text(encoding="utf-8").startswith("# 2026-07-17\n\n## [DONE]")
    append_entry(entry("manual:two"), root)
    content = path.read_text(encoding="utf-8")
    assert content.count("# 2026-07-17") == 1
    assert content.count("## [DONE]") == 2


def test_event_key_is_idempotent_but_none_allows_duplicates(tmp_path):
    root = tmp_path / "worklog"
    first = append_entry(entry("auto:xxx"), root, event_key="auto:xxx")
    before = first.read_bytes()
    assert append_entry(entry("auto:xxx"), root, event_key="auto:xxx") is None
    assert first.read_bytes() == before
    append_entry(entry(), root, event_key=None)
    append_entry(entry(), root, event_key=None)
    assert first.read_text(encoding="utf-8").count("manual:alice") == 2


def test_event_key_prefix_does_not_cause_false_idempotency_match(tmp_path):
    root = tmp_path / "worklog"
    node10 = "auto:alert:CPUHigh:node10"
    node1 = "auto:alert:CPUHigh:node1"
    append_entry(entry(node10), root, event_key=node10)
    path = append_entry(entry(node1), root, event_key=node1)
    content = path.read_text(encoding="utf-8")
    assert content.count(f"- **来源**: {node10}\n") == 1
    assert content.count(f"- **来源**: {node1}\n") == 1


def test_weekly_report_exclusion_can_be_toggled_without_changing_entries(tmp_path):
    root = tmp_path / "worklog"
    path = append_entry(entry("manual:first"), root)
    append_entry(entry("manual:second"), root)

    assert set_weekly_report_excluded(path, 1, True) is True
    content = path.read_text(encoding="utf-8")
    assert content.count("- **周报**: 忽略") == 1
    assert content.index("manual:second") < content.index("- **周报**: 忽略")

    assert set_weekly_report_excluded(path, 1, False) is False
    restored = path.read_text(encoding="utf-8")
    assert "- **周报**: 忽略" not in restored
    assert restored.count("## [DONE] [OPS] 测试") == 2


def test_weekly_report_exclusion_rejects_missing_entry(tmp_path):
    path = append_entry(entry(), tmp_path / "worklog")
    with pytest.raises(IndexError, match="条目不存在"):
        set_weekly_report_excluded(path, 4, True)


def write_session(path, verdict, approved=False):
    path.mkdir(parents=True)
    (path / "session.json").write_text(json.dumps({
        "run_id": path.name,
        "trigger": "manual:检查内存",
        "verdict": verdict,
        "steps": [{"approved": approved}],
        "root_cause": "swap 残留",
        "evidence": ["swap 100%", "si=0", "so=0"],
        "playbook_results": [{"playbook": "playbooks/check_memory.sh"}],
    }), encoding="utf-8")


@pytest.mark.parametrize("verdict,approved,heading", [
    ("conclusive", False, "[DONE] [OPS]"),
    ("conclusive", True, "[DONE] [BUGFIX]"),
    ("needs_llm", False, "[WIP] [RESEARCH]"),
])
def test_troubleshoot_mapping(tmp_path, monkeypatch, verdict, approved, heading):
    monkeypatch.chdir(tmp_path)
    session = tmp_path / "records" / "troubleshoot" / f"run-{verdict}-{approved}"
    write_session(session, verdict, approved)
    path = append_from_troubleshoot(session, ts=NOW)
    assert heading in path.read_text(encoding="utf-8")


def test_unfinalized_troubleshoot_does_not_emit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    session = tmp_path / "session"
    write_session(session, None)
    assert append_from_troubleshoot(session, ts=NOW) is None


@pytest.mark.parametrize("with_error,expected", [(False, "2 台通过 / 0 台异常"), (True, "1 台通过 / 1 台异常")])
def test_patrol_always_done_ops_and_counts_nodes(tmp_path, monkeypatch, with_error, expected):
    monkeypatch.chdir(tmp_path)
    snapshot = tmp_path / "patrol" / "snapshot.json"
    snapshot.parent.mkdir()
    nodes = {
        "n1": {"verdict": "conclusive", "severity": "ok"},
        "n2": {"error": "boom"} if with_error else {"verdict": "conclusive", "severity": "ok"},
    }
    snapshot.write_text(json.dumps({
        "run_id": f"patrol-{with_error}", "platform": "alpha",
        "playbook": "playbooks/patrol_health.sh", "nodes": nodes,
    }), encoding="utf-8")
    path = append_from_patrol(snapshot, ts=NOW)
    content = path.read_text(encoding="utf-8")
    assert "[DONE] [OPS] 巡检 alpha patrol_health" in content
    assert expected in content


@pytest.mark.parametrize("status", ["done", "failed"])
def test_alert_done_and_failed_are_done_ops(tmp_path, monkeypatch, status):
    monkeypatch.chdir(tmp_path)
    outcome = {"status": status, "run_id": f"run-{status}", "matched_doc": "knowledge/a.md"}
    event = {"alertname": "CPUHigh", "host": "node1", "source": "prometheus", "severity": "P1"}
    path = append_from_alert(outcome, event, ts=NOW)
    assert "[DONE] [OPS] 告警采证 CPUHigh @ node1" in path.read_text(encoding="utf-8")


def test_alert_accepts_triage_run_id_alias(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    outcome = {"triage_run_id": "alias-run", "matched_doc": "knowledge/a.md"}
    event = {"alertname": "CPUHigh", "host": "node1", "source": "prometheus", "severity": "P1"}
    path = append_from_alert(outcome, event, ts=NOW)
    assert "auto:alert:alias-run" in path.read_text(encoding="utf-8")


def test_alert_frozen_outcome_uses_explicit_worklog_root(tmp_path):
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class FrozenOutcome:
        run_id: str
        matched_doc: str
        playbook: str
        conclusion_hint: str

    outcome = FrozenOutcome("frozen-run", "knowledge/a.md", "playbooks/a.sh", "ok")
    event = {"alertname": "CPUHigh", "host": "node1", "source": "prometheus", "severity": "P1"}
    root = tmp_path / "explicit-worklog"
    path = append_from_alert(outcome, event, worklog_root=root, ts=NOW)
    assert path.parent == root
    assert "auto:alert:frozen-run" in path.read_text(encoding="utf-8")


def test_missing_or_corrupt_hook_input_warns(tmp_path, capsys):
    assert append_from_troubleshoot(tmp_path / "missing", ts=NOW) is None
    assert "WARN worklog" in capsys.readouterr().err
    bad = tmp_path / "bad.json"
    bad.write_text("{bad", encoding="utf-8")
    assert append_from_patrol(bad, ts=NOW) is None
    assert "WARN worklog" in capsys.readouterr().err


def test_atomic_replace_failure_removes_temp_and_keeps_original(tmp_path, monkeypatch):
    path = tmp_path / "2026-07-17.md"
    path.write_text("# original\n", encoding="utf-8")
    original = path.read_bytes()
    monkeypatch.setattr("lib.atomic_file.os.replace", lambda *args: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        _atomic_append(path, "new", "header")
    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".2026-07-17.md.*.tmp"))
