import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import ts_exec
import ts_session


def _setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ts_session.run(["start", "--run-id", "ts-x", "--node", "n1",
                    "--trigger", "t", "--platform", "alpha"])


def _steps():
    return json.loads(Path("records/troubleshoot/ts-x/session.json")
                      .read_text(encoding="utf-8"))["steps"]


def test_readonly_cmd_runs_and_records(tmp_path, monkeypatch, capsys, fixture_pool):
    _setup(tmp_path, monkeypatch)
    capsys.readouterr()
    rc = ts_exec.run(["--run-id", "ts-x", "--node", "n1", "--cmd", "free -m"],
                     pool=fixture_pool)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["exit_code"] == 0 and out["stdout"] == "OK"
    assert _steps()[0]["cmd"] == "free -m" and _steps()[0]["approved"] is False


def test_write_cmd_needs_approval(tmp_path, monkeypatch, capsys, fixture_pool):
    _setup(tmp_path, monkeypatch)
    capsys.readouterr()
    rc = ts_exec.run(["--run-id", "ts-x", "--node", "n1",
                      "--cmd", "swapoff -a"], pool=fixture_pool)
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "write_needs_approval"
    assert _steps() == []          # 被拒命令不入档


def test_write_cmd_approved_runs(tmp_path, monkeypatch, capsys, fixture_pool):
    _setup(tmp_path, monkeypatch)
    capsys.readouterr()
    rc = ts_exec.run(["--run-id", "ts-x", "--node", "n1",
                      "--cmd", "swapoff -a", "--approved"], pool=fixture_pool)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert _steps()[0]["approved"] is True


def test_forbidden_cmd_denied_even_approved(tmp_path, monkeypatch, capsys, fixture_pool):
    _setup(tmp_path, monkeypatch)
    capsys.readouterr()
    rc = ts_exec.run(["--run-id", "ts-x", "--node", "n1",
                      "--cmd", "rm -rf /", "--approved"], pool=fixture_pool)
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "forbidden"
    assert _steps() == []


def test_pool_runtime_error_is_exec_error(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    capsys.readouterr()

    class DeadPool:
        def run(self, node, cmd, timeout=30.0, approved=False):
            raise RuntimeError("SSH 连接失败: 192.0.2.26")
        def close(self):
            pass

    rc = ts_exec.run(["--run-id", "ts-x", "--node", "n1", "--cmd", "free -m"],
                     pool=DeadPool())
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "exec_error"
    assert _steps() == []


def test_platform_inherited_from_session(tmp_path, monkeypatch, capsys, fixture_pool):
    monkeypatch.chdir(tmp_path)
    ts_session.run(["start", "--run-id", "ts-x", "--node", "n1",
                    "--trigger", "t", "--platform", "beta"])
    seen = {}

    def fake_build_pool(platform_path, local_dev_path):
        seen["platform"] = platform_path
        return fixture_pool

    monkeypatch.setattr(ts_exec, "build_pool", fake_build_pool)
    capsys.readouterr()
    rc = ts_exec.run(["--run-id", "ts-x", "--node", "n1", "--cmd", "free -m"])
    assert rc == 0
    assert seen["platform"] == str(REPO / "envs" / "example" / "config" / "platform-beta.yaml")


def test_secret_redacted_in_record_and_output(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    capsys.readouterr()

    class LeakyPool:
        def run(self, node, cmd, timeout=30.0, approved=False):
            from lib.ssh_pool import CmdResult
            return CmdResult(node, cmd, 0,
                             "host=n5\nmqtt.ak=AK123\nregister.secret_key=sec1", "", 0.1,
                             timed_out=False)
        def close(self):
            pass

    rc = ts_exec.run(["--run-id", "ts-x", "--node", "n1",
                      "--cmd", "cat /opt/app/application.properties"], pool=LeakyPool())
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert "AK123" not in out["stdout"] and "sec1" not in out["stdout"]
    assert "host=n5" in out["stdout"]
    step = _steps()[0]
    assert "AK123" not in step["stdout"] and "sec1" not in step["stdout"]


def test_record_failure_keeps_result_in_json(tmp_path, monkeypatch, capsys, fixture_pool):
    _setup(tmp_path, monkeypatch)
    capsys.readouterr()
    (Path("records/troubleshoot/ts-x") / "session.json").write_text(
        "{corrupt", encoding="utf-8")
    rc = ts_exec.run(["--run-id", "ts-x", "--node", "n1", "--cmd", "free -m"],
                     pool=fixture_pool)
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "record_error"
    assert out["stdout"] == "OK"          # 已执行结果不因落档失败丢失
