"""知识草稿的人工审批:渲染预览 → 挂审批 → 同意即经 KnowledgeSink 写入。

新版 Web 人工任务只有一张审批卡(全文 + 证据校验意见);升级前的任务保留
「先审意图(kb_proposal)、再审全文(kb_draft)」两段式,恢复历史兼容。
收口后的去向(回到 waiting_input 或推进 completed)由调用方的 finish 决定。
"""
from __future__ import annotations

import inspect
from typing import Any, Callable, Dict, Optional, Tuple

from .approvals import PendingApproval, new_approval_id, pending_approval, request_approval
from .errors import ApprovalPending, KbProposalRejected, KnowledgeFailure, OrchestrationError
from .models import TaskSource, TaskStatus, TriageTask

KB_TOOL = "create_kb_draft"

Finish = Callable[[TriageTask], TriageTask]


class KbReview:
    def __init__(self, store, invocations, knowledge_sink, display_path: Callable[[Any], str]):
        self.store = store
        self.invocations = invocations
        self.knowledge_sink = knowledge_sink
        self.display_path = display_path

    def settle(self, task_id: str, call_id: str, result: Dict[str, Any]) -> None:
        self.invocations.settle(task_id, call_id, KB_TOOL, result)

    def request_review(self, task: TriageTask, proposal: Dict[str, Any], call_id: str) -> None:
        """人工任务的知识落档:同一意图已获批或新版 Web 任务直接进全文预览。"""
        # Current Web manual tasks use one review card: evidence checks are
        # rendered as human-readable warnings together with the full draft.
        # Keep the two-stage path for pre-existing tasks created before this
        # contract, so their recovery history remains compatible.
        if task.manual_artifacts or self.intent_approved(task.task_id, proposal):
            self.request_draft_review(task, proposal, call_id)
            return
        request_approval(self.store, task.task_id, {
            "approval_id": new_approval_id("kp"),
            "kind": "kb_proposal",
            "tool_call_id": call_id,
            "intent": self.intent_key(proposal),
            "mode": str(proposal.get("mode") or ""),
            "doc": self.target_display(proposal),
            "title": str(proposal.get("title") or ""),
            "summary": str(proposal.get("mechanism") or "")[:500],
            "symptoms": list(proposal.get("symptoms") or [])[:8],
            "proposal": proposal,
        })
        raise ApprovalPending()

    def request_draft_review(self, task: TriageTask, proposal: Dict[str, Any], call_id: str) -> None:
        """渲染草稿全文(改动带 diff 标注)挂人工确认。"""
        try:
            preview = self.knowledge_sink.preview(task.run_id, proposal)
        except KbProposalRejected as exc:
            raise OrchestrationError(f"知识草稿被驳回: {exc}") from exc
        except Exception as exc:
            raise KnowledgeFailure(
                f"知识草稿预览失败: {type(exc).__name__}: {exc}"
            ) from exc
        request_approval(self.store, task.task_id, {
            "approval_id": new_approval_id("kb"),
            "kind": "kb_draft",
            "tool_call_id": call_id,
            "doc": self.display_path(preview.path),
            "mode": preview.mode,
            "markdown": preview.markdown,
            "diff": getattr(preview, "diff", None) or "",
            "evidence_issues": list(getattr(preview, "evidence_issues", ()) or ()),
            "proposal": proposal,
        })
        raise ApprovalPending()

    @staticmethod
    def intent_key(proposal: Dict[str, Any]) -> str:
        mode = str(proposal.get("mode") or "")
        if mode == "append_case":
            return f"append_case:{proposal.get('doc') or ''}"
        return (
            "new:"
            f"{proposal.get('platform') or ''}/{proposal.get('service') or ''}/"
            f"{proposal.get('problem') or ''}"
        )

    @staticmethod
    def target_display(proposal: Dict[str, Any]) -> str:
        mode = str(proposal.get("mode") or "")
        if mode == "append_case":
            return str(proposal.get("doc") or "")
        platform = str(proposal.get("platform") or "")
        service = str(proposal.get("service") or "")
        problem = str(proposal.get("problem") or "")
        return f"{platform}/{service}/{platform}-{service}-{problem}.md"

    def intent_approved(self, task_id: str, proposal: Dict[str, Any]) -> bool:
        intent = self.intent_key(proposal)
        approved = set()
        requests: Dict[str, str] = {}
        for event in self.store.events(task_id):
            payload = event.payload
            if (
                event.type == "approval_requested"
                and payload.get("kind") == "kb_proposal"
            ):
                requests[str(payload.get("approval_id") or "")] = str(
                    payload.get("intent") or ""
                )
            elif event.type == "approval_decided":
                approval_id = str(payload.get("approval_id") or "")
                if (
                    approval_id in requests
                    and str(payload.get("decision") or "") == "approve"
                ):
                    approved.add(requests[approval_id])
        return intent in approved

    def _decline(self, task: TriageTask, call_id: str, feedback: str, finish: Finish) -> TriageTask:
        self.settle(task.task_id, call_id, {
            "kb_persisted": False,
            "declined_by_user": True,
            "reason": feedback or "用户确认本次排查无需沉淀知识库",
        })
        task = self.store.update(task.task_id, expected=TaskStatus.RUNNING, kb_declined=True)
        return finish(task)

    def consume_proposal(self, task: TriageTask, *, finish: Finish) -> Optional[TriageTask]:
        """兑现意图闸决议:同意即挂全文预览,打回给模型改意图,拒绝走收口。"""
        pending = pending_approval(self.store, task.task_id, kind="kb_proposal")
        if pending is None:
            return None
        request, decision, feedback, _scope = pending
        call_id = str(request.get("tool_call_id") or "")
        # 同意后已挂出的全文预览由 kb_draft 分支兑现,不重复处理
        for event in self.store.events(task.task_id):
            if (
                event.type == "approval_requested"
                and event.payload.get("kind") == "kb_draft"
                and str(event.payload.get("tool_call_id") or "") == call_id
            ):
                return None
        proposal = request.get("proposal") or {}
        if decision == "approve":
            try:
                self.request_draft_review(task, proposal, call_id)
            except ApprovalPending:
                return self.store.get(task.task_id)
            except OrchestrationError as exc:
                if isinstance(exc, KnowledgeFailure):
                    raise
                self.settle(task.task_id, call_id, {"error": str(exc), "failed": True})
                return None
            return None
        if decision == "reject":
            return self._decline(task, call_id, feedback, finish)
        self.settle(task.task_id, call_id, {
            "kb_persisted": False,
            "revision_requested": True,
            "feedback": feedback or "用户要求调整知识落档请求后重新提交",
        })
        return None

    def persist(
        self,
        task: TriageTask,
        proposal: Dict[str, Any],
        *,
        finish: Finish,
        allow_incomplete_evidence: bool = False,
    ) -> Tuple[TriageTask, Dict[str, Any], bool]:
        try:
            persist_kwargs = {}
            try:
                parameters = inspect.signature(self.knowledge_sink.persist).parameters
            except (TypeError, ValueError):
                parameters = {}
            if (
                "allow_incomplete_evidence" in parameters
                or any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
            ):
                persist_kwargs["allow_incomplete_evidence"] = allow_incomplete_evidence
            persisted = self.knowledge_sink.persist(task.run_id, proposal, **persist_kwargs)
        except KbProposalRejected as exc:
            # 草稿内容问题模型可自纠,回错误重试;环境性失败才落 kb_failed
            raise OrchestrationError(f"知识草稿被驳回: {exc}") from exc
        except Exception as exc:
            raise KnowledgeFailure(
                f"知识库沉淀失败: {type(exc).__name__}: {exc}"
            ) from exc
        task = self.store.update(
            task.task_id,
            expected=TaskStatus.RUNNING,
            kb_doc=self.display_path(persisted.path),
        )
        finished = finish(task)
        result = {"kb_doc": task.kb_doc}
        promotion = getattr(persisted, "promotion", None)
        if promotion:
            result["kb_promotion"] = promotion
        return finished, result, True

    def pending_draft(self, task_id: str) -> Optional[PendingApproval]:
        return pending_approval(self.store, task_id, kind="kb_draft")

    def consume_draft(self, task: TriageTask, *, finish: Finish) -> Optional[TriageTask]:
        """把人工审批结论兑现成待决 create_kb_draft 调用的 tool_result。"""
        pending = self.pending_draft(task.task_id)
        if pending is None:
            return None
        request, decision, feedback, _scope = pending
        call_id = str(request.get("tool_call_id") or "")
        proposal = request.get("proposal") or {}
        if decision == "approve":
            try:
                completed, result, _terminal = self.persist(
                    task,
                    proposal,
                    finish=finish,
                    # 人工确认就是证据完整性豁免；结构、lint、红线和原子
                    # 写入仍由 KnowledgeSink/KnowledgeWriter 强制执行。
                    allow_incomplete_evidence=task.source is TaskSource.MANUAL,
                )
            except OrchestrationError as exc:
                if isinstance(exc, KnowledgeFailure):
                    raise
                self.settle(task.task_id, call_id, {"error": str(exc), "failed": True})
                return None
            self.settle(completed.task_id, call_id, result)
            return completed
        if decision == "reject":
            return self._decline(task, call_id, feedback, finish)
        self.settle(task.task_id, call_id, {
            "kb_persisted": False,
            "revision_requested": True,
            "feedback": feedback or "用户要求修改草稿后重新提交确认",
        })
        return None
