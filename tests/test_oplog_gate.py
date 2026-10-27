import json

import pytest

from lib.record import OplogMissing, lint_records, start_session


def test_approved_step_requires_oplog_without_side_effects(tmp_path):
    s = start_session(tmp_path, run_id="r1", node="node12", trigger="manual",
                      platform="alpha", service="system")
    s.record_deep_dive_step("touch /tmp/x", 0, "", approved=True)

    with pytest.raises(OplogMissing):
        s.finalize("conclusive", "P1", "root", ["e"], ["r"])

    data = json.loads((s.dir / "session.json").read_text(encoding="utf-8"))
    assert data["verdict"] is None
    assert not (s.dir / "report.md").exists()


def test_missing_oplog_path_is_rejected(tmp_path):
    s = start_session(tmp_path, run_id="r2", node="node12", trigger="manual",
                      platform="alpha", service="system")
    s.record_deep_dive_step("touch /tmp/x", 0, "", approved=True)

    with pytest.raises(OplogMissing):
        s.finalize("conclusive", "P1", "root", ["e"], ["r"],
                   oplogs=["knowledge/oplog/missing.md"])


def test_existing_oplog_allows_approved_finalize(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    oplog = tmp_path / "knowledge" / "oplog" / "2026" / "change.md"
    oplog.parent.mkdir(parents=True)
    oplog.write_text("---\ntitle: change\n---\n", encoding="utf-8")
    records = tmp_path / "records"
    s = start_session(records, run_id="r3", node="node12", trigger="manual",
                      platform="alpha", service="system")
    s.record_deep_dive_step("touch /tmp/x", 0, "", approved=True)

    s.finalize("conclusive", "P1", "root", ["e"], ["r"],
               oplogs=["knowledge/oplog/2026/change.md"])

    data = json.loads((s.dir / "session.json").read_text(encoding="utf-8"))
    assert data["oplogs"] == ["knowledge/oplog/2026/change.md"]


def test_existing_oplog_resolves_from_repo_root_not_cwd(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    oplog = repo / "knowledge" / "oplog" / "2026" / "change.md"
    oplog.parent.mkdir(parents=True)
    oplog.write_text("---\ntitle: change\n---\n", encoding="utf-8")
    records = repo / "records" / "troubleshoot"
    s = start_session(records, run_id="r3b", node="node12", trigger="manual",
                      platform="alpha", service="system")
    s.record_deep_dive_step("touch /tmp/x", 0, "", approved=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    s.finalize("conclusive", "P1", "root", ["e"], ["r"],
               oplogs=["knowledge/oplog/2026/change.md"])

    data = json.loads((s.dir / "session.json").read_text(encoding="utf-8"))
    assert data["oplogs"] == ["knowledge/oplog/2026/change.md"]


def test_unapproved_steps_do_not_require_oplogs(tmp_path):
    s = start_session(tmp_path, run_id="r4", node="node12", trigger="manual",
                      platform="alpha", service="system")
    s.record_deep_dive_step("cat /proc/meminfo", 0, "")

    s.finalize("conclusive", "P1", "root", ["e"], ["r"])

    data = json.loads((s.dir / "session.json").read_text(encoding="utf-8"))
    assert data["oplogs"] == []


def test_lint_fails_finalized_approved_session_without_oplogs(tmp_path):
    s = start_session(tmp_path, run_id="r5", node="node12", trigger="manual",
                      platform="alpha", service="system")
    data = json.loads((s.dir / "session.json").read_text(encoding="utf-8"))
    data["steps"].append({"cmd": "touch /tmp/x", "exit_code": 0, "stdout": "", "stderr": "", "approved": True})
    data["verdict"] = "conclusive"
    data["severity"] = "P1"
    data["root_cause"] = "root"
    data["evidence"] = ["e"]
    data["remediation"] = ["r"]
    data["oplogs"] = []
    (s.dir / "session.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    errors = lint_records(tmp_path)

    assert ("r5", "approved 步骤已 finalize 时必须记录 oplogs") in errors
