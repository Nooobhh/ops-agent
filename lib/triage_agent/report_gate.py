"""报告闸:收口证据不足时转发起人三键裁决(approve 放行 / revise 打回 / reject 终止)。

这里只管挂审批与解读裁决;放行后如何落报告、终止时如何保成果由运行时决定
(结构档走 finalize 证据闸,新运行时按 finish 核验结果,收口语义不同)。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from .approvals import new_approval_id, pending_approval, request_approval
from .errors import ApprovalPending
from .models import TriageTask


@dataclass(frozen=True)
class ReportGateDecision:
    decision: str  # approve / revise / reject
    call_id: str
    proposal: Dict[str, Any]
    feedback: str
    # 挂闸时的原始驳回理由:打回未填反馈时回传给模型
    reason: str

    def revision_error(self) -> str:
        # 证据闸有零采证与引用不符两种成因,硬编码「改引用」文案会把零采证
        # 任务引向改措辞而不是去采证,故未填反馈时回落到闸的原始理由
        return "报告被人工打回: " + (
            self.feedback
            or self.reason.strip()
            or "请只逐字引用已记录的采证输出片段后重新收口"
        )


def request_report_gate(
    store,
    task: TriageTask,
    *,
    call_id: str,
    reason: str,
    proposal: Dict[str, Any],
) -> None:
    request_approval(store, task.task_id, {
        "approval_id": new_approval_id("rg"),
        "kind": "report_gate",
        "tool_call_id": call_id,
        "reason": reason,
        "proposal": proposal,
    })
    raise ApprovalPending()


def pending_report_gate(store, task_id: str) -> Optional[ReportGateDecision]:
    pending = pending_approval(store, task_id, kind="report_gate")
    if pending is None:
        return None
    request, decision, feedback, _scope = pending
    return ReportGateDecision(
        decision=decision,
        call_id=str(request.get("tool_call_id") or ""),
        proposal=request.get("proposal") or {},
        feedback=feedback,
        reason=str(request.get("reason") or ""),
    )
