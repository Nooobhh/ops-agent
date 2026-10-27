"""A3.2 抽出的底座模块契约:新运行时直接依赖这些行为。"""
from types import SimpleNamespace

import pytest

from lib.triage_agent.approvals import pending_approval
from lib.triage_agent.errors import ActionDenied, BudgetExhausted
from lib.triage_agent.history import rebuild_messages
from lib.triage_agent.model_client import ToolCall
from lib.triage_agent.report_gate import ReportGateDecision
from lib.triage_agent.tool_batch import run_tool_batch


def _event(event_type, payload, event_id="e"):
    return SimpleNamespace(type=event_type, payload=payload, event_id=event_id)


def _calls(*names):
    return [ToolCall(call_id=f"c{index}", name=name, arguments={}) for index, name in enumerate(names)]


def _batch(calls, handle, *, dependent=(), recoverable=True):
    skipped = []
    outcome = run_tool_batch(
        SimpleNamespace(task_id="t1"),
        calls,
        batch_id="b1",
        handle=handle,
        skip=lambda task_id, call, reason, **_: skipped.append((call.call_id, reason)),
        refresh=lambda task_id: SimpleNamespace(task_id=task_id),
        is_recoverable=lambda exc: recoverable,
        continue_after_error=lambda name, exc: True,
        is_dependent=lambda name: name in dependent,
    )
    return outcome, skipped


def test_soft_failure_skips_only_dependent_calls():
    def handle(task, call, **_):
        if call.call_id == "c0":
            raise ActionDenied("bad args")
        return task, False

    outcome, skipped = _batch(_calls("shell", "finish", "shell"), handle, dependent={"finish"})
    assert [call_id for call_id, _ in skipped] == ["c1"]
    assert outcome.executed and outcome.rejections[0]["call_id"] == "c0"


def test_hard_failure_skips_rest_and_reraises():
    def handle(task, call, **_):
        raise RuntimeError("ssh down")

    with pytest.raises(RuntimeError):
        _batch(_calls("shell", "shell"), handle, recoverable=False)


def test_budget_exhaustion_is_returned_to_caller():
    def handle(task, call, **_):
        raise BudgetExhausted("诊断动作次数达到上限", "diagnostic_cap", code="X")

    outcome, skipped = _batch(_calls("shell", "shell"), handle)
    assert outcome.budget_exhausted is not None and [c for c, _ in skipped] == ["c1"]


def test_terminal_call_skips_the_rest():
    outcome, skipped = _batch(_calls("finish", "shell"), lambda task, call, **_: (task, True))
    assert outcome.terminal is not None and [c for c, _ in skipped] == ["c1"]


def test_late_tool_results_follow_their_assistant_batch():
    call = {"id": "c1", "type": "function", "function": {"name": "shell", "arguments": "{}"}}
    events = [
        _event("user_message", {"message": "q"}),
        _event("assistant_message", {"message": {"role": "assistant", "content": None, "tool_calls": [call]}}),
        _event("user_message", {"message": "追问"}),
        _event("tool_result", {"tool_call_id": "c1", "result": {"model_result": {"ok": True}}}),
    ]
    messages, declared, settled = rebuild_messages(events)
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "user"]
    assert declared == settled == {"c1"}


def test_pending_approval_ignores_settled_calls():
    events = [
        _event("approval_requested", {"approval_id": "a1", "kind": "command", "tool_call_id": "c1"}),
        _event("approval_decided", {"approval_id": "a1", "decision": "approve", "scope": "task"}),
    ]
    store = SimpleNamespace(events=lambda task_id: events)
    assert pending_approval(store, "t1", kind="command")[1:] == ("approve", "", "task")
    events.append(_event("tool_result", {"tool_call_id": "c1"}))
    assert pending_approval(store, "t1", kind="command") is None


def test_report_gate_revision_falls_back_to_gate_reason():
    gate = ReportGateDecision("revise", "c1", {}, feedback="", reason="零采证")
    assert gate.revision_error() == "报告被人工打回: 零采证"
