"""命令放行结论的落地:放行、驳回、交审核模型、挂人工审批,以及兑现人工裁决。

判定(argv 正表或裸 shell 命令闸)由各运行时自己做,这里只接 PolicyVerdict:

    allow   放行
    deny    回可自纠错误
    ask     红线写操作 / 凭据路径 / default 档未登记 → 直接挂人工
    review  auto 档未登记 → 审核模型判安全即放行,否则挂人工

人工批准后由 Harness 直接执行挂起的那条调用(execute 回调),不回到放行闸;
永久名单覆盖不了 ask:那是「以后都不问」,对 rm 和凭据太宽。自动任务无人值守,
够不到裁决就直接驳回。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .approvals import new_approval_id, pending_approval, request_approval
from .errors import ActionDenied, ApprovalPending
from .models import TaskSource, TaskStatus, TriageTask


class CommandApprovals:
    def __init__(self, store, invocations, *, policy=None, reviewer=None):
        self.store = store
        self.invocations = invocations
        self.policy = policy
        self.reviewer = reviewer

    def resolve(
        self,
        task: TriageTask,
        decision,
        *,
        call_id: str,
        command: str,
        signature: str,
        node: Optional[str],
        targets: List[str],
        deny_guidance: str = "",
    ) -> None:
        """allow / review 判安全时返回;否则抛 ActionDenied 或挂人工后抛 ApprovalPending。"""
        if decision.action == "allow":
            return
        if decision.action == "deny":
            raise ActionDenied(f"命令未获放行({decision.reason}): {command}{deny_guidance}")
        review = None
        if decision.action == "review":
            # auto 档:审核模型是**放行闸**而不是提示。它守的是静态判不出安全与否的
            # 那片区域,只能靠看意图;够不到 ask 那几类——它们在判定层就分流走了
            if self.reviewer is not None:
                review = self.reviewer.review(
                    command=command,
                    signature=signature,
                    context={
                        "platform": task.platform,
                        "node": node or task.node,
                        "symptom": task.initial_message,
                    },
                )
            if review is not None and review.reviewed and review.safe:
                return
            # 判危险,或审核器不可用/响应异常 → 落人工。审核模型缺席时全部转
            # 人工而不是全部放行,是这一层的 fail-safe 方向
        if task.source is not TaskSource.MANUAL:
            raise ActionDenied(f"自动任务不支持人工裁决,未获放行的命令一律驳回: {command}")
        self.request(
            task, call_id=call_id, command=command, signature=signature,
            node=node, targets=targets, review=review, reason=decision.reason,
        )
        raise ApprovalPending()

    def request(
        self,
        task: TriageTask,
        *,
        call_id: str,
        command: str,
        signature: str,
        node: Optional[str],
        targets: List[str],
        review,
        reason: str,
    ) -> None:
        request_approval(self.store, task.task_id, {
            "approval_id": new_approval_id("cm"),
            "kind": "command",
            "tool_call_id": call_id,
            "command": command,
            "signature": signature,
            "node": node or task.node,
            "targets": targets,
            "mode": task.command_mode or (
                self.policy.load().mode if self.policy is not None else "auto"
            ),
            "reason": reason,
            "review": None if review is None else {
                "safe": review.safe,
                "reason": review.reason,
                "reviewed": review.reviewed,
            },
        })

    def consume(
        self,
        task: TriageTask,
        *,
        tool_name: str,
        execute: Callable[[TriageTask, str, str], None],
    ) -> None:
        """兑现四档裁决。

        approve × once   放行这一次
        approve × task   本次排查内同签名都放行
        approve × always 写永久白名单
        reject  × always 写永久黑名单
        reject  × 其他   仅驳回本次
        """
        pending = pending_approval(self.store, task.task_id, kind="command")
        if pending is None:
            return
        request, decision, feedback, scope = pending
        signature = str(request.get("signature") or "")
        call_id = str(request.get("tool_call_id") or "")
        approved = decision == "approve"
        # 裸 shell 一条命令可含多个未登记段,签名按换行分段,名单逐段生效
        signatures = [item for item in signature.split("\n") if item]

        # 只有明确的 approve/reject 才动永久名单:revise 是「要求改写」,
        # 把它当永久拒绝会因为一次措辞反馈把整类命令拉黑
        if (
            scope == "always"
            and decision in {"approve", "reject"}
            and self.policy is not None
        ):
            for item in signatures:
                self.policy.remember(item, allow=approved)
        new_defaults = [item for item in signatures if item not in task.command_defaults]
        if approved and scope == "task" and new_defaults:
            task = self.store.update(
                task.task_id,
                expected=TaskStatus.RUNNING,
                command_defaults=[*task.command_defaults, *new_defaults],
            )
        if approved:
            execute(task, call_id, signature)
            return
        result: Dict[str, Any] = {
            "approved": False,
            "scope": scope or "once",
            "error": feedback or f"命令被拒绝执行: {request.get('command')}",
        }
        self.invocations.settle(task.task_id, call_id, tool_name, result)
