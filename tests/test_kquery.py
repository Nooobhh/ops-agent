import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import kquery
import ts_session


def _out(capsys):
    return json.loads(capsys.readouterr().out)


def test_multi_node_cmd(tmp_path, monkeypatch, capsys, fixture_pool):
    monkeypatch.chdir(tmp_path)
    rc = kquery.run(["--platform", "alpha", "--nodes", "node3,node4",
                     "--cmd", "ls /opt/app"], pool=fixture_pool)
    out = _out(capsys)
    assert rc == 0 and out["ok"] is True
    assert [r["node"] for r in out["results"]] == ["node3", "node4"]
    assert all(r["exit_code"] == 0 for r in out["results"])


def test_sql_mode_is_gone_mysql_goes_through_the_plugin(tmp_path, monkeypatch, capsys, fixture_pool):
    monkeypatch.chdir(tmp_path)
    import pytest
    with pytest.raises(SystemExit):
        kquery.run(["--platform", "alpha", "--nodes", "node1", "--sql", "SELECT 1"], pool=fixture_pool)


def test_write_cmd_denied(tmp_path, monkeypatch, capsys, fixture_pool):
    monkeypatch.chdir(tmp_path)
    rc = kquery.run(["--platform", "alpha", "--nodes", "node1",
                     "--cmd", "rm /tmp/x"], pool=fixture_pool)
    out = _out(capsys)
    assert rc == 1 and out["error"] == "write_denied"


def test_run_id_records_steps(tmp_path, monkeypatch, capsys, fixture_pool):
    monkeypatch.chdir(tmp_path)
    ts_session.run(["start", "--run-id", "ts-k", "--node", "n1",
                    "--trigger", "manual:调查", "--platform", "alpha"])
    capsys.readouterr()
    rc = kquery.run(["--platform", "alpha", "--nodes", "node3,node4",
                     "--cmd", "free -m", "--run-id", "ts-k"], pool=fixture_pool)
    assert rc == 0
    steps = json.loads(Path("records/troubleshoot/ts-k/session.json")
                       .read_text(encoding="utf-8"))["steps"]
    assert [s["cmd"] for s in steps] == ["[node3] free -m", "[node4] free -m"]


def test_node_failure_does_not_break_others(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    class HalfDeadPool:
        def run(self, node, cmd, timeout=30.0, approved=False):
            from lib.ssh_pool import CmdResult
            if node == "node3":
                raise RuntimeError("connect timeout")
            return CmdResult(node, cmd, 0, "OK", "", 0.1, timed_out=False)
        def close(self):
            pass

    rc = kquery.run(["--platform", "alpha", "--nodes", "node3,node4",
                     "--cmd", "free -m"], pool=HalfDeadPool())
    out = _out(capsys)
    assert rc == 0
    assert out["results"][0]["error"] == "exec_error"
    assert out["results"][1]["exit_code"] == 0
