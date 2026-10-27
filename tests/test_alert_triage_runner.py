import json
import re
from dataclasses import dataclass

from lib.alerts.model import AlertEvent
from lib.record import lint_records
from lib.runner import PlaybookTimeout
from lib.alerts.triage_runner import run_auto_triage
from lib.envpack import active_env_pack


def event(alertname="cpu_hot", *, platform="alpha", host="node1"):
    return AlertEvent(
        source="prometheus",
        platform=platform,
        alertname=alertname,
        severity="critical",
        host=host,
        host_raw=host or "",
        service="system",
        state="firing",
        detail="hot",
    )


def routing(playbook="playbooks/alpha/system/check_memory_pressure.sh"):
    return {
        "alerts": {
            "alpha": {
                "cpu_hot": {
                    "doc": "knowledge/alpha/system/cpu.md",
                    "playbook": playbook,
                    "platform": "alpha",
                    "service": "system",
                },
                "doc_only": {
                    "doc": "knowledge/alpha/system/doc.md",
                    "platform": "alpha",
                    "service": "system",
                },
            },
        },
        "docs": [{"path": "knowledge/alpha/system/cpu.md", "playbook": playbook}],
    }


@dataclass
class FakeResult:
    verdict: str = "needs_llm"
    severity: str = "P1"
    conclusion_hint: str = "memory pressure"

    def to_session_dict(self):
        return {
            "playbook": "check_memory_pressure.sh",
            "node": "node1",
            "verdict": self.verdict,
            "severity": self.severity,
            "conclusion_hint": self.conclusion_hint,
            "steps": [
                {
                    "name": "load",
                    "cmd": "uptime",
                    "expect": "<4",
                    "actual": "8",
                    "pass": False,
                    "raw": "8",
                }
            ],
            "metrics": {},
            "next_playbook": None,
            "raw_dir": None,
        }


def test_unmatched_alert_is_not_applicable_and_does_not_create_session(tmp_path):
    outcome = run_auto_triage(event("missing"), routing({}), pool=object(), records_root=tmp_path)

    assert outcome.status == "not_applicable"
    assert outcome.run_id is None
    assert list(tmp_path.iterdir()) == []


def test_platform_mismatch_refuses_to_run_other_platform_playbook(tmp_path):
    """beta 事件不应命中只在 alpha 分区登记的同名路由。"""
    outcome = run_auto_triage(event(platform="beta"), routing(), pool=object(), records_root=tmp_path)

    assert outcome.status == "not_applicable"
    assert outcome.error == "no routing match"
    assert list(tmp_path.iterdir()) == []


def test_shared_doc_matches_any_platform(monkeypatch, tmp_path):
    """shared 文档跨平台通用,不该被平台闸挡掉。"""
    r = routing()
    route = r["alerts"]["alpha"].pop("cpu_hot")
    route["platform"] = "shared"
    r["alerts"]["shared"] = {"cpu_hot": route}
    monkeypatch.setattr("lib.alerts.triage_runner.run_playbook",
                        lambda *a, **k: FakeResult())
    outcome = run_auto_triage(event(platform="beta"), r, pool=object(), records_root=tmp_path)

    assert outcome.status == "done"


def test_same_patrol_alert_routes_to_event_platform(monkeypatch, tmp_path):
    """alpha/beta 同名 patrol 告警各自命中本平台文档与 playbook。"""
    alertname = "patrol:patrol_hdfs_health:dn_process"
    r = routing()
    r["alerts"] = {
        "alpha": {alertname: {
            "doc": "knowledge/alpha/hdfs/datanode/alpha-datanode-health.md",
            "playbook": "playbooks/alpha/hdfs/datanode/check_datanode_health.sh",
            "platform": "alpha", "service": "hdfs",
        }},
        "beta": {alertname: {
            "doc": "knowledge/beta/hdfs/datanode/beta-datanode-health.md",
            "playbook": "playbooks/beta/hdfs/datanode/check_datanode_health.sh",
            "platform": "beta", "service": "hdfs",
        }},
    }
    r["docs"] = [
        {"path": route["doc"], "playbook": route["playbook"]}
        for entries in r["alerts"].values() for route in entries.values()
    ]
    monkeypatch.setattr("lib.alerts.triage_runner.run_playbook",
                        lambda *a, **k: FakeResult())

    outcome = run_auto_triage(
        event(alertname, platform="beta"), r, pool=object(), records_root=tmp_path,
    )

    assert outcome.status == "done"
    assert outcome.matched_doc == "knowledge/beta/hdfs/datanode/beta-datanode-health.md"
    assert outcome.playbook == "playbooks/beta/hdfs/datanode/check_datanode_health.sh"


def test_matched_doc_without_playbook_is_not_applicable_with_doc(tmp_path):
    outcome = run_auto_triage(event("doc_only"), routing(), pool=object(), records_root=tmp_path)

    assert outcome.status == "not_applicable"
    assert outcome.matched_doc == "knowledge/alpha/system/doc.md"
    assert list(tmp_path.iterdir()) == []


def test_playbook_success_records_unfinalized_session_and_artifact(monkeypatch, tmp_path):
    calls = []

    def fake_run_playbook(pool, node, playbook_path, run_id, timeout=0):
        calls.append((pool, node, playbook_path.as_posix(), run_id, timeout))
        return FakeResult()

    monkeypatch.setattr("lib.alerts.triage_runner.run_playbook", fake_run_playbook)

    outcome = run_auto_triage(event(), routing(), pool="pool", records_root=tmp_path)

    assert outcome.status == "done"
    assert re.fullmatch(r"al-\d{8}-\d{4}-[0-9a-f]{4}", outcome.run_id)
    run_dir = tmp_path / outcome.run_id
    session = json.loads((run_dir / "session.json").read_text(encoding="utf-8"))
    assert session["playbook_results"]
    assert session["verdict"] is None
    assert (run_dir / "artifacts" / "alert-context.json").exists()
    assert lint_records(tmp_path) == []
    assert calls[0][1:] == (
        "node1",
        active_env_pack().playbook_path("playbooks/alpha/system/check_memory_pressure.sh").as_posix(),
        outcome.run_id,
        180,
    )


def test_ordered_playbooks_continue_after_insufficient_until_conclusive(
    monkeypatch, tmp_path,
):
    first = "playbooks/alpha/system/check_memory_pressure.sh"
    second = "playbooks/alpha/system/check_memory_deep.sh"
    r = routing(first)
    r["alerts"]["alpha"]["cpu_hot"]["playbooks"] = [
        {"path": first, "order": 1}, {"path": second, "order": 2},
    ]
    r["docs"][0]["playbooks"] = [
        {"path": first, "order": 1}, {"path": second, "order": 2},
    ]
    calls = []

    def fake_run_playbook(_pool, _node, path, run_id, timeout=0):
        calls.append(path.as_posix())
        return FakeResult(verdict="insufficient" if len(calls) == 1 else "conclusive")

    monkeypatch.setattr("lib.alerts.triage_runner.run_playbook", fake_run_playbook)

    outcome = run_auto_triage(event(), r, pool="pool", records_root=tmp_path)

    assert outcome.status == "done"
    # 执行的是环境包内文件,结果仍按知识库登记的引用记
    assert calls == [active_env_pack().playbook_path(ref).as_posix() for ref in (first, second)]
    assert outcome.playbooks == [first, second]
    assert outcome.playbook == second


def test_playbook_timeout_returns_failed_and_keeps_session(monkeypatch, tmp_path):
    def fake_run_playbook(*args, **kwargs):
        raise PlaybookTimeout("slow")

    monkeypatch.setattr("lib.alerts.triage_runner.run_playbook", fake_run_playbook)

    outcome = run_auto_triage(event(), routing(), pool="pool", records_root=tmp_path)

    assert outcome.status == "failed"
    assert "slow" in outcome.error
    assert (tmp_path / outcome.run_id / "session.json").exists()
    assert lint_records(tmp_path) == []
