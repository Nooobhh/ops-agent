"""triage agent 共享异常:轻量模块,运行时与底座可安全 import 而不拖入 actions 的重依赖。"""
from __future__ import annotations


class ActionDenied(ValueError):
    """actions 层业务规则驳回(文档/playbook 未登记、verdict 无效等),模型可自纠。"""


class KbProposalRejected(ValueError):
    """知识草稿被驳回(字段无效/目标文档不符/证据不符),模型可自纠;
    区别于 KbSinkError 的环境性失败(lint/rebuild/session 损坏)。"""


class OrchestrationError(RuntimeError):
    """编排层错误;failure_kind 供任务落 failed/kb_failed 时记真实成因。"""

    failure_kind = None


class KnowledgeFailure(OrchestrationError):
    """知识沉淀的环境性失败:任务落 kb_failed 而非 failed。"""


class BudgetExhausted(Exception):
    """采证次数/工具级额度用尽:编排循环转续期闸门,不当作可自纠驳回。"""

    phase = "budget"
    disposition = "needs_user"

    def __init__(self, message: str, failure_kind: str, *, code: str):
        super().__init__(message)
        self.failure_kind = failure_kind
        self.code = code


class ApprovalPending(Exception):
    """任务已挂起等人工确认:本次 tool_call 的 result 由审批结论在恢复后补写。"""
