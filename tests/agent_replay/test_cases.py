import json

import pytest
import yaml

from lib.agent_replay.cases import build_case, freeze_state_fixtures, load_manifest, select_cases


def write_task(root, task_id, *, task, events, invocations):
    task_dir = root / "state" / "triage-tasks" / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "task.json").write_text(json.dumps({"task_id": task_id, **task}), encoding="utf-8")
    (task_dir / "messages.jsonl").write_text(
        "".join(json.dumps({"type": kind, "payload": payload}) + "\n" for kind, payload in events),
        encoding="utf-8",
    )
    (task_dir / "invocations.json").write_text(json.dumps(invocations), encoding="utf-8")


@pytest.fixture
def source_repo(tmp_path):
    root = tmp_path / "source"
    write_task(
        root, "tr-main",
        task={"status": "completed", "platform": "alpha", "node": "node1", "run_id": "ts-main",
              "initial_message": "检查 node1 内存", "manual_artifacts": False},
        events=[
            ("tool_call", {"tool_call_id": "c1", "name": "run_diagnostic",
                           "arguments": {"diagnostic_id": "system.memory_summary", "params": {}}}),
            ("tool_call", {"tool_call_id": "c2", "name": "run_command",
                           "arguments": {"node": "node1", "pipeline": [["uptime"]]}}),
            ("approval_requested", {"approval_id": "cm-1", "kind": "command", "command": "rm  -rf /tmp/x"}),
            ("approval_decided", {"approval_id": "cm-1", "approved": False, "decision": "reject"}),
            ("approval_requested", {"approval_id": "bg-1", "kind": "budget"}),
            ("approval_decided", {"approval_id": "bg-1", "approved": True, "decision": "approve"}),
            ("hypothesis_reviewed", {"review": {"prediction_results": [{"evidence_refs": ["c2"]}]}}),
        ],
        invocations={
            "c1": {"status": "done", "result": {"stdout": "Mem:  total 64G\nused 10G", "exit_code": 0}},
            "c2": {"status": "done", "result": {"command": "uptime", "stdout": "load 1.0", "exit_code": 0}},
        },
    )
    write_task(
        root, "tr-extra",
        task={"status": "failed", "platform": "alpha", "node": "node1"},
        events=[("tool_call", {"tool_call_id": "c9", "name": "run_command",
                               "arguments": {"node": "node1", "pipeline": [["df", "-h"]]}})],
        invocations={"c9": {"status": "done", "result": {"command": "df -h", "stdout": "/ 50%", "exit_code": 0}}},
    )
    session = root / "records" / "troubleshoot" / "ts-main"
    session.mkdir(parents=True)
    (session / "session.json").write_text(json.dumps({
        "verdict": "conclusive", "evidence": ["total 64G used 10G"],
    }), encoding="utf-8")
    state = root / "state"
    (state / "graphs" / "jobs").mkdir(parents=True)
    (state / "graphs" / "jobs" / "graph-latest.json").write_text("{}", encoding="utf-8")
    (state / "graphs" / "jobs" / "runtime.json").write_text("{}", encoding="utf-8")
    (state / "topology").mkdir()
    (state / "topology" / "snapshot-alpha.json").write_text("{}", encoding="utf-8")
    return root


def test_build_case_freezes_inputs_recordings_policy_and_key_evidence(source_repo):
    case = build_case({
        "id": "qa-node1-mem",
        "source": "tr-main",
        "extra_sources": ["tr-extra"],
        "expect": {"task_type": "qa"},
        "followups": ["继续"],
    }, source_repo)

    assert case["create"]["message"] == "检查 node1 内存"
    # 现行 Web 入口都是 manual_artifacts=True,历史值不沿用
    assert case["create"]["manual_artifacts"] is True
    assert [item["call_id"] for item in case["recordings"]] == ["c1", "c2", "c9"]
    assert case["interaction"]["budget_grants"] == 1
    assert case["interaction"]["command_rejects"] == ["rm -rf /tmp/x"]
    assert case["interaction"]["followups"] == ["继续"]
    # c2 来自假设审核引用,c1 由 finalize 证据原文(空白归一)反查
    assert case["labels"]["key_evidence"] == ["c2", "c1"]
    assert case["labels"]["source"]["verdict"] == "conclusive"
    assert case["create"]["alert_context"] is None


def test_alert_entry_carries_request_target_and_alert_context(source_repo):
    alert = {"source": "demo", "alertname": "datanode_health_summary", "platform": "alpha", "node": "node1"}
    case = build_case({
        "id": "alert-node1",
        "source": "tr-main",
        "request": {"platform": "alpha", "node": "node1"},
        "alert_context": alert,
    }, source_repo)

    assert (case["create"]["platform"], case["create"]["node"]) == ("alpha", "node1")
    assert case["create"]["alert_context"] == alert


def test_freeze_fixtures_copies_only_listed_state(source_repo, tmp_path):
    copied = freeze_state_fixtures(source_repo / "state", tmp_path / "fixtures")
    assert copied == ["graphs/jobs/graph-latest.json", "topology/"]
    assert not (tmp_path / "fixtures" / "state" / "triage-tasks").exists()


def test_manifest_validation_and_selection(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"cases": [{"id": "x1", "create": "auto", "source": "tr"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="alert_event"):
        load_manifest(bad)

    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    for case_id, smoke in (("a1", True), ("b1", False)):
        (cases_dir / f"{case_id}.json").write_text(
            json.dumps({"schema_version": 1, "case_id": case_id, "smoke": smoke}), encoding="utf-8",
        )
    assert [case["case_id"] for case in select_cases(cases_dir, "smoke")] == ["a1"]
    assert [case["case_id"] for case in select_cases(cases_dir, "b1,a1")] == ["b1", "a1"]
    with pytest.raises(ValueError, match="不存在"):
        select_cases(cases_dir, "zz")
