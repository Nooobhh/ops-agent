"""巡检「通过」判定:不看 verdict,只看有无错误 / 失败步骤 / severity。"""
import json

import pytest

from lib.playbook import patrol_node_healthy
from lib.worklog import append_from_patrol

OK_STEPS = [{"name": "load", "pass": True}]


def test_needs_llm_with_ok_severity_counts_as_healthy():
    """巡检 playbook 一律 emit needs_llm,若据此判健康则通过数恒为 0。"""
    assert patrol_node_healthy({"verdict": "needs_llm", "severity": "ok", "steps": OK_STEPS})


def test_conclusive_still_healthy():
    assert patrol_node_healthy({"verdict": "conclusive", "severity": "ok", "steps": OK_STEPS})


@pytest.mark.parametrize("result", [
    {"error": "SSHException: Channel closed"},
    {"verdict": "needs_llm", "severity": "P1", "steps": OK_STEPS},
    {"verdict": "needs_llm", "severity": "ok",
     "steps": [{"name": "dn", "pass": False}]},
])
def test_unhealthy_cases(result):
    assert not patrol_node_healthy(result)


def test_skipped_step_does_not_break_health():
    """step_opt 跳过的步骤 pass=true,不该拖累通过判定。"""
    assert patrol_node_healthy({
        "verdict": "needs_llm", "severity": "ok",
        "steps": [{"name": "dn", "pass": True, "actual": "not-deployed:DATANODE"}],
    })


def test_worklog_counts_healthy_needs_llm_nodes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    snapshot = tmp_path / "records" / "patrol" / "patrol-x" / "snapshot.json"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(json.dumps({
        "run_id": "patrol-x", "platform": "alpha", "playbook": "p/patrol_node_health.sh",
        "nodes": {
            "node1": {"verdict": "needs_llm", "severity": "ok", "steps": OK_STEPS},
            "node2": {"verdict": "needs_llm", "severity": "ok", "steps": OK_STEPS},
            "node3": {"error": "PushTimeout: x"},
        },
    }), encoding="utf-8")

    append_from_patrol(snapshot)
    content = next((tmp_path / "records" / "worklog").glob("*.md")).read_text(encoding="utf-8")
    assert "2 台通过 / 1 台异常" in content
