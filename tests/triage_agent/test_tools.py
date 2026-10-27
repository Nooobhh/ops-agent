from __future__ import annotations

import pytest
from pydantic import ValidationError

from lib.triage_agent.tools import (
    KnowledgeProposal,
    normalize_tool_arguments,
    validate_tool_call,
    tool_definitions,
)


def test_knowledge_proposal_allows_human_review_when_evidence_is_empty() -> None:
    proposal = KnowledgeProposal(
        mode="new",
        title="memory",
        platform="alpha",
        service="system",
        problem="memory",
        symptoms=["memory high"],
        alerts=["MemoryHigh"],
        mechanism="pressure",
        evidence=[],
        remediation=["observe"],
    )
    assert proposal.evidence == []


def test_kb_draft_accepts_flat_model_payload() -> None:
    proposal = validate_tool_call("create_kb_draft", {
        "mode": "append_case",
        "doc": "alpha/system/memory.md",
        "evidence": ["observed"],
    })
    assert proposal.proposal.doc == "alpha/system/memory.md"


def test_nested_tool_normalization_keeps_unknown_fields_strict() -> None:
    normalized = normalize_tool_arguments("create_kb_draft", {"mode": "append_case", "unknown": "must fail"})
    assert normalized == {"mode": "append_case", "unknown": "must fail"}
    with pytest.raises(ValidationError):
        validate_tool_call("create_kb_draft", normalized)


def test_only_kb_draft_and_artifact_definitions_remain() -> None:
    assert {item["function"]["name"] for item in tool_definitions()} == {"create_kb_draft", "create_task_artifact"}
