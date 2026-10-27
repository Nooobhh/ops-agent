from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple

from pydantic import Field, field_validator

from lib.datastore import UserDirectory
from lib.envpack import active_env_pack
from lib.envpack.fields import TargetPlatform

from .harness_tier import LEGACY_TASK_MESSAGE, uses_new_runtime
from .model_registry import ModelRegistry, ModelRegistryError, ReasoningEffort
from .task_classifier import (
    TaskClassifier, extract_service_component, extract_service_components,
    parse_alert_text,
)
from .models import (
    StrictModel,
    TaskEvent,
    TaskSource,
    TaskStatus,
    TriageTask,
)
from .task_types import (
    AlertEnvelope,
    TaskType,
    envelope_from_mapping,
)
from .task_store import InvalidTransition, TaskStore


_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CLOSED_STATUSES = {
    TaskStatus.CANCELLED,
}
_NON_CANCELLABLE_STATUSES = {
    TaskStatus.COMPLETED,
    TaskStatus.CANCELLED,
    TaskStatus.FAILED,
    TaskStatus.KB_FAILED,
}
_ARCHIVABLE_STATUSES = _NON_CANCELLABLE_STATUSES
_LIVE_QUERY = re.compile(r"当前|现在|实时|状态|占用|使用率|负载|内存|磁盘|cpu|load", re.I)
_SERVICE_TARGET = re.compile(r"服务|组件|程序|进程|接口|流程|service|component", re.I)
_PLATFORM_WIDE = re.compile(
    r"平台级|整个平台|全平台|集群级|整集群|整个集群|全集群|"
    r"所有服务|全部服务|各服务|多个服务|多服务|"
    r"所有组件|全部组件|各组件|多个组件|多组件|"
    r"跨服务|跨组件|整体|全局"
)
_ANSWER_FIELD = re.compile(r"(?:^|[\s,，;；])(?P<key>服务|组件|service|component)\s*[:=：]\s*(?P<value>[\w-]{1,64})", re.I)
_NODE_KEY = re.compile(r"(?<![a-z0-9_])node[-_]?\d+(?![a-z0-9_])", re.I)
_HOST_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_MAX_HOST_PROBES = 20


class TaskConflict(RuntimeError):
    pass


class InvalidTaskNode(TaskConflict):
    def __init__(self, node: str, platform: str, available_nodes: List[str]):
        super().__init__(f"节点 {node} 未在平台 {platform} 登记")
        self.node = node
        self.platform = platform
        self.available_nodes = available_nodes


class InvalidTaskIdentifier(ValueError):
    pass


class CreateTaskRequest(StrictModel):
    message: str = Field(min_length=1, max_length=4000)
    platform: Optional[TargetPlatform] = None
    node: Optional[str] = Field(default=None, min_length=1, max_length=128)
    service: Optional[str] = Field(default=None, min_length=1, max_length=128)
    component: Optional[str] = Field(default=None, min_length=1, max_length=128)
    model_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    reasoning_effort: Optional[ReasoningEffort] = None
    command_mode: Optional[Literal["whitelist", "auto", "bypass"]] = None
    manual_artifacts: bool = False
    alert_context: Optional[AlertEnvelope] = None
    task_type: Optional[TaskType] = None
    # Server-validated compact observation from the resource-risk entry point.
    risk_seed: Optional[Dict[str, Any]] = None

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("问题描述不能为空")
        return stripped

    @field_validator("node")
    @classmethod
    def validate_node(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("节点不能为空")
        return stripped


class TaskListRequest(StrictModel):
    status: Optional[TaskStatus] = None
    source: Optional[TaskSource] = None
    alert_key: Optional[str] = None
    limit: int = Field(default=50, ge=1, le=200)


class TaskDetail(StrictModel):
    schema_version: Literal[1] = 1
    task: TriageTask
    events: List[TaskEvent]
    # 由 (platform, node, scope) 派生的资源 ID,只读展示,不落盘
    target_ref: Optional[str] = None


class TaskListResponse(StrictModel):
    schema_version: Literal[1] = 1
    items: List[TriageTask]


class TaskEventsResponse(StrictModel):
    schema_version: Literal[1] = 1
    items: List[TaskEvent]


class TriageTaskService:
    def __init__(
        self,
        store: TaskStore,
        *,
        model_registry: Optional[ModelRegistry] = None,
        classifier: Optional[TaskClassifier] = None,
        classification_model_factory: Optional[Callable[[Dict[str, Any]], Any]] = None,
        polling_snapshot_reader: Optional[Callable[[str, Optional[str]], Optional[Dict[str, Any]]]] = None,
        users: Optional[UserDirectory] = None,
        target_ref_resolver: Optional[Callable[[TriageTask], Optional[str]]] = None,
    ):
        self.store = store
        self.target_ref_resolver = target_ref_resolver
        # 本机用户目录:人工任务缺省创建人与自动任务创建人都取数据根所有者
        self.users = users
        self.model_registry = model_registry
        self.classifier = classifier or TaskClassifier()
        self.classification_model_factory = classification_model_factory
        self.polling_snapshot_reader = polling_snapshot_reader

    def create_manual(
        self,
        request: CreateTaskRequest,
        *,
        resolve_node=None,
        infer_platform=None,
        known_nodes=None,
        created_by: Optional[str] = None,
    ) -> TaskDetail:
        envelope = envelope_from_mapping(request.alert_context)
        # A pasted Prometheus alert is the supported manual-task input path as
        # well.  Keep the parsed envelope in the task snapshot; the classifier
        # also parses it, but its decision intentionally contains only routing
        # fields and is not the durable alert source of truth.
        if envelope is None:
            envelope = parse_alert_text(request.message)
        platform = request.platform or (envelope.platform if envelope else None)
        # 粘贴的通知只有告警主机:分类前先按它推断平台,与告警页入口(node=主机)口径一致,
        # 否则无路由告警会因「平台未知」误入 AlertAgent
        node = request.node or (
            (envelope.node or envelope.host_raw) if envelope else None
        )
        if platform is None and node and infer_platform is not None:
            platform = infer_platform(node)
        if platform and node and resolve_node is not None:
            canonical = resolve_node(platform, node)
            # Preserve the legacy create response for an unclassified node;
            # the first remote action still hard-fails at the SSHPool boundary.
            # Known keys/IPs are canonicalised immediately so normal tasks do
            # not carry an address or alias through the Agent loop.
            if canonical is not None:
                node = canonical
        # Resolve the selected manual model before classification so the
        # classifier and the subsequent Agent use the same explicit model.
        initial_selection = self._resolve_selection(
            "manual",
            model_id=request.model_id,
            reasoning_effort=request.reasoning_effort,
        )
        classification_model = None
        if self.classification_model_factory is not None and initial_selection:
            classification_model = self.classification_model_factory(initial_selection)
        outcome = self.classifier.classify_and_summarize(
            request.message,
            alert_context=envelope,
            platform=platform,
            node=node,
            service=request.service,
            model=classification_model,
        )
        decision = outcome.final
        if request.task_type is not None:
            # Explicit entry points (for example a resource-risk card) may
            # pin the workflow type while still retaining the classifier
            # output in classification_audit for inspection.
            decision = decision.model_copy(update={
                "task_type": request.task_type,
                "task_type_confirmed": True,
                "source": "user_confirmed",
                "scope": "node" if node else decision.scope,
            })
            outcome = outcome.model_copy(update={
                "final": decision,
                "needs_review": False,
            })
        if outcome.needs_review:
            # A2 §5.5 不让用户选类型:取程序判断,模型判断与冲突留在 classification_audit;
            # 程序没判出类型(default)不算冲突,取模型判断,否则丢掉该类型的开场材料
            chosen = outcome.final if outcome.program.task_type == "default" else outcome.program
            decision = chosen.model_copy(update={"task_type_confirmed": True})
            outcome = outcome.model_copy(update={"final": decision, "needs_review": False})
        # Remote target identity must come from the request/alert or an explicit
        # textual match, not a model guess based only on a service name.
        platform = platform or outcome.program.platform
        service = request.service or decision.service
        component = request.component or decision.component
        if outcome.program.node and (
            node is None
            or envelope is not None and node == envelope.host_raw
        ):
            node = outcome.program.node
        if node is None:
            platform, node = _named_node(
                request.message, platform,
                resolve_node=resolve_node, infer_platform=infer_platform,
            )
        if platform is None and node and infer_platform is not None:
            platform = infer_platform(node)
        if platform and node and resolve_node is not None:
            canonical = resolve_node(platform, node)
            if canonical is not None:
                node = canonical
        scope = decision.scope or (
            "node" if node or decision.task_type == "alert" else "cluster"
            if _needs_runtime_target(decision.task_type, request.message) else None
        )
        if platform and scope == "cluster" and not node and known_nodes is not None:
            nodes = list(known_nodes(platform))
            node = nodes[0] if nodes else None
        context_missing = _missing_context(
            decision.task_type, request.message, platform=platform,
            node=node, scope=scope, service=service, component=component,
        )
        if envelope is not None:
            envelope = envelope.model_copy(update={
                "platform": platform or envelope.platform,
                "node": node or envelope.node,
            })
        risk_seed = _normalize_risk_seed(
            request.risk_seed,
            platform=platform,
            node=node,
        )
        selection = self._resolve_task_type_selection(
            decision.task_type,
            model_id=request.model_id,
            reasoning_effort=request.reasoning_effort,
        )
        policy = self._policy_snapshot(
            decision.task_type,
            model_id=selection.get("model_profile_id"),
            reasoning_effort=selection.get("reasoning_effort"),
        )
        task = self.store.create_manual(
            request.message,
            platform=platform,
            node=node,
            service=service,
            component=component,
            context_missing=context_missing,
            scope=scope,
            display_title=outcome.display_title,
            intent=outcome.intent,
            **selection,
            command_mode=request.command_mode,
            manual_artifacts=request.manual_artifacts,
            task_type=decision.task_type,
            task_type_confirmed=True,
            task_type_confidence=decision.confidence,
            task_type_source=decision.source,
            classifier_version=decision.classifier_version,
            classification_audit=outcome.model_dump(mode="json"),
            polling_snapshot=self._read_polling_snapshot(platform, node),
            risk_seed=risk_seed,
            agent_policy_snapshot=policy,
            alert_context=(
                envelope.model_dump(mode="json") if envelope is not None else None
            ),
            created_by=created_by or self._owner_id(),
        )
        self.store.append_event(
            task.task_id,
            "task_type_decided",
            {
                **decision.model_dump(mode="json"),
                "program": outcome.program.model_dump(mode="json"),
                "model": outcome.model.model_dump(mode="json") if outcome.model else None,
                "conflicts": outcome.conflicts,
                "display_title": outcome.display_title,
                "intent": outcome.intent,
            },
        )
        if context_missing:
            task = self._request_context(task)
        return self._detail(task)

    def enqueue_auto(
        self,
        occurrence_key: str,
        alert_event: Dict[str, Any],
    ) -> Tuple[TaskDetail, bool]:
        # Keep the legacy enqueue_auto contract for historical alert tasks.
        # New Prometheus alerts will be created through the normal task path
        # and receive the immutable task-type policy instead.
        selection = self._resolve_selection("auto")
        task, created = self.store.enqueue_auto(
            occurrence_key,
            alert_event,
            created_by=self._owner_id(),
            **selection,
        )
        if created:
            task = self.store.update(
                task.task_id,
                expected=TaskStatus.QUEUED,
                agent_policy_snapshot=self._policy_snapshot(
                    "alert",
                    model_id=selection.get("model_profile_id"),
                    reasoning_effort=selection.get("reasoning_effort"),
                ),
            )
        return self._detail(task), created

    def list_tasks(self, request: TaskListRequest) -> TaskListResponse:
        tasks = self.store.list(limit=200)
        if request.status is not None:
            tasks = [task for task in tasks if task.status is request.status]
        if request.source is not None:
            tasks = [task for task in tasks if task.source is request.source]
        if request.alert_key is not None:
            tasks = [
                task for task in tasks
                if task.alert_key == request.alert_key
            ]
        return TaskListResponse(items=tasks[:request.limit])

    def get_task(self, task_id: str) -> TaskDetail:
        task = self.store.get(_validate_identifier(task_id, "任务 ID"))
        return self._detail(task)

    def add_message(
        self,
        task_id: str,
        message: str,
        *,
        resolve_node=None,
        infer_platform=None,
        known_nodes=None,
    ) -> TaskDetail:
        identifier = _validate_identifier(task_id, "任务 ID")
        request = CreateTaskRequest(message=message)
        task = self.store.get(identifier)
        if task.source is TaskSource.AUTO:
            raise TaskConflict("自动排查任务不能追加消息")
        if not uses_new_runtime(task):
            raise TaskConflict(LEGACY_TASK_MESSAGE)
        if task.status in _CLOSED_STATUSES:
            raise TaskConflict("已关闭任务不能追加消息")
        task_updates = self._alert_message_updates(
            task,
            request.message,
            resolve_node=resolve_node,
            infer_platform=infer_platform,
        )
        if task.context_missing:
            task_updates.update(_context_reply_updates(
                task, request.message,
                resolve_node=resolve_node,
                infer_platform=infer_platform,
                known_nodes=known_nodes,
            ))
        try:
            changed = self.store.resume_with_message(
                identifier,
                request.message,
                task_updates=task_updates or None,
            )
        except InvalidTransition as exc:
            raise TaskConflict(str(exc)) from exc
        if task.context_missing:
            if changed.context_missing:
                changed = self._request_context(changed)
            else:
                self.store.append_event(changed.task_id, "task_context_confirmed", {
                    "platform": changed.platform,
                    "node": changed.node,
                    "scope": changed.scope,
                    "service": changed.service,
                    "component": changed.component,
                })
        return self._detail(changed)

    def update_runtime_controls(
        self,
        task_id: str,
        updates: Dict[str, Any],
        *,
        known_nodes=None,
        resolve_node=None,
    ) -> TaskDetail:
        identifier = _validate_identifier(task_id, "任务 ID")
        task = self.store.get(identifier)
        if task.source is TaskSource.AUTO:
            raise TaskConflict("自动排查任务不能修改运行时控制项")
        if not uses_new_runtime(task):
            raise TaskConflict(LEGACY_TASK_MESSAGE)
        if task.status is TaskStatus.CANCELLED:
            raise TaskConflict("已取消任务不能修改运行时控制项")
        if task.status is TaskStatus.WAITING_OPERATION:
            raise TaskConflict("写操作执行中,结束后再修改运行时控制项")

        next_platform = updates.get("platform", task.platform)
        next_node = updates.get("node", task.node)
        # 待澄清任务可能只有原样 nodeN 尚无平台;只改模型时不校验目标
        if next_node is not None and {"platform", "node"} & updates.keys():
            if next_platform is None:
                raise TaskConflict("设置节点前必须先选择平台")
            # 空表意味着「该平台没有合法节点」而非「跳过校验」——后者会在拓扑
            # 读不出来时静默放行任何脏节点
            if known_nodes is not None:
                available = list(known_nodes(next_platform))
                canonical = resolve_node(next_platform, next_node) if resolve_node else next_node
                if canonical is None or canonical not in available:
                    raise InvalidTaskNode(next_node, next_platform, available)
                next_node = canonical

        changes: Dict[str, Any] = {}
        for key in ("platform", "node", "command_mode"):
            if key in updates:
                changes[key] = updates[key]

        if "model_id" in updates or "reasoning_effort" in updates:
            if updates.get("model_id", task.model_profile_id) is None:
                changes.update({
                    "model_profile_id": None,
                    "model_title": None,
                    "model_name": None,
                    "reasoning_effort": None,
                })
            elif self.model_registry is None:
                raise ModelRegistryError("模型注册表不可用")
            else:
                selection = self.model_registry.resolve_for_task_type(
                    task.task_type or "default",
                    model_id=updates.get("model_id", task.model_profile_id),
                    reasoning_effort=updates.get(
                        "reasoning_effort", task.reasoning_effort,
                    ),
                )
                changes.update(selection.model_dump())

        if "platform" in updates and updates["platform"] is None and "node" not in updates:
            # 清空平台后不保留一个无法解释归属的节点。
            changes["node"] = None
        changed = self.store.update(
            identifier,
            expected=task.status,
            **changes,
        )
        return self._detail(changed)

    def retry(self, task_id: str) -> TaskDetail:
        identifier = _validate_identifier(task_id, "任务 ID")
        task = self.store.get(identifier)
        if task.source is TaskSource.AUTO:
            raise TaskConflict("自动排查任务不能重试")
        if not uses_new_runtime(task):
            raise TaskConflict(LEGACY_TASK_MESSAGE)
        try:
            changed = self.store.retry_failed(identifier)
        except InvalidTransition as exc:
            raise TaskConflict(str(exc)) from exc
        return self._detail(changed)

    def events(
        self,
        task_id: str,
        after: Optional[str] = None,
    ) -> TaskEventsResponse:
        identifier = _validate_identifier(task_id, "任务 ID")
        cursor = (
            _validate_identifier(after, "事件游标")
            if after is not None
            else None
        )
        return TaskEventsResponse(
            items=self.store.events(identifier, after=cursor)
        )

    def decide_approval(
        self,
        task_id: str,
        approval_id: str,
        *,
        decision: str,
        feedback: Optional[str] = None,
        scope: Optional[str] = None,
        decided_by: Optional[Dict[str, str]] = None,
    ) -> TaskDetail:
        identifier = _validate_identifier(task_id, "任务 ID")
        approval = _validate_identifier(approval_id, "审批 ID")
        note = (feedback or "").strip()
        if decision == "revise" and not note:
            raise TaskConflict("要求修改草稿时必须填写修改意见")
        if decision == "approve" and not decided_by and self._pending_kind(identifier, approval) == "operation":
            # 写操作的批准必须落审批人(审计与 oplog);一阶段由本机用户给出
            raise TaskConflict("写操作审批需要审批人身份:请先在「设置 → 本机用户」填写")
        try:
            changed = self.store.resolve_approval(
                identifier,
                approval,
                decision=decision,
                feedback=note,
                scope=scope,
                decided_by=decided_by,
            )
        except InvalidTransition as exc:
            raise TaskConflict(str(exc)) from exc
        return self._detail(changed)

    def set_archived(self, task_id: str, archived: bool) -> TaskDetail:
        identifier = _validate_identifier(task_id, "任务 ID")
        current = self.store.get(identifier)
        if archived and current.status not in _ARCHIVABLE_STATUSES:
            raise TaskConflict("只有已结束的任务可以归档")
        return self._detail(self.store.set_archived(identifier, archived))

    def rename(self, task_id: str, title: str) -> TaskDetail:
        identifier = _validate_identifier(task_id, "任务 ID")
        current = self.store.get(identifier)
        return self._detail(self.store.update(
            identifier,
            expected=current.status,
            display_title=title,
        ))

    def cancel(self, task_id: str) -> TaskDetail:
        identifier = _validate_identifier(task_id, "任务 ID")
        current = self.store.get(identifier)
        if current.status in _NON_CANCELLABLE_STATUSES:
            raise TaskConflict("终态任务不能取消")
        if current.status is TaskStatus.WAITING_OPERATION:
            raise TaskConflict("写操作执行中,Harness 跟踪到结束后才能取消")
        try:
            changed = self.store.transition(
                identifier,
                current.status,
                TaskStatus.CANCELLED,
                lease_owner=None,
                lease_expires_at=None,
            )
        except InvalidTransition as exc:
            raise TaskConflict(str(exc)) from exc
        return self._detail(changed)

    def complete(self, task_id: str) -> TaskDetail:
        """人工标记任务完成；只改状态，不触发任何附属产物。"""
        identifier = _validate_identifier(task_id, "任务 ID")
        current = self.store.get(identifier)
        if current.source is TaskSource.AUTO:
            raise TaskConflict("自动任务由系统收口")
        if current.status is not TaskStatus.WAITING_INPUT:
            raise TaskConflict("只有 Agent 已回复并等待用户时才能标记完成")
        try:
            changed = self.store.transition(
                identifier,
                TaskStatus.WAITING_INPUT,
                TaskStatus.COMPLETED,
                lease_owner=None,
                lease_expires_at=None,
            )
        except InvalidTransition as exc:
            raise TaskConflict(str(exc)) from exc
        return self._detail(changed)

    def _detail(self, task: TriageTask) -> TaskDetail:
        return TaskDetail(
            task=task,
            events=self.store.events(task.task_id),
            target_ref=self.target_ref_resolver(task) if self.target_ref_resolver else None,
        )

    def _pending_kind(self, task_id: str, approval_id: str) -> Optional[str]:
        for event in reversed(self.store.events(task_id)):
            if event.type == "approval_requested" and event.payload.get("approval_id") == approval_id:
                return str(event.payload.get("kind") or "")
        return None

    def _owner_id(self) -> Optional[str]:
        return self.users.owner_id() if self.users is not None else None

    def _resolve_selection(
        self,
        source: Literal["auto", "manual"],
        *,
        model_id: Optional[str] = None,
        reasoning_effort: Optional[ReasoningEffort] = None,
    ) -> Dict[str, Optional[str]]:
        if self.model_registry is None:
            return {}
        selection = self.model_registry.resolve(
            source,
            model_id=model_id,
            reasoning_effort=reasoning_effort,
        )
        return selection.model_dump()

    def _resolve_task_type_selection(
        self,
        task_type: TaskType,
        *,
        model_id: Optional[str] = None,
        reasoning_effort: Optional[ReasoningEffort] = None,
    ) -> Dict[str, Optional[str]]:
        if self.model_registry is None:
            return {}
        selection = self.model_registry.resolve_for_task_type(
            task_type,
            model_id=model_id,
            reasoning_effort=reasoning_effort,
        )
        return selection.model_dump()

    def _policy_snapshot(
        self,
        task_type: TaskType,
        *,
        model_id: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if self.model_registry is None:
            return None
        policy = self.model_registry.agent_policy(task_type)
        if policy is None:
            return None
        snapshot = policy.model_dump(mode="json")
        if model_id:
            snapshot["model_id"] = model_id
        if reasoning_effort:
            snapshot["reasoning_effort"] = reasoning_effort
        return snapshot

    def _request_context(self, task: TriageTask) -> TriageTask:
        missing = task.context_missing
        if "platform" in missing:
            identified = f"已识别服务 {task.service}。" if task.service else ""
            question = f"{identified}请确认本任务所属平台；也可在输入框补充服务、组件和节点。"
            options = [  # 可排查的集群(托管或直连 MySQL),按环境包声明顺序给显示名
                cluster.display_name for cluster in active_env_pack().clusters
                if {"managed", "mysql_direct"} & set(cluster.roles)
            ]
        elif "service" in missing:
            question = "请补充需要排查的服务或组件名称（例如 HDFS DataNode、YARN ResourceManager）。"
            options = []
        else:
            question = "请补充目标节点名称；跨节点调查可回复“整个集群”。"
            options = []
        self.store.append_event(task.task_id, "task_context_clarification_requested", {
            "kind": "task_context",
            "question": question,
            "options": options,
            "task_type": task.task_type,
            "missing": missing,
        })
        return self.store.transition(
            task.task_id, TaskStatus.QUEUED, TaskStatus.WAITING_INPUT,
        )

    def _read_polling_snapshot(
        self,
        platform: Optional[str],
        node: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        if not platform or self.polling_snapshot_reader is None:
            return None
        try:
            value = self.polling_snapshot_reader(platform, node)
        except Exception:  # noqa: BLE001
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _alert_message_updates(
        task: TriageTask,
        message: str,
        *,
        resolve_node=None,
        infer_platform=None,
    ) -> Dict[str, Any]:
        """Persist a newly pasted alert without reclassifying the task.

        ``task_type`` is immutable after confirmation.  A follow-up message
        may nevertheless supply the missing structured alert payload needed by
        AlertAgent, so only the alert context and canonical target fields are
        amended here.
        """
        if task.task_type != "alert":
            return {}
        parsed = parse_alert_text(message)
        if parsed is None or not parsed.alertname:
            return {}

        platform = task.platform or parsed.platform
        if platform is None and parsed.host_raw and infer_platform is not None:
            platform = infer_platform(parsed.host_raw)
        node = task.node or parsed.node
        if node is None and platform and parsed.host_raw and resolve_node is not None:
            node = resolve_node(platform, parsed.host_raw)

        existing = envelope_from_mapping(task.alert_context)
        if existing is not None:
            parsed = existing.model_copy(update={
                "source": parsed.source,
                "fingerprint": parsed.fingerprint or existing.fingerprint,
                "alertname": parsed.alertname or existing.alertname,
                "labels": {**existing.labels, **parsed.labels},
                "annotations": {**existing.annotations, **parsed.annotations},
                "platform": platform or existing.platform,
                "host_raw": parsed.host_raw or existing.host_raw,
                "node": node or existing.node,
                "service": parsed.service or existing.service,
                "severity": parsed.severity or existing.severity,
                "state": parsed.state or existing.state,
                "active_at": parsed.active_at or existing.active_at,
                "value": parsed.value or existing.value,
                "detail": parsed.detail or existing.detail,
                "raw_ref": parsed.raw_ref or existing.raw_ref,
            })
        else:
            parsed = parsed.model_copy(update={
                "platform": platform or parsed.platform,
                "node": node or parsed.node,
            })

        updates: Dict[str, Any] = {
            "alert_context": parsed.model_dump(mode="json"),
        }
        if platform and not task.platform:
            updates["platform"] = platform
        if node and not task.node:
            updates["node"] = node
        if task.scope is None and (node or parsed.host_raw):
            updates["scope"] = "node"
        # The prior pack may have been built before the alert was supplied.
        # Force one fresh snapshot on the next worker turn.
        if task.context_pack_ref:
            updates["context_pack_ref"] = None
            updates["context_pack_snapshot"] = None
        return updates


def _validate_identifier(value: str, label: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(str(value)):
        raise InvalidTaskIdentifier(f"{label} 格式不合法")
    return str(value)


def _needs_runtime_target(task_type: str, message: str) -> bool:
    return task_type in {"investigation", "alert"} or (
        task_type == "qa" and bool(_LIVE_QUERY.search(message))
    )


def _missing_context(
    task_type: str,
    message: str,
    *,
    platform: Optional[str],
    node: Optional[str],
    scope: Optional[str],
    service: Optional[str],
    component: Optional[str],
) -> List[str]:
    if not _needs_runtime_target(task_type, message):
        return []
    missing = []
    if not platform:
        missing.append("platform")
    named_services, named_components = extract_service_components(message)
    if (
        not service and not component
        and not _PLATFORM_WIDE.search(message)
        and not named_services and not named_components
        and _SERVICE_TARGET.search(message)
    ):
        missing.append("service")
    if scope == "node" and not node:
        missing.append("node")
    return missing


def _platform_from_reply(message: str) -> Optional[str]:
    # managed 集群认裸短键;其余集群认显示名或「平台: 短键」,按 env.yaml 声明顺序取第一个
    for cluster in active_env_pack().clusters:
        key = re.escape(cluster.legacy_key)
        if "managed" in cluster.roles:
            pattern = rf"(?<![a-z0-9_]){key}(?![a-z0-9_])"
        else:
            label = re.escape(cluster.display_name).replace(r"\ ", r"\s*")
            pattern = rf"{label}|平台\s*[:=：]\s*{key}"
        if re.search(pattern, message, re.I):
            return cluster.legacy_key
    return None


def _named_node(
    text: str,
    platform: Optional[str],
    *,
    resolve_node=None,
    infer_platform=None,
) -> Tuple[Optional[str], Optional[str]]:
    """Return (platform, node) for a node named by key, IP, alias or FQDN.

    Only tokens that resolve to a registered node count; a bare ``nodeN`` is
    kept raw so a later platform answer can still resolve it.  Every probe
    re-reads topology yaml, so only host-shaped tokens (registered identities
    all contain a digit, ``-`` or ``.``) are tried, at most _MAX_HOST_PROBES.
    """
    if resolve_node is not None:
        tokens: List[str] = []
        for raw in _HOST_TOKEN.findall(text):
            token = raw.strip("._-")
            if re.search(r"[\d.-]", token) and token not in tokens:
                tokens.append(token)
        for token in tokens[:_MAX_HOST_PROBES]:
            target = platform or (infer_platform(token) if infer_platform else None)
            node = resolve_node(target, token) if target else None
            if node:
                return target, node
    match = _NODE_KEY.search(text)
    return platform, match.group(0).lower() if match else None


def _context_reply_updates(
    task: TriageTask,
    message: str,
    *,
    resolve_node=None,
    infer_platform=None,
    known_nodes=None,
) -> Dict[str, Any]:
    original = task.initial_message
    prior = task.context_query or original
    supplement = prior[len(original):] if prior.startswith(original) else prior
    supplement = f"{supplement}\n{message}"[-8000:].strip()
    context_query = f"{original}\n{supplement}"
    platform, named_node = _named_node(
        message, task.platform or _platform_from_reply(message),
        resolve_node=resolve_node, infer_platform=infer_platform,
    )
    node = named_node or task.node
    if not platform and node and infer_platform is not None:
        platform = infer_platform(node)
    if platform and node and resolve_node is not None:
        node = resolve_node(platform, node)
    service, component = extract_service_component(message)
    service = task.service or service
    component = task.component or component
    for field in _ANSWER_FIELD.finditer(message):
        if field.group("key").lower() in {"服务", "service"}:
            service = field.group("value").lower()
        else:
            component = field.group("value").lower()
    scope = task.scope
    if re.search(r"整个集群|全[部]?集群|跨节点", message):
        scope = "cluster"
        node = None
    elif named_node or node and scope is None:
        # 澄清回复里点名的节点优先于集群级自动选定的入口节点
        scope = "node"
    elif scope is None and _needs_runtime_target(task.task_type or "default", task.initial_message):
        scope = "cluster"
    if platform and scope == "cluster" and not node and known_nodes is not None:
        nodes = list(known_nodes(platform))
        node = nodes[0] if nodes else None
    missing = _missing_context(
        task.task_type or "default", context_query,
        platform=platform, node=node, scope=scope,
        service=service, component=component,
    )
    return {
        "platform": platform, "node": node, "scope": scope,
        "service": service, "component": component,
        "context_missing": missing,
        "context_query": context_query,
        "context_pack_ref": None,
        "context_pack_snapshot": None,
    }


_RISK_SEED_FIELDS = (
    "risk_ref", "source", "alert_type", "platform", "node", "service",
    "component", "job", "metric", "label", "resource", "title",
    "description", "value", "unit", "threshold", "severity", "samples",
    "first_observed_at", "last_updated_at", "snapshot_ref", "freshness_seconds",
)


def _normalize_risk_seed(
    value: Optional[Dict[str, Any]],
    *,
    platform: Optional[str],
    node: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Keep a compact, target-bound resource-risk observation."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TaskConflict("risk_seed 必须是对象")
    risk_ref = str(value.get("risk_ref") or "").strip()
    if not risk_ref or len(risk_ref) > 256 or not re.fullmatch(
        r"[A-Za-z0-9:_-]+", risk_ref
    ):
        raise TaskConflict("risk_seed.risk_ref 格式不合法")
    seed_platform = str(value.get("platform") or platform or "").strip().lower()
    seed_node = str(value.get("node") or node or "").strip()
    if platform and seed_platform != str(platform).lower():
        raise TaskConflict("risk_seed 平台与任务平台不一致")
    if node and seed_node != node:
        raise TaskConflict("risk_seed 节点与任务节点不一致")
    if not seed_platform or not seed_node:
        raise TaskConflict("risk_seed 缺少平台或节点")
    metric = str(value.get("metric") or "").strip()
    if metric and len(metric) > 128:
        raise TaskConflict("risk_seed.metric 不合法")
    seed = {
        key: value[key]
        for key in _RISK_SEED_FIELDS
        if key in value and value[key] is not None
    }
    seed.update({
        "risk_ref": risk_ref,
        "source": "resource_risk",
        "platform": seed_platform,
        "node": seed_node,
    })
    if metric:
        seed["metric"] = metric
    for key in ("service", "component"):
        if key in seed:
            seed[key] = str(seed[key]).strip().lower()[:256]
    for key in ("job", "label", "unit", "severity", "resource", "title", "alert_type"):
        if key in seed:
            seed[key] = str(seed[key])[:256]
    if "description" in seed:
        seed["description"] = str(seed["description"]).strip()[:1200]
    for key in ("samples", "freshness_seconds"):
        if key in seed:
            try:
                seed[key] = max(0, int(seed[key]))
            except (TypeError, ValueError) as exc:
                raise TaskConflict(f"risk_seed.{key} 不合法") from exc
    for key in ("value", "threshold"):
        if key in seed:
            try:
                seed[key] = float(seed[key])
            except (TypeError, ValueError) as exc:
                raise TaskConflict(f"risk_seed.{key} 不合法") from exc
    return seed
