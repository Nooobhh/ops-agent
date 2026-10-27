"""人工审批的事件流原语:挂起、查找待兑现的裁决、判断系统审批是否已兑现。

审批状态全在 TaskStore 事件里(approval_requested / approval_decided / tool_result),
worker 重启后由事件流重建;新旧运行时共用,各自决定裁决兑现成什么动作。
"""
from __future__ import annotations

import secrets
from typing import Any, Dict, Optional, Tuple

from .models import TaskStatus, TriageTask

# (请求 payload, approve/revise/reject, 反馈, 作用域)
PendingApproval = Tuple[Dict[str, Any], str, str, Optional[str]]


def new_approval_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(6)}"


def request_approval(store, task_id: str, payload: Dict[str, Any]) -> TriageTask:
    """落 approval_requested 并挂起任务(释放租约),返回挂起后的任务。"""
    store.append_event(task_id, "approval_requested", payload)
    return store.transition(
        task_id,
        TaskStatus.RUNNING,
        TaskStatus.WAITING_APPROVAL,
        lease_owner=None,
        lease_expires_at=None,
    )


def pending_approval(
    store,
    task_id: str,
    *,
    kind: str,
    require_call: bool = True,
) -> Optional[PendingApproval]:
    """取该类最近一次已裁决、尚未兑现的审批;没有则 None。"""
    request: Optional[Dict[str, Any]] = None
    decision: Optional[Dict[str, Any]] = None
    settled = set()
    for event in store.events(task_id):
        payload = event.payload
        if (
            event.type == "approval_requested"
            and payload.get("kind") == kind
        ):
            request, decision = payload, None
        elif (
            event.type == "approval_decided"
            and request is not None
            and payload.get("approval_id") == request.get("approval_id")
        ):
            decision = payload
        elif event.type == "tool_result":
            settled.add(str(payload.get("tool_call_id") or ""))
    if request is None or decision is None:
        return None
    call_id = str(request.get("tool_call_id") or "")
    # 系统发起的审批(如 worklog 闸)不挂在 tool_call 上,凭任务未完成判定
    if require_call and (not call_id or call_id in settled):
        return None
    verdict = str(decision.get("decision") or "").strip()
    if verdict not in {"approve", "revise", "reject"}:
        verdict = "approve" if decision.get("approved") else "reject"
    scope = decision.get("scope")
    return (
        request,
        verdict,
        str(decision.get("feedback") or ""),
        str(scope) if scope else None,
    )


def approval_reached_status(store, task_id: str, approval_id: str, status: str) -> bool:
    """判断无 tool_call 的系统审批是否已经完成其状态迁移。"""
    decided = False
    for event in store.events(task_id):
        payload = event.payload
        if (
            event.type == "approval_decided"
            and payload.get("approval_id") == approval_id
        ):
            decided = True
        elif (
            decided
            and event.type == "status"
            and payload.get("status") == status
        ):
            return True
    return False
