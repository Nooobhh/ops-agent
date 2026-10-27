import json
from pathlib import Path

from lib.alerts.model import AlertEvent
from lib.alerts.triage_runner import run_auto_triage
from lib.record import start_session
from scripts import ts_conclude


CONCLUSION = {
    "verdict": "conclusive",
    "severity": "P1",
    "root_cause": "swap 残留",
    "evidence": ["swap 100%", "si=0", "so=0"],
    "remediation": ["低峰处理"],
}


def setup_conclusion(tmp_path):
    session = start_session(
        tmp_path / "records" / "troubleshoot",
        run_id="ts-hook", node="node1", trigger="manual:检查内存",
        platform="alpha", service="system",
    )
    artifact = session.dir / "artifacts" / "conclusion.json"
    artifact.parent.mkdir()
    artifact.write_text(json.dumps(CONCLUSION), encoding="utf-8")
    return artifact


def test_ts_conclude_emits_once_and_keeps_stdout_json(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    artifact = setup_conclusion(tmp_path)
    args = ["--run-id", "ts-hook", "--conclusion-json-file", str(artifact)]
    assert ts_conclude.run(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["ok"] is True
    assert ts_conclude.run(args) == 0
    json.loads(capsys.readouterr().out)
    worklog = next((tmp_path / "records" / "worklog").glob("*.md"))
    content = worklog.read_text(encoding="utf-8")
    assert content.count("auto:troubleshoot:ts-hook") == 1
    assert "[DONE] [OPS]" in content


def test_ts_conclude_hook_failure_is_non_blocking(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    artifact = setup_conclusion(tmp_path)
    monkeypatch.setattr(
        "lib.worklog.append_from_troubleshoot",
        lambda *args: (_ for _ in ()).throw(OSError("disk full")),
    )
    assert ts_conclude.run([
        "--run-id", "ts-hook", "--conclusion-json-file", str(artifact),
    ]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["ok"] is True
    assert "WARN worklog" in captured.err


class FakeResult:
    def to_session_dict(self):
        return {
            "playbook": "playbooks/check.sh", "verdict": "conclusive",
            "severity": "P1", "conclusion_hint": "root cause", "steps": [],
        }


def alert_event():
    return AlertEvent(
        source="prometheus", platform="alpha", alertname="CPUHigh", severity="P1",
        host="node1", host_raw="node1", service="system", state="firing", detail="hot",
    )


def alert_routing():
    return {
        "alerts": {"alpha": {"CPUHigh": {"doc": "knowledge/a.md", "playbook": "playbooks/check.sh", "platform": "alpha", "service": "system"}}},
        "docs": [{"path": "knowledge/a.md", "playbook": "playbooks/check.sh"}],
    }


def test_alert_triage_success_and_failure_each_emit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("lib.alerts.triage_runner.run_playbook", lambda *args, **kwargs: FakeResult())
    assert run_auto_triage(alert_event(), alert_routing(), object()).status == "done"
    monkeypatch.setattr(
        "lib.alerts.triage_runner.run_playbook",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert run_auto_triage(alert_event(), alert_routing(), object()).status == "failed"
    worklog = next((tmp_path / "records" / "worklog").glob("*.md"))
    content = worklog.read_text(encoding="utf-8")
    assert content.count("[DONE] [OPS] 告警采证") == 2
