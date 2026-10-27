import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import ts_conclude
import ts_exec
import ts_session


GOOD = {
    "verdict": "conclusive", "severity": "P1",
    "root_cause": "慢性 swap 残留",
    "evidence": ["swap 100% 且 si/so≈0"],
    "remediation": ["低峰 swapoff(先降载)"],
}


def _setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ts_session.run([
        "start", "--run-id", "ts-c", "--node", "n1",
        "--trigger", "t", "--platform", "alpha",
    ])


def _conclusion_file(tmp_path, data):
    folder = tmp_path / "records/troubleshoot/ts-c/artifacts"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "conclusion.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _run(tmp_path, monkeypatch, capsys, data):
    _setup(tmp_path, monkeypatch)
    path = _conclusion_file(tmp_path, data)
    capsys.readouterr()
    rc = ts_conclude.run(["--run-id", "ts-c", "--conclusion-json-file", path])
    return rc, json.loads(capsys.readouterr().out)


def test_finalize_renders_report_without_writing_knowledge(tmp_path, monkeypatch, capsys):
    rc, output = _run(tmp_path, monkeypatch, capsys, GOOD)
    assert rc == 0 and output["ok"] is True
    assert Path(output["report"]).is_file()
    assert not (tmp_path / "knowledge").exists()


@pytest.mark.parametrize("field,value", [
    ("kb_doc", "alpha/system/x.md"),
    ("kb_draft", {"doc": "alpha/system/x.md"}),
    ("kb_doc_effective", True),
])
def test_legacy_knowledge_fields_are_rejected(
        tmp_path, monkeypatch, capsys, field, value):
    rc, output = _run(tmp_path, monkeypatch, capsys, {**GOOD, field: value})
    assert rc == 1
    assert output["error"] == "knowledge_entry_closed"


def test_bad_verdict_rejected(tmp_path, monkeypatch, capsys):
    rc, output = _run(tmp_path, monkeypatch, capsys, {**GOOD, "verdict": "maybe"})
    assert rc == 1 and output["error"] == "bad_verdict"


def test_missing_keys_rejected(tmp_path, monkeypatch, capsys):
    rc, output = _run(tmp_path, monkeypatch, capsys, {"verdict": "conclusive"})
    assert rc == 1 and output["error"] == "missing_keys"


def test_non_dict_conclusion_rejected(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    path = _conclusion_file(tmp_path, [])
    capsys.readouterr()
    rc = ts_conclude.run(["--run-id", "ts-c", "--conclusion-json-file", path])
    output = json.loads(capsys.readouterr().out)
    assert rc == 1 and output["error"] == "bad_conclusion_file"


def test_string_evidence_rejected(tmp_path, monkeypatch, capsys):
    rc, output = _run(tmp_path, monkeypatch, capsys, {**GOOD, "evidence": "abc"})
    assert rc == 1 and output["error"] == "bad_conclusion_file"


def test_invalid_utf8_conclusion_rejected(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    folder = tmp_path / "records/troubleshoot/ts-c/artifacts"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "conclusion.json"
    path.write_bytes(b"\xff\xfe{bad}")
    capsys.readouterr()
    rc = ts_conclude.run(["--run-id", "ts-c", "--conclusion-json-file", str(path)])
    output = json.loads(capsys.readouterr().out)
    assert rc == 1 and output["error"] == "bad_conclusion_file"


def test_oplog_gate_still_blocks_approved_write(
        tmp_path, monkeypatch, capsys, fixture_pool):
    _setup(tmp_path, monkeypatch)
    ts_exec.run([
        "--run-id", "ts-c", "--node", "n1",
        "--cmd", "swapoff -a", "--approved",
    ], pool=fixture_pool)
    path = _conclusion_file(tmp_path, GOOD)
    capsys.readouterr()
    rc = ts_conclude.run(["--run-id", "ts-c", "--conclusion-json-file", path])
    output = json.loads(capsys.readouterr().out)
    assert rc == 1 and output["error"] == "oplog_missing"


def test_corrupt_session_is_record_error(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    path = _conclusion_file(tmp_path, GOOD)
    (tmp_path / "records/troubleshoot/ts-c/session.json").write_text(
        "{corrupt", encoding="utf-8",
    )
    capsys.readouterr()
    rc = ts_conclude.run(["--run-id", "ts-c", "--conclusion-json-file", path])
    output = json.loads(capsys.readouterr().out)
    assert rc == 1 and output["error"] == "record_error"


def test_insufficient_remains_allowed(tmp_path, monkeypatch, capsys):
    value = {**GOOD, "verdict": "insufficient", "evidence": []}
    rc, output = _run(tmp_path, monkeypatch, capsys, value)
    assert rc == 0 and output["ok"] is True
