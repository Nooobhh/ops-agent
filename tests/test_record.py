import json
from pathlib import Path

import pytest

from lib.record import REPORT_RENDERER_VERSION, TroubleshootSession, start_session


def test_start_session(tmp_path):
    s = start_session(tmp_path, run_id="ts-20260710-1432-ab3f",
                      node="node12", trigger="alert:集群监控-内存告警",
                      platform="alpha", service="system")
    assert isinstance(s, TroubleshootSession)
    assert s.dir == tmp_path / "ts-20260710-1432-ab3f"
    assert s.dir.exists()
    data = json.loads((s.dir / "session.json").read_text(encoding="utf-8"))
    assert data["run_id"] == "ts-20260710-1432-ab3f"
    assert data["node"] == "node12"
    assert data["trigger"] == "alert:集群监控-内存告警"
    assert data["platform"] == "alpha" and data["service"] == "system"
    assert data["steps"] == []
    assert data["playbook_results"] == []
    assert data["verdict"] is None
    assert data["report_renderer_version"] == REPORT_RENDERER_VERSION
    assert set(data) == {
        "run_id", "node", "trigger", "platform", "service",
        "playbook_results", "steps", "verdict", "severity", "root_cause",
        "evidence", "remediation", "oplogs", "report_renderer_version",
        "amend_count", "last_amended_at", "superseded_evidence",
    }


def test_start_survey_session_records_type_and_nodes(tmp_path):
    s = start_session(
        tmp_path, run_id="survey-1", node="node1", trigger="survey:memory",
        platform="alpha", service="yarn", session_type="survey",
        nodes=["node1", "node3", "node16"],
    )

    data = json.loads((s.dir / "session.json").read_text(encoding="utf-8"))

    assert data["session_type"] == "survey"
    assert data["nodes"] == ["node1", "node3", "node16"]


def test_record_playbook_result(tmp_path):
    s = start_session(tmp_path, run_id="r1", node="node12", trigger="manual",
                      platform="alpha", service="system")
    s.record_playbook_result({
        "playbook": "alpha/system/check_memory_pressure",
        "verdict": "conclusive", "severity": "P1",
        "conclusion_hint": "慢性 swap 残留",
        "steps": [{"name": "swap", "cmd": "free -m", "pass": False,
                   "actual": "swap 100%", "expect": "<50%", "raw": "..."}],
        "metrics": {"mem_pct": 91.2, "si": 0, "so": 0},
    })
    data = json.loads((s.dir / "session.json").read_text(encoding="utf-8"))
    assert len(data["playbook_results"]) == 1
    assert data["playbook_results"][0]["verdict"] == "conclusive"


def test_record_deep_dive_step(tmp_path):
    s = start_session(tmp_path, run_id="r2", node="node12", trigger="manual",
                      platform="alpha", service="system")
    s.record_deep_dive_step(
        cmd="cat /proc/meminfo", exit_code=0,
        stdout="MemTotal: 512000 kB\n", stderr="",
    )
    data = json.loads((s.dir / "session.json").read_text(encoding="utf-8"))
    assert len(data["steps"]) == 1
    assert data["steps"][0]["cmd"] == "cat /proc/meminfo"
    assert data["steps"][0]["exit_code"] == 0
    assert "MemTotal" in data["steps"][0]["stdout"]


def test_record_turn_summary_redacts_and_updates_execution_report(tmp_path):
    s = start_session(
        tmp_path,
        run_id="r-summary",
        node="node14",
        trigger="manual",
        platform="beta",
        service="hbase",
        report_mode="execution",
    )
    s.record_deep_dive_step(
        cmd="jstat -gcutil 8159 1000 1",
        exit_code=0,
        stdout="O=95.58 FGC=3",
    )

    s.record_turn_summary("调查完成，token=secret-token")

    data = json.loads((s.dir / "session.json").read_text(encoding="utf-8"))
    assert len(data["turn_summaries"]) == 1
    assert data["turn_summaries"][0]["summary"] == (
        "调查完成，token=***REDACTED***"
    )
    report = (s.dir / "report.md").read_text(encoding="utf-8")
    assert "调查完成，token=***REDACTED***" in report
    assert "secret-token" not in report


def test_finalize_writes_report_and_verdict(tmp_path):
    s = start_session(tmp_path, run_id="r3", node="node12", trigger="manual",
                      platform="alpha", service="system")
    s.record_playbook_result({
        "playbook": "alpha/system/check_memory_pressure",
        "verdict": "conclusive", "severity": "P1",
        "conclusion_hint": "swap 残留",
        "steps": [{"name": "swap", "cmd": "free -m", "pass": False,
                   "actual": "19G/100%", "expect": "<50%", "raw": ""}],
        "metrics": {"mem_pct": 91.2},
    })
    s.finalize(
        verdict="conclusive",
        severity="P1",
        root_cause="慢性 swap 残留(si/so≈0 无活跃换页)",
        evidence=["swap 使用 19G/100%", "si/so 均 0"],
        remediation=["低峰 swapoff -a 前先降载", "ES memory_lock 配置检查"],
    )
    data = json.loads((s.dir / "session.json").read_text(encoding="utf-8"))
    assert data["verdict"] == "conclusive"
    assert data["severity"] == "P1"
    assert data["report_renderer_version"] == REPORT_RENDERER_VERSION
    report = (s.dir / "report.md").read_text(encoding="utf-8")
    assert "conclusive" in report
    assert "慢性 swap 残留" in report
    assert "swap 使用 19G/100%" in report
    assert "低峰 swapoff" in report


def test_finalize_requires_no_extra_state(tmp_path):
    """未 record 过任何结果时也能 finalize(可能是无数据结论)。"""
    s = start_session(tmp_path, run_id="r4", node="node12", trigger="manual",
                      platform="alpha", service="system")
    s.finalize(verdict="insufficient", severity="ok",
               root_cause="现场无异常",
               evidence=[], remediation=["观察 24h"])
    assert (s.dir / "report.md").exists()
