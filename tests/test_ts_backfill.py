import importlib
import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from lib.record import lint_records
from scripts import ts_conclude, ts_session


def _backfill_module():
    try:
        return importlib.import_module("scripts.ts_backfill")
    except ModuleNotFoundError:
        pytest.fail("scripts.ts_backfill 尚未实现")


def _manifest(**updates):
    value = {
        "session_type": "survey",
        "platform": "alpha",
        "service": "yarn",
        "node": "node1",
        "nodes": ["node1", "node3"],
        "trigger": "survey:alpha yarn 内存全景(事后补录)",
        "steps": [{
            "cmd": "free -m", "exit_code": 0,
            "stdout": "MemAvailable: 2048 MB\n", "stderr": "",
            "approved": False,
        }],
    }
    value.update(updates)
    return value


def _write_manifest(tmp_path, value):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def _run_backfill(tmp_path, monkeypatch, capsys, value):
    monkeypatch.chdir(tmp_path)
    manifest = _write_manifest(tmp_path, value)
    rc = _backfill_module().run(["--manifest", str(manifest)])
    captured = capsys.readouterr()
    return rc, json.loads(captured.out), captured


def test_valid_survey_manifest_creates_open_lint_clean_session(
        tmp_path, monkeypatch, capsys):
    rc, out, captured = _run_backfill(
        tmp_path, monkeypatch, capsys, _manifest(),
    )

    assert rc == 0 and out["ok"] is True
    assert re.fullmatch(r"bf-\d{8}-\d{4}-[0-9a-f]{4}", out["run_id"])
    assert out["steps"] == 1
    assert "ts_conclude.py" in out["next"] and "kb_import.py" in out["next"]
    assert captured.out.count("\n") == 1

    session_path = Path(out["dir"]) / "session.json"
    data = json.loads(session_path.read_text(encoding="utf-8"))
    assert data["session_type"] == "survey"
    assert data["nodes"] == ["node1", "node3"]
    assert data["node"] == "node1"
    assert len(data["steps"]) == 1
    assert data["verdict"] is None
    assert lint_records(tmp_path / "records/troubleshoot") == []

    assert ts_session.run(["status", "--run-id", out["run_id"]]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["verdict"] is None and status["steps"] == 1


def test_backfill_redacts_command_stdout_and_stderr(
        tmp_path, monkeypatch, capsys):
    value = _manifest(steps=[{
        "cmd": "mysql -P3306 -uroot -p'secret123' -e 'select 1'",
        "exit_code": 1,
        "stdout": "password=abc\n",
        "stderr": "token=xyz\n",
    }])

    rc, out, _ = _run_backfill(tmp_path, monkeypatch, capsys, value)
    data = json.loads((Path(out["dir"]) / "session.json").read_text(encoding="utf-8"))
    persisted = json.dumps(data["steps"], ensure_ascii=False)

    assert rc == 0
    assert "secret123" not in persisted
    assert "password=abc" not in persisted
    assert "token=xyz" not in persisted
    assert "***REDACTED***" in persisted
    assert "-P3306" in data["steps"][0]["cmd"]


@pytest.mark.parametrize("value, field", [
    (_manifest(platform="bad"), "platform"),
    (_manifest(steps=[]), "steps"),
    (_manifest(steps=[{"exit_code": 0}]), "steps[0].cmd"),
    (_manifest(node="node2"), "node"),
])
def test_invalid_manifest_leaves_no_records(
        tmp_path, monkeypatch, capsys, value, field):
    rc, out, captured = _run_backfill(tmp_path, monkeypatch, capsys, value)

    assert rc == 1 and out["error"] == "bad_manifest"
    assert field in out["message"]
    assert captured.out.count("\n") == 1
    assert not (tmp_path / "records").exists()


def test_manifest_defaults_optional_step_fields(tmp_path, monkeypatch, capsys):
    value = {
        "platform": "beta", "node": "node1", "trigger": "manual:disk survey",
        "steps": [{"cmd": "df -h", "exit_code": 0}],
    }

    rc, out, _ = _run_backfill(tmp_path, monkeypatch, capsys, value)
    data = json.loads((Path(out["dir"]) / "session.json").read_text(encoding="utf-8"))

    assert rc == 0
    assert "session_type" not in data and "nodes" not in data
    assert data["service"] == ""
    assert data["steps"][0]["stdout"] == ""
    assert data["steps"][0]["stderr"] == ""
    assert data["steps"][0]["approved"] is False


def test_existing_generated_run_id_is_rejected(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    module = _backfill_module()
    monkeypatch.setattr(module, "_new_run_id", lambda: "bf-20260729-1200-abcd")
    manifest = _write_manifest(tmp_path, _manifest())
    args = ["--manifest", str(manifest)]

    assert module.run(args) == 0
    capsys.readouterr()
    assert module.run(args) == 1
    out = json.loads(capsys.readouterr().out)

    assert out["error"] == "exists"


def test_manifest_commands_are_recorded_without_execution(
        tmp_path, monkeypatch, capsys):
    marker = tmp_path / "must-not-exist"
    value = _manifest(steps=[{
        "cmd": f"touch {marker}", "exit_code": 0, "stdout": "recorded",
    }])

    rc, _, _ = _run_backfill(tmp_path, monkeypatch, capsys, value)

    assert rc == 0
    assert not marker.exists()


def test_backfill_conclude_end_to_end_does_not_write_knowledge(
        tmp_path, monkeypatch, capsys):
    rc, out, _ = _run_backfill(
        tmp_path, monkeypatch, capsys, _manifest(),
    )
    assert rc == 0
    run_id = out["run_id"]

    conclusion = {
        "verdict": "conclusive", "severity": "P2",
        "root_cause": "cluster memory inventory complete",
        "evidence": ["MemAvailable: 2048 MB"],
        "remediation": ["continue observation"],
    }
    conclusion_path = tmp_path / "conclusion.json"
    conclusion_path.write_text(json.dumps(conclusion), encoding="utf-8")

    assert ts_conclude.run([
        "--run-id", run_id, "--conclusion-json-file", str(conclusion_path),
    ]) == 0
    conclude_out = json.loads(capsys.readouterr().out)

    assert conclude_out["ok"] is True
    assert Path(conclude_out["report"]).exists()
    assert not (tmp_path / "knowledge").exists()
