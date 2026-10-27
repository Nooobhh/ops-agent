import json
import sys
from pathlib import Path
from types import SimpleNamespace

from scripts import patrol_runner


def test_patrol_main_emits_idempotent_worklog(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    snapshot = tmp_path / "records" / "patrol" / "patrol-hook" / "snapshot.json"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(json.dumps({
        "run_id": "patrol-hook", "platform": "alpha",
        "playbook": "playbooks/patrol_health.sh",
        "nodes": {"node1": {"verdict": "conclusive", "severity": "ok"}},
    }), encoding="utf-8")
    summary = snapshot.with_name("summary.md")
    summary.write_text("# summary\n", encoding="utf-8")
    result = patrol_runner.PatrolResult(snapshot, summary)
    monkeypatch.setattr(patrol_runner, "run_patrol_once", lambda **kwargs: result)
    monkeypatch.setattr("lib.config.load_platform", lambda path: SimpleNamespace(nodes={"node1": object()}))
    monkeypatch.setattr("lib.config.load_local_dev", lambda path: None)
    credential_calls = []

    def build_credentials(path, **kwargs):
        credential_calls.append((path, kwargs))
        return object()

    monkeypatch.setattr("lib.credentials.FileCredentials", build_credentials)
    pool = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr("lib.ssh_pool.SSHPool", lambda *args, **kwargs: pool)
    monkeypatch.setattr(sys, "argv", [
        "patrol_runner", "--playbook", "playbooks/patrol_health.sh",
        "--run-id", "patrol-hook", "--nodes", "node1",
    ])
    assert patrol_runner.main() == 0
    captured = capsys.readouterr()
    assert "snapshot:" in captured.out
    assert credential_calls[-1][1] == {"merge_home": True}
    assert patrol_runner.main() == 0
    capsys.readouterr()
    worklog = next((tmp_path / "records" / "worklog").glob("*.md"))
    content = worklog.read_text(encoding="utf-8")
    assert content.count("auto:patrol:patrol-hook") == 1
    assert "[DONE] [OPS] 巡检 alpha patrol_health" in content


def test_patrol_hook_failure_does_not_change_stdout(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text("{}", encoding="utf-8")
    result = patrol_runner.PatrolResult(snapshot, tmp_path / "summary.md")
    monkeypatch.setattr(patrol_runner, "run_patrol_once", lambda **kwargs: result)
    monkeypatch.setattr("lib.config.load_platform", lambda path: SimpleNamespace(nodes={}))
    monkeypatch.setattr("lib.config.load_local_dev", lambda path: None)
    monkeypatch.setattr("lib.credentials.FileCredentials", lambda path, **kwargs: object())
    monkeypatch.setattr("lib.ssh_pool.SSHPool", lambda *args, **kwargs: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr("lib.worklog.append_from_patrol", lambda path: (_ for _ in ()).throw(OSError("boom")))
    monkeypatch.setattr(sys, "argv", [
        "patrol_runner", "--playbook", "playbooks/patrol.sh", "--run-id", "patrol-hook",
    ])
    assert patrol_runner.main() == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("snapshot:")
    assert "WARN worklog" in captured.err
