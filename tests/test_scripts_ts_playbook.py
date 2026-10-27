import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import ts_playbook
import ts_session

from tests.conftest import FixturePool

PB = "playbooks/alpha/system/check_memory_pressure.sh"
ROUTING = {"alerts": {"alpha": {"内存告警": {"doc": "d.md", "playbook": PB,
                                   "platform": "alpha", "service": "system"}}}, "docs": []}


def _setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "build").mkdir()
    (tmp_path / "build/routing.json").write_text(
        json.dumps(ROUTING, ensure_ascii=False), encoding="utf-8")
    pb = tmp_path / PB
    pb.parent.mkdir(parents=True)
    pb.write_text("#!/bin/bash\n", encoding="utf-8")
    ts_session.run(["start", "--run-id", "ts-pb", "--node", "node12",
                    "--trigger", "t", "--platform", "alpha"])


def test_runs_registered_playbook(tmp_path, monkeypatch, capsys, fixture_pool):
    _setup(tmp_path, monkeypatch)
    capsys.readouterr()
    rc = ts_playbook.run(["--run-id", "ts-pb", "--node", "node12",
                          "--playbook", PB], pool=fixture_pool)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["verdict"] == "conclusive"
    data = json.loads(Path("records/troubleshoot/ts-pb/session.json")
                      .read_text(encoding="utf-8"))
    assert len(data["playbook_results"]) == 1


def test_rejects_unregistered_path(tmp_path, monkeypatch, capsys, fixture_pool):
    _setup(tmp_path, monkeypatch)
    capsys.readouterr()
    rc = ts_playbook.run(["--run-id", "ts-pb", "--node", "node12",
                          "--playbook", "playbooks/evil.sh"], pool=fixture_pool)
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "unregistered_playbook"
    assert fixture_pool.cmds == []


def test_requires_session(tmp_path, monkeypatch, capsys, fixture_pool):
    _setup(tmp_path, monkeypatch)
    capsys.readouterr()
    rc = ts_playbook.run(["--run-id", "ts-nope", "--node", "node12",
                          "--playbook", PB], pool=fixture_pool)
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "no_session"


def test_non_json_playbook_output_is_execution_error(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    capsys.readouterr()
    rc = ts_playbook.run(["--run-id", "ts-pb", "--node", "node12",
                          "--playbook", PB], pool=FixturePool(playbook_stdout="NOT JSON"))
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "playbook_execution_error"


def test_unknown_node_is_execution_error(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    capsys.readouterr()

    class KeyErrorPool:
        def run(self, node, cmd, timeout=180.0, approved=False):
            raise KeyError(node)
        def push(self, node, local, remote):
            raise KeyError(node)
        def close(self):
            pass

    rc = ts_playbook.run(["--run-id", "ts-pb", "--node", "node99",
                          "--playbook", PB], pool=KeyErrorPool())
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "playbook_execution_error"


def test_record_failure_keeps_result_in_json(tmp_path, monkeypatch, capsys, fixture_pool):
    _setup(tmp_path, monkeypatch)
    capsys.readouterr()
    (Path("records/troubleshoot/ts-pb") / "session.json").write_text(
        "{corrupt", encoding="utf-8")
    rc = ts_playbook.run(["--run-id", "ts-pb", "--node", "node12",
                          "--playbook", PB], pool=fixture_pool)
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "record_error"
    assert out["verdict"] == "conclusive"          # 已执行结果不因落档失败丢失
