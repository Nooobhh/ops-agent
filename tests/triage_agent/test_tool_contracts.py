import pytest
from lib.triage_agent.tool_registry import ToolAdmissionError, ToolRegistry
from lib.triage_agent.tool_results import (
    ToolResult,
    error_tool_result,
    model_tool_result,
    normalize_tool_result,
)
from lib.triage_agent.tools import TOOL_METADATA, TOOLS


def test_remaining_tools_have_complete_metadata():
    assert set(TOOLS) == set(TOOL_METADATA) == {"create_kb_draft", "create_task_artifact"}
    for name, metadata in TOOL_METADATA.items():
        assert metadata.name == name and metadata.version and metadata.budget_cost and metadata.failure_policy
        assert ToolRegistry().metadata(name) is metadata
    with pytest.raises(ToolAdmissionError):
        ToolRegistry().metadata("shell")


def test_normalized_result_has_strict_envelope_and_legacy_projection():
    result = normalize_tool_result(
        "run_diagnostic",
        {
            "diagnostic_id": "system.memory_summary",
            "node": "node1",
            "exit_code": 1,
            "stdout": "",
            "stderr": "not found",
        },
        call_id="call-1",
        target_kind="registered_node",
        evidence_eligible=True,
    )
    canonical = ToolResult.model_validate(result["tool_result"])
    ToolResult.model_validate(result)
    assert canonical.ok is True
    assert canonical.outcome == "completed"
    assert canonical.evidence.eligible is True
    assert result["exit_code"] == 1
    assert result["tool"]["name"] == "run_diagnostic"


def test_model_tool_result_is_bounded_but_audit_result_keeps_full_output():
    raw = {
        "operation": "run_command",
        "target": {"platform": "alpha", "node": "node1"},
        "execution": {"stdout": "x" * 10000, "stderr": "", "exit_code": 0},
        "evidence_refs": ["call-1"],
    }
    result = normalize_tool_result(
        "run_command", raw, call_id="call-1", target_kind="registered_node",
        evidence_eligible=True,
    )
    assert len(result["tool_result"]["data"]["execution"]["stdout"]) == 10000
    model = model_tool_result(result)
    assert len(model["data"]["stdout"]) <= 3000
    assert model["evidence"]["refs"] == ["call-1"]


def test_model_result_keeps_non_command_payloads_visible():
    # 2026-10-21 A1 实验:检索候选/任务依赖/快照被投影成 data={},模型以为「检索为空」
    candidates = normalize_tool_result(
        "search_kb_candidates",
        {"candidates": [{"path": "jobs/a.md", "title": "读写并发", "summary": "s"}]},
        call_id="call-1",
    )
    assert model_tool_result(candidates)["data"]["candidates"][0]["path"] == "jobs/a.md"

    deps = normalize_tool_result(
        "read_task_dependencies",
        {"schema_version": 1, "kind": "graph_dependencies", "tasks": [
            {"task_id": 10167, "reads": [{"table": "ads.t", "writers": {"tasks": [{"task_id": 5919}]}}]},
        ]},
        call_id="call-2",
    )
    model = model_tool_result(deps)["data"]
    assert model["tasks"][0]["reads"][0]["writers"]["tasks"][0]["task_id"] == 5919
    assert "schema_version" not in model

    playbook = normalize_tool_result(
        "run_registered_playbook",
        {"verdict": "conclusive", "steps": [{"name": "dn", "actual": "14 live", "pass": True}]},
        call_id="call-3",
    )
    assert model_tool_result(playbook)["data"]["steps"][0]["actual"] == "14 live"


def test_model_result_payload_is_bounded_and_marked_truncated():
    big = {"tasks": [{"task_id": i, "sql": "x" * 2000, "reads": list(range(50))} for i in range(60)]}
    result = normalize_tool_result("read_task_dependencies", big, call_id="call-4")

    data = model_tool_result(result, max_content_chars=12000)["data"]

    assert len(__import__("json").dumps(data, ensure_ascii=False)) <= 12000
    assert data["truncated"] is True
    assert data["tasks"] and data["tasks"][0]["task_id"] == 0  # 收紧时主字段仍在
    assert len(result["tool_result"]["data"]["tasks"]) == 60  # 审计结果保持完整


def test_error_result_has_disposition_without_exposing_exception_object():
    result = error_tool_result(
        "run_command",
        ValueError("invalid password=super-secret"),
        call_id="call-2",
        code="INVALID_ARGUMENT",
        phase="validation",
        retryable=True,
        disposition="agent_retry",
        outcome="denied",
    )
    assert result["error"] == "invalid password=***REDACTED***"
    assert result["error_detail"]["disposition"] == "agent_retry"
    assert result["tool_result"]["outcome"] == "denied"
