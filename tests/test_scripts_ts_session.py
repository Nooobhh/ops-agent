import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import ts_session


def test_start_creates_session(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = ts_session.run(["start", "--run-id", "ts-test-0001", "--node", "node12",
                         "--trigger", "alert:内存告警", "--platform", "alpha",
                         "--service", "system"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["run_id"] == "ts-test-0001"
    data = json.loads(Path("records/troubleshoot/ts-test-0001/session.json")
                      .read_text(encoding="utf-8"))
    assert data["node"] == "node12" and data["verdict"] is None


def test_start_autogenerates_run_id(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = ts_session.run(["start", "--node", "n1", "--trigger", "manual:x",
                         "--platform", "alpha"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["run_id"].startswith("ts-")


def test_start_survey_with_node_set(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = ts_session.run([
        "start", "--run-id", "ts-survey", "--type", "survey",
        "--nodes", "node1,node3,node16", "--trigger", "survey:memory",
        "--platform", "alpha", "--service", "yarn",
    ])
    out = json.loads(capsys.readouterr().out)
    data = json.loads(Path(out["dir"], "session.json").read_text(encoding="utf-8"))

    assert rc == 0
    assert data["session_type"] == "survey"
    assert data["nodes"] == ["node1", "node3", "node16"]
    assert data["node"] == "node1"


@pytest.mark.parametrize("node_args", [[], ["--node", "n1", "--nodes", "n1,n2"]])
def test_start_requires_exactly_one_node_form(node_args, capsys):
    with pytest.raises(SystemExit) as exc:
        ts_session.run([
            "start", "--run-id", "ts-nodes", *node_args,
            "--trigger", "manual:x", "--platform", "alpha",
        ])

    assert exc.value.code == 2
    assert capsys.readouterr().out == ""


def test_start_rejects_empty_nodes(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    rc = ts_session.run([
        "start", "--run-id", "ts-empty-nodes", "--nodes", " , ",
        "--trigger", "manual:x", "--platform", "alpha",
    ])
    out = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert out["error"] == "bad_nodes"
    assert not Path("records/troubleshoot/ts-empty-nodes").exists()


def test_start_rejects_duplicate(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    args = ["start", "--run-id", "ts-dup", "--node", "n1",
            "--trigger", "t", "--platform", "alpha"]
    assert ts_session.run(args) == 0
    capsys.readouterr()
    rc = ts_session.run(args)
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "exists"


def test_status_reads_back(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    ts_session.run(["start", "--run-id", "ts-s", "--node", "n1",
                    "--trigger", "t", "--platform", "alpha"])
    capsys.readouterr()
    rc = ts_session.run(["status", "--run-id", "ts-s"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["verdict"] is None and out["steps"] == 0


def test_status_corrupt_session_is_config_error(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    d = Path("records/troubleshoot/ts-bad")
    d.mkdir(parents=True)
    (d / "session.json").write_text("[]", encoding="utf-8")
    rc = ts_session.run(["status", "--run-id", "ts-bad"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "config_error"


def test_status_missing_session(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = ts_session.run(["status", "--run-id", "ts-nope"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "not_found"
