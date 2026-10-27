from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .task_types import TaskType, TaskTypeSource


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskSource(str, Enum):
    MANUAL = "manual"
    AUTO = "auto"


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    WAITING_APPROVAL = "waiting_approval"
    # 人工批准的写操作已提交,Harness 跟踪到终态前不跑模型、不可取消
    WAITING_OPERATION = "waiting_operation"
    COMPLETED = "completed"
    FAILED = "failed"
    KB_FAILED = "kb_failed"
    CANCELLED = "cancelled"


FailureKind = Literal[
    "knowledge", "rejections", "turn_cap", "time_limit", "diagnostic_cap", "tool_cap",
    "model_unavailable", "closing_round_unanswered", "legacy_runtime_retired",
]


class TriageTask(StrictModel):
    schema_version: Literal[1] = 1
    tool_contract_version: Literal[1] = 1
    tool_result_schema_version: Literal[1] = 1
    task_id: str
    # One Task may be resumed after a terminal state.  Each active execution
    # batch gets a new id so its in-memory RemoteSession cannot cross rounds.
    execution_id: Optional[str] = None
    source: TaskSource
    status: TaskStatus
    created_at: str
    updated_at: str
    # 创建人用户 ID;None = 旧任务或未设本机用户,读时按数据根所有者推断
    created_by: Optional[str] = None
    initial_message: str
    display_title: Optional[str] = None
    intent: Optional[str] = None
    platform: Optional[str] = None
    node: Optional[str] = None
    service: Optional[str] = None
    component: Optional[str] = None
    # cluster = 集群级调查:node 仅是自动选定的入口节点,UI 展示「集群」
    scope: Optional[Literal["node", "cluster"]] = None
    alert_key: Optional[str] = None
    occurrence_key: Optional[str] = None
    alert_event: Optional[Dict[str, Any]] = None
    # 新任务按用户意图固定主类型；旧任务缺失时按 default 兼容。
    task_type: Optional[TaskType] = None
    task_type_confirmed: bool = True
    task_type_confidence: Optional[float] = Field(default=None, ge=0, le=1)
    task_type_source: Optional[TaskTypeSource] = None
    classifier_version: Optional[str] = None
    # Program/model classification is kept as an audit snapshot when the two
    # disagree and the task is waiting for the operator to choose a side.
    classification_audit: Optional[Dict[str, Any]] = None
    context_missing: List[str] = Field(default_factory=list)
    context_query: Optional[str] = None
    polling_snapshot: Optional[Dict[str, Any]] = None
    # Compact risk observation supplied by a resource-risk task entry point.
    # Full history stays in the resource-risk store and is loaded on demand.
    risk_seed: Optional[Dict[str, Any]] = None
    agent_id: Optional[str] = None
    agent_prompt_id: Optional[str] = None
    agent_prompt_version: Optional[str] = None
    agent_prompt_hash: Optional[str] = None
    base_prompt_version: Optional[str] = None
    agent_input_schema_version: Literal[1] = 1
    agent_policy_snapshot: Optional[Dict[str, Any]] = None
    # Prometheus 告警或复制告警文本归一后的安全快照，不含凭证。
    alert_context: Optional[Dict[str, Any]] = None
    active_agent: Optional[str] = None
    agent_path: List[str] = Field(default_factory=list)
    context_pack_ref: Optional[str] = None
    context_pack_snapshot: Optional[Dict[str, Any]] = None
    loop_phase: Optional[str] = None
    loop_stop_reason: Optional[str] = None
    current_hypothesis: Optional[Dict[str, Any]] = None
    hypothesis_history: List[Dict[str, Any]] = Field(default_factory=list)
    artifact_refs: List[str] = Field(default_factory=list)
    model_profile_id: Optional[str] = None
    model_title: Optional[str] = None
    model_name: Optional[str] = None
    reasoning_effort: Optional[
        Literal["none", "low", "medium", "high", "xhigh"]
    ] = None
    # 随模型快照固化的 Harness 档位;None 为升级前任务或未声明档位的 profile
    harness_tier: Optional[Literal["autonomous", "guided", "structured"]] = None
    # 新运行时首次执行时固化的档位开关取值(投影长度、软预算等),任务内不随代码常量漂移
    harness_switches: Optional[Dict[str, Any]] = None
    # 新运行时首次执行时冻结的插件(MCP)工具快照;None 为升级前任务,无插件工具
    plugin_tools: Optional[Dict[str, Any]] = None
    # 本任务的命令档位覆盖;覆盖的是档位不是永久白名单, None 表示跟随全局。
    command_mode: Optional[Literal["whitelist", "auto", "bypass"]] = None
    # 新版 Web 人工排查由右侧操作显式生成知识草稿与 worklog。
    # 默认 False 保持升级前任务和 API 调用的可恢复语义。
    manual_artifacts: bool = False
    run_id: Optional[str] = None
    matched_doc: Optional[str] = None
    consulted_docs: List[str] = Field(default_factory=list)
    report_path: Optional[str] = None
    kb_doc: Optional[str] = None
    kb_declined: bool = False
    # 最近一次已确认写入 worklog 的任务事件游标。后续预览只消费增量事件。
    worklog_cursor: Optional[str] = None
    worklog_entries: int = 0
    deep_dive: bool = False
    tool_turns: int = 0
    diagnostic_calls: int = 0
    # Harness tool budget ledger.  These counters are persisted with the Task
    # so a worker restart cannot reset per-tool quotas or accounting.
    tool_call_counts: Dict[str, int] = Field(default_factory=dict)
    tool_target_counts: Dict[str, int] = Field(default_factory=dict)
    tool_target_runtime_seconds: Dict[str, float] = Field(default_factory=dict)
    tool_runtime_seconds: float = 0.0
    tool_output_bytes: int = 0
    # 人工批准的续期次数:每次放宽一档软预算(见 agent_runtime/tiers.py 的 GRANT_*)
    budget_grants: int = 0
    # 本次排查内已放行的命令签名(四档裁决里的「本次排查默认」)。
    # 只在本任务有效,不落永久名单——那是 scope=always 的事
    command_defaults: List[str] = Field(default_factory=list)
    # 已退役的一次性放行券:批准后改由 Harness 直接执行原调用,不再发券也不再消费;
    # 字段仅为兼容旧 task.json 保留
    command_grants: List[str] = Field(default_factory=list)
    error: Optional[str] = None
    failure_kind: Optional[FailureKind] = None
    lease_owner: Optional[str] = None
    lease_expires_at: Optional[str] = None
    pending_invocation_id: Optional[str] = None
    # waiting_operation 任务下一次跟踪轮询的时间(UTC ISO);其余状态为 None
    operation_poll_at: Optional[str] = None
    archived_at: Optional[str] = None

    @classmethod
    def new_manual(cls, task_id: str, message: str) -> "TriageTask":
        now = datetime.now(timezone.utc).isoformat()
        return cls(
            task_id=task_id,
            source=TaskSource.MANUAL,
            status=TaskStatus.QUEUED,
            created_at=now,
            updated_at=now,
            initial_message=message,
        )


class TaskEvent(StrictModel):
    schema_version: Literal[1] = 1
    event_id: str
    task_id: str
    ts: str
    type: Literal[
        "user_message",
        "assistant_message",
        "tool_call",
        "tool_result",
        "tool_rejection_chain",
        "approval_requested",
        "approval_decided",
        "task_type_decided",
        "task_type_clarification_requested",
        "task_type_confirmed",
        "task_context_clarification_requested",
        "task_context_confirmed",
        "model_retry",
        "harness_feedback",
        "agent_selected",
        "context_pack_built",
        "history_compacted",
        "agent_playbook_result",
        "hypothesis_proposed",
        "evidence_plan_created",
        "hypothesis_reviewed",
        "agent_decision",
        "agent_handoff_requested",
        "agent_handoff_decided",
        "agent_handoff_accepted",
        "agent_handoff_completed",
        "loop_finished",
        # 新运行时(A2):开场、追加提醒、usage、收口回合、档位变更
        "agent_opening",
        "harness_notice",
        "model_usage",
        "budget_reminder",
        "closing_round",
        "closing_round_unanswered",
        "harness_tier_changed",
        # 续期裁决已兑现(无 tool_call 的审批靠它防重入时重复兑现)
        "budget_decision_applied",
        # 人工批准的插件写操作(M6):写前日志 → 插件返回 → 已下发 → 跟踪进度 → 结案
        "operation_executing",
        "operation_executed",
        "operation_submitted",
        "operation_progress",
        "operation_finished",
        "status",
        "error",
    ]
    payload: Dict[str, Any] = Field(default_factory=dict)


class ToolInvocation(StrictModel):
    invocation_id: str
    status: Literal["pending", "done", "failed"]
    result: Optional[Dict[str, Any]] = None


class TaskEvents(StrictModel):
    schema_version: Literal[1] = 1
    items: List[TaskEvent] = Field(default_factory=list)
