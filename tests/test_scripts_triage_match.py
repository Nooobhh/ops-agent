import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import triage_match

ROUTING = {
    "alerts": {"alpha": {"集群监控-内存告警": {
        "doc": "knowledge/alpha/system/alpha-system-memory-pressure.md",
        "playbook": "playbooks/alpha/system/check_memory_pressure.sh",
        "platform": "alpha", "service": "system"}}},
    "docs": [
        {"path": "knowledge/alpha/yarn/a.md", "title": "vcore 拥塞", "type": "troubleshoot",
         "platform": "alpha", "service": "yarn", "problem": "排队", "symptoms": ["pending 高"],
         "playbook": None, "status": "draft"},
        {"path": "knowledge/alpha/_index.md", "is_index": True, "type": "knowledge",
         "platform": "alpha", "service": ""},
    ],
}


def _routing_file(tmp_path):
    p = tmp_path / "routing.json"
    p.write_text(json.dumps(ROUTING, ensure_ascii=False), encoding="utf-8")
    return str(p)


def test_alert_hit(tmp_path, capsys):
    rc = triage_match.run(["--alert", "集群监控-内存告警", "--platform", "alpha",
                           "--routing", _routing_file(tmp_path)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["hit"]["playbook"] == "playbooks/alpha/system/check_memory_pressure.sh"
    assert out["candidates"] == []


def test_alert_miss_falls_to_candidates(tmp_path, capsys):
    rc = triage_match.run(["--alert", "不存在", "--platform", "alpha", "--service", "yarn",
                           "--routing", _routing_file(tmp_path)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["hit"] is None
    assert [c["path"] for c in out["candidates"]] == ["knowledge/alpha/yarn/a.md"]


def test_no_args_is_error(tmp_path, capsys):
    rc = triage_match.run(["--routing", _routing_file(tmp_path)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["error"] == "bad_args"


def test_alert_without_platform_is_error(tmp_path, capsys):
    rc = triage_match.run(["--alert", "x", "--routing", _routing_file(tmp_path)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["error"] == "bad_args"


def test_missing_routing_is_config_error(tmp_path, capsys):
    rc = triage_match.run(["--alert", "x", "--platform", "alpha",
                           "--routing", str(tmp_path / "nope.json")])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["error"] == "config_error"
