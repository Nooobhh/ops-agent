"""预算续期闸:人工任务预算用尽时先问是否续期,同意即加一档预算继续。

只对人工任务开闸——自动任务无人值守,沿用调用方的终止路径。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .approvals import new_approval_id, pending_approval, request_approval
from .models import TaskSource, TaskStatus, TriageTask

# 人工批准一次续期放宽的额度(轮次 / 诊断次数 / 运行秒数)
TURN_GRANT = 25
DIAGNOSTIC_GRANT = 20
SECONDS_GRANT = 900.0


@dataclass(frozen=True)
class BudgetDecision:
    approved: bool
    reason: str
    failure_kind: str


def request_budget_gate(
    store,
    task: TriageTask,
    reason: str,
    kind: str,
    *,
    turn_budget: int,
    diagnostic_budget: int,
) -> Optional[TriageTask]:
    """挂续期审批并返回挂起后的任务;非人工任务返回 None。"""
    if task.source is not TaskSource.MANUAL:
        return None
    return request_approval(store, task.task_id, {
        "approval_id": new_approval_id("bg"),
        "kind": "budget",
        "reason": reason,
        "failure_kind": kind,
        "tool_turns": task.tool_turns,
        "diagnostic_calls": task.diagnostic_calls,
        "turn_budget": turn_budget,
        "diagnostic_budget": diagnostic_budget,
        "grants": task.budget_grants,
        "grant_turns": TURN_GRANT,
        "has_report": bool(task.report_path),
    })


def consume_budget_decision(store, task: TriageTask) -> Optional[BudgetDecision]:
    """兑现续期裁决:同意即 budget_grants+1;拒绝交调用方按耗尽终止。

    续期审批不挂在 tool_call 上,pending_approval 无从判断是否已兑现;每次兑现记一条
    budget_decision_applied,否则此后任何审批让任务重入都会再加一档预算(或再判一次拒绝)。
    """
    pending = pending_approval(store, task.task_id, kind="budget", require_call=False)
    if pending is None:
        return None
    request, decision, _feedback, _scope = pending
    approval_id = request.get("approval_id")
    if any(event.type == "budget_decision_applied" and event.payload.get("approval_id") == approval_id
           for event in store.events(task.task_id)):
        return None
    store.append_event(task.task_id, "budget_decision_applied", {"approval_id": approval_id, "decision": decision})
    if decision == "approve":
        store.update(
            task.task_id,
            expected=TaskStatus.RUNNING,
            budget_grants=task.budget_grants + 1,
        )
    return BudgetDecision(
        approved=decision == "approve",
        reason=str(request.get("reason") or "预算耗尽"),
        failure_kind=request.get("failure_kind") or "turn_cap",
    )
