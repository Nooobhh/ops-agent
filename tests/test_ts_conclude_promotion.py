"""Knowledge promotion is intentionally outside the CLI conclude path."""
import json

from lib.record import start_session
from scripts.ts_conclude import run


def test_conclude_rejects_the_old_auto_promotion_contract(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    session = start_session(
        tmp_path / "records/troubleshoot", run_id="ts-1", node="node1",
        trigger="demo", platform="alpha", service="system",
    )
    session.finalize("conclusive", "P2", "root", ["evidence"], ["observe"])
    conclusion = tmp_path / "conclusion.json"
    conclusion.write_text(json.dumps({
        "verdict": "conclusive", "severity": "P2", "root_cause": "root",
        "evidence": ["evidence"], "remediation": ["observe"],
        "kb_doc": "alpha/system/demo.md", "kb_doc_effective": True,
    }), encoding="utf-8")
    rc = run(["--run-id", "ts-1", "--conclusion-json-file", str(conclusion)])
    output = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert output["error"] == "knowledge_entry_closed"
