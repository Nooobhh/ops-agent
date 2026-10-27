from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple

from lib.atomic_file import atomic_write_text
from lib.file_lock import FileLock

from .harness_tier import CURRENT_TIER
from .model_registry import ReasoningEffort
from .models import TaskEvent, TaskSource, TaskStatus, ToolInvocation, TriageTask
from .task_types import TaskType, TaskTypeSource


class TaskStoreError(RuntimeError):
    pass


class TaskNotFound(TaskStoreError):
    pass


class InvalidTransition(TaskStoreError):
    pass


_ALLOWED_TRANSITIONS = {
    TaskStatus.QUEUED: {
        TaskStatus.RUNNING, TaskStatus.WAITING_INPUT, TaskStatus.CANCELLED,
    },
    TaskStatus.RUNNING: {
        TaskStatus.WAITING_INPUT,
        TaskStatus.WAITING_APPROVAL,
        TaskStatus.WAITING_OPERATION,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.KB_FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.WAITING_INPUT: {
        TaskStatus.QUEUED, TaskStatus.COMPLETED, TaskStatus.CANCELLED,
    },
    TaskStatus.WAITING_APPROVAL: {TaskStatus.QUEUED, TaskStatus.CANCELLED},
    # 写操作执行中不可取消:中途放手会让结果与 oplog 断档(上游系统也无法经插件中止)
    TaskStatus.WAITING_OPERATION: {TaskStatus.QUEUED},
    TaskStatus.COMPLETED: set(),
    TaskStatus.FAILED: set(),
    TaskStatus.KB_FAILED: set(),
    TaskStatus.CANCELLED: set(),
}


class TaskStore:
    def __init__(
        self,
        root: Path,
        *,
        now: Optional[Callable[[], datetime]] = None,
        id_factory: Optional[Callable[[], str]] = None,
    ):
        self.root = Path(root)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or self._new_task_id
        self._lock_path = self.root / ".lock"
        self._claim_cache: Dict[Path, Tuple[Tuple[int, int, int], Tuple[str, Optional[str], str]]] = {}

    def create_manual(
        self,
        message: str,
        *,
        display_title: Optional[str] = None,
        intent: Optional[str] = None,
        platform: Optional[str] = None,
        node: Optional[str] = None,
        service: Optional[str] = None,
        component: Optional[str] = None,
        context_missing: Optional[List[str]] = None,
        scope: Optional[Literal["node", "cluster"]] = None,
        model_profile_id: Optional[str] = None,
        model_title: Optional[str] = None,
        model_name: Optional[str] = None,
        reasoning_effort: Optional[ReasoningEffort] = None,
        command_mode: Optional[Literal["whitelist", "auto", "bypass"]] = None,
        manual_artifacts: bool = False,
        task_type: Optional[TaskType] = None,
        task_type_confirmed: bool = True,
        task_type_confidence: Optional[float] = None,
        task_type_source: Optional[TaskTypeSource] = None,
        classifier_version: Optional[str] = None,
        classification_audit: Optional[Dict[str, Any]] = None,
        polling_snapshot: Optional[Dict[str, Any]] = None,
        risk_seed: Optional[Dict[str, Any]] = None,
        agent_id: Optional[str] = None,
        agent_prompt_id: Optional[str] = None,
        agent_prompt_version: Optional[str] = None,
        agent_prompt_hash: Optional[str] = None,
        base_prompt_version: Optional[str] = None,
        agent_input_schema_version: int = 1,
        agent_policy_snapshot: Optional[Dict[str, Any]] = None,
        alert_context: Optional[Dict[str, Any]] = None,
        created_by: Optional[str] = None,
    ) -> TriageTask:
        task_id = self._id_factory()
        now_iso = self._now_iso()
        task = TriageTask(
            task_id=task_id,
            source=TaskSource.MANUAL,
            status=TaskStatus.QUEUED,
            created_at=now_iso,
            updated_at=now_iso,
            created_by=created_by,
            initial_message=message,
            display_title=display_title,
            intent=intent,
            platform=platform,
            node=node,
            service=service,
            component=component,
            context_missing=context_missing or [],
            scope=scope,
            model_profile_id=model_profile_id,
            model_title=model_title,
            model_name=model_name,
            reasoning_effort=reasoning_effort,
                harness_tier=CURRENT_TIER,
            command_mode=command_mode,
            manual_artifacts=manual_artifacts,
            task_type=task_type,
            task_type_confirmed=task_type_confirmed,
            task_type_confidence=task_type_confidence,
            task_type_source=task_type_source,
            classifier_version=classifier_version,
            classification_audit=classification_audit,
            polling_snapshot=polling_snapshot,
            risk_seed=risk_seed,
            agent_id=agent_id,
            agent_prompt_id=agent_prompt_id,
            agent_prompt_version=agent_prompt_version,
            agent_prompt_hash=agent_prompt_hash,
            base_prompt_version=base_prompt_version,
            agent_input_schema_version=agent_input_schema_version,
            agent_policy_snapshot=agent_policy_snapshot,
            alert_context=alert_context,
        )
        with FileLock(str(self._lock_path)):
            if self._task_path(task_id).exists():
                raise TaskStoreError(f"任务已存在: {task_id}")
            self._write_task_unlocked(task)
            self._append_event_unlocked(
                task_id, "user_message", {"message": message}
            )
            self._append_event_unlocked(
                task_id, "status", {"status": TaskStatus.QUEUED.value}
            )
        return task

    def enqueue_auto(
        self,
        occurrence_key: str,
        alert_event: Dict[str, Any],
        *,
        model_profile_id: Optional[str] = None,
        model_title: Optional[str] = None,
        model_name: Optional[str] = None,
        reasoning_effort: Optional[ReasoningEffort] = None,
        created_by: Optional[str] = None,
    ) -> Tuple[TriageTask, bool]:
        with FileLock(str(self._lock_path)):
            for existing in self._list_unlocked():
                if existing.occurrence_key == occurrence_key:
                    return existing, False
            task_id = self._id_factory()
            now_iso = self._now_iso()
            alertname = str(alert_event.get("alertname") or "unknown")
            host = alert_event.get("host")
            task = TriageTask(
                task_id=task_id,
                source=TaskSource.AUTO,
                status=TaskStatus.QUEUED,
                created_at=now_iso,
                updated_at=now_iso,
                created_by=created_by,
                initial_message=f"alert:{alertname}",
                platform=alert_event.get("platform"),
                node=host,
                scope="node" if host else None,
                alert_key=alert_event.get("key"),
                occurrence_key=occurrence_key,
                alert_event=dict(alert_event),
                task_type="alert",
                task_type_confidence=1.0,
                task_type_source="structured",
                classifier_version="task-type-v1",
                alert_context=dict(alert_event),
                model_profile_id=model_profile_id,
                model_title=model_title,
                model_name=model_name,
                reasoning_effort=reasoning_effort,
                harness_tier=CURRENT_TIER,
            )
            self._write_task_unlocked(task)
            self._append_event_unlocked(
                task_id, "status", {"status": TaskStatus.QUEUED.value}
            )
            return task, True

    def get(self, task_id: str) -> TriageTask:
        with FileLock(str(self._lock_path)):
            return self._read_task_unlocked(task_id)

    def list(
        self,
        *,
        status: Optional[TaskStatus] = None,
        limit: int = 50,
    ) -> List[TriageTask]:
        with FileLock(str(self._lock_path)):
            tasks = self._list_unlocked()
        if status is not None:
            tasks = [task for task in tasks if task.status is status]
        tasks.sort(key=lambda item: (item.created_at, item.task_id), reverse=True)
        return tasks[:max(0, int(limit))]

    def append_event(
        self,
        task_id: str,
        event_type: str,
        payload: Dict[str, Any],
    ) -> TaskEvent:
        with FileLock(str(self._lock_path)):
            self._read_task_unlocked(task_id)
            return self._append_event_unlocked(task_id, event_type, payload)

    def events(
        self,
        task_id: str,
        *,
        after: Optional[str] = None,
    ) -> List[TaskEvent]:
        with FileLock(str(self._lock_path)):
            self._read_task_unlocked(task_id)
            parsed = self._events_unlocked(task_id)
        if after is None:
            return parsed
        for index, event in enumerate(parsed):
            if event.event_id == after:
                return parsed[index + 1:]
        return parsed

    def save_context_pack(
        self,
        task_id: str,
        pack: Dict[str, Any],
        *,
        stage: str = "initial",
    ) -> TriageTask:
        """Persist a bounded retrieval result and point the task at its snapshot."""
        if not isinstance(pack, dict):
            raise TaskStoreError("ContextPack 必须是 object")
        with FileLock(str(self._lock_path)):
            current = self._read_task_unlocked(task_id)
            previous = current.context_pack_ref or ""
            next_index = 1
            if previous.startswith("state/triage-tasks/"):
                next_index = 2
                stem = Path(previous).stem
                match = re.search(r"-(\d+)$", stem)
                if match:
                    next_index = int(match.group(1)) + 1
            filename = "context-pack.json" if next_index == 1 else f"context-pack-{next_index}.json"
            self._write_json_atomic(
                self._task_dir(task_id) / filename,
                pack,
            )
            ref = f"state/triage-tasks/{task_id}/{filename}"
            changed = TriageTask.model_validate({
                **current.model_dump(mode="json"),
                "context_pack_ref": ref,
                "context_pack_snapshot": pack,
                "updated_at": self._now_iso(),
            })
            self._write_task_unlocked(changed)
            self._append_event_unlocked(
                task_id,
                "context_pack_built",
                {
                    "ref": ref,
                    "stage": stage,
                    "retrieval_profile": pack.get("retrieval_profile"),
                    "retrieval_meta": pack.get("retrieval_meta") or {},
                },
            )
            return changed

    def load_context_pack(self, task_id: str) -> Optional[Dict[str, Any]]:
        with FileLock(str(self._lock_path)):
            task = self._read_task_unlocked(task_id)
            if not task.context_pack_ref:
                return None
            ref = Path(task.context_pack_ref)
            expected_prefix = f"state/triage-tasks/{task_id}/"
            if (
                not task.context_pack_ref.startswith(expected_prefix)
                or len(ref.parts) != 4
                or ref.name in {"", ".", ".."}
            ):
                raise TaskStoreError("ContextPack 引用不合法")
            path = self._task_dir(task_id) / ref.name
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise TaskStoreError(f"ContextPack 不可读: {task_id}") from exc
            if not isinstance(value, dict):
                raise TaskStoreError(f"ContextPack 结构损坏: {task_id}")
            return value

    def resume_with_message(
        self,
        task_id: str,
        message: str,
        *,
        task_updates: Optional[Dict[str, Any]] = None,
    ) -> TriageTask:
        with FileLock(str(self._lock_path)):
            current = self._read_task_unlocked(task_id)
            if current.source is not TaskSource.MANUAL:
                raise InvalidTransition("自动排查任务不能追加消息")
            if current.status not in {
                TaskStatus.WAITING_INPUT,
                TaskStatus.FAILED,
                TaskStatus.KB_FAILED,
                TaskStatus.COMPLETED,
            }:
                raise InvalidTransition(
                    "只有 waiting_input、completed、failed 或 kb_failed "
                    "人工任务可以通过用户消息恢复"
                )
            return self._resume_unlocked(
                current,
                message=message,
                task_updates=task_updates,
            )

    def retry_failed(self, task_id: str) -> TriageTask:
        with FileLock(str(self._lock_path)):
            current = self._read_task_unlocked(task_id)
            if current.source is not TaskSource.MANUAL:
                raise InvalidTransition("自动排查任务不能重试")
            if current.status not in {
                TaskStatus.FAILED,
                TaskStatus.KB_FAILED,
            }:
                raise InvalidTransition(
                    "只有 failed 或 kb_failed 人工任务可以重试"
                )
            return self._resume_unlocked(current)

    def set_archived(self, task_id: str, archived: bool) -> TriageTask:
        with FileLock(str(self._lock_path)):
            current = self._read_task_unlocked(task_id)
            if bool(current.archived_at) == archived:
                return current
            changed = current.model_copy(update={
                "archived_at": self._now_iso() if archived else None,
                "updated_at": self._now_iso(),
            })
            self._write_task_unlocked(changed)
            return changed

    def resolve_approval(
        self,
        task_id: str,
        approval_id: str,
        *,
        decision: str = "approve",
        feedback: Optional[str] = None,
        scope: Optional[str] = None,
        decided_by: Optional[Dict[str, str]] = None,
    ) -> TriageTask:
        if decision not in {"approve", "revise", "reject"}:
            raise InvalidTransition(f"审批结论无效: {decision}")
        # scope 与 decision 正交:命令审批用两者的组合表达四档裁决
        # (放行一次 / 本次排查放行 / 拒绝 / 永久加名单),其余审批不传 scope
        if scope is not None and scope not in {"once", "task", "always"}:
            raise InvalidTransition(f"审批范围无效: {scope}")
        with FileLock(str(self._lock_path)):
            current = self._read_task_unlocked(task_id)
            if current.status is not TaskStatus.WAITING_APPROVAL:
                raise InvalidTransition(
                    "只有 waiting_approval 任务可以提交审批结果"
                )
            pending_id = self._pending_approval_id_unlocked(task_id)
            if pending_id != approval_id:
                raise InvalidTransition("审批 ID 与当前待审批动作不匹配")
            self._append_event_unlocked(
                task_id,
                "approval_decided",
                {
                    "approval_id": approval_id,
                    "approved": decision == "approve",
                    "decision": decision,
                    "feedback": feedback or "",
                    "scope": scope,
                    # 审批人进审计;一阶段为本机用户,多用户后由登录会话给出
                    **({"decided_by": dict(decided_by)} if decided_by else {}),
                },
            )
            changed = current.model_copy(update={
                "status": TaskStatus.QUEUED,
                "updated_at": self._now_iso(),
                "lease_owner": None,
                "lease_expires_at": None,
            })
            self._write_task_unlocked(changed)
            self._append_event_unlocked(
                task_id,
                "status",
                {"status": TaskStatus.QUEUED.value},
            )
            return changed

    def claim_next(
        self,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> Optional[TriageTask]:
        with FileLock(str(self._lock_path)):
            now = self._aware_now()
            candidates = []
            for status, lease_expires_at, task_id, _poll_at in self._claim_view_unlocked():
                if status == TaskStatus.QUEUED.value or (
                    status == TaskStatus.RUNNING.value
                    and _lease_expired(lease_expires_at, now)
                ):
                    candidates.append(self._read_task_unlocked(task_id))
            if not candidates:
                return None
            task = min(candidates, key=lambda item: (item.created_at, item.task_id))
            if task.status is TaskStatus.RUNNING:
                # A worker can disappear after persisting an assistant message
                # but before every tool call in that message has started. The
                # replacement worker must close all of those calls before the
                # history is sent back to an OpenAI-compatible endpoint.
                self._close_pending_invocations_unlocked(task.task_id)
            claimed = task.model_copy(update={
                "status": TaskStatus.RUNNING,
                "execution_id": task.execution_id or self._new_execution_id(),
                "updated_at": now.isoformat(),
                "lease_owner": worker_id,
                "lease_expires_at": (
                    now + timedelta(seconds=max(1, int(lease_seconds)))
                ).isoformat(),
            })
            self._write_task_unlocked(claimed)
            self._append_event_unlocked(
                task.task_id,
                "status",
                {"status": TaskStatus.RUNNING.value},
            )
            return claimed

    def transition(
        self,
        task_id: str,
        expected: TaskStatus,
        target: TaskStatus,
        **updates,
    ) -> TriageTask:
        expected_status = TaskStatus(expected)
        target_status = TaskStatus(target)
        with FileLock(str(self._lock_path)):
            current = self._read_task_unlocked(task_id)
            if current.status is not expected_status:
                raise InvalidTransition(
                    f"任务状态不是 {expected_status.value}: {current.status.value}"
                )
            if target_status not in _ALLOWED_TRANSITIONS[current.status]:
                raise InvalidTransition(
                    f"状态迁移不允许: {current.status.value}->{target_status.value}"
                )
            if target_status in {
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.KB_FAILED,
                TaskStatus.CANCELLED,
            }:
                # An assistant message can contain a whole tool batch while
                # only its first calls have started. Close every remaining
                # invocation before exposing the terminal task state so the
                # UI and the next model replay cannot retain a spinner.
                self._close_pending_invocations_unlocked(task_id)
                updates.setdefault("pending_invocation_id", None)
            changed = current.model_copy(update={
                **updates,
                "status": target_status,
                "updated_at": self._now_iso(),
            })
            changed = TriageTask.model_validate(changed.model_dump())
            self._write_task_unlocked(changed)
            self._append_event_unlocked(
                task_id, "status", {"status": target_status.value}
            )
            return changed

    def update(
        self,
        task_id: str,
        *,
        expected: TaskStatus,
        **updates,
    ) -> TriageTask:
        expected_status = TaskStatus(expected)
        with FileLock(str(self._lock_path)):
            current = self._read_task_unlocked(task_id)
            if current.status is not expected_status:
                raise InvalidTransition(
                    f"任务状态不是 {expected_status.value}: {current.status.value}"
                )
            changed = TriageTask.model_validate({
                **current.model_dump(mode="json"),
                **updates,
                "updated_at": self._now_iso(),
            })
            self._write_task_unlocked(changed)
            return changed

    def record_tool_usage(
        self,
        task_id: str,
        tool_name: str,
        *,
        runtime_seconds: float = 0.0,
        output_bytes: int = 0,
        target_key: Optional[str] = None,
    ) -> TriageTask:
        """Atomically persist the Harness tool budget ledger.

        Unlike ``update`` this is allowed after a tool transitions a task to
        waiting_input/waiting_approval, so terminal control tools are accounted
        for as well.
        """
        with FileLock(str(self._lock_path)):
            current = self._read_task_unlocked(task_id)
            counts = dict(current.tool_call_counts)
            counts[tool_name] = int(counts.get(tool_name, 0)) + 1
            target_counts = dict(current.tool_target_counts)
            target_runtime = dict(current.tool_target_runtime_seconds)
            if target_key:
                target_counts[target_key] = int(target_counts.get(target_key, 0)) + 1
                target_runtime[target_key] = (
                    float(target_runtime.get(target_key, 0.0))
                    + max(0.0, float(runtime_seconds))
                )
            changed = TriageTask.model_validate({
                **current.model_dump(mode="json"),
                "tool_call_counts": counts,
                "tool_target_counts": target_counts,
                "tool_target_runtime_seconds": target_runtime,
                "tool_runtime_seconds": (
                    float(current.tool_runtime_seconds) + max(0.0, float(runtime_seconds))
                ),
                "tool_output_bytes": (
                    int(current.tool_output_bytes) + max(0, int(output_bytes))
                ),
                "updated_at": self._now_iso(),
            })
            self._write_task_unlocked(changed)
            return changed

    def heartbeat(
        self,
        task_id: str,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> TriageTask:
        with FileLock(str(self._lock_path)):
            task = self._read_task_unlocked(task_id)
            if task.status is not TaskStatus.RUNNING or task.lease_owner != worker_id:
                raise InvalidTransition("只有当前 worker 可续租 running 任务")
            now = self._aware_now()
            updated = task.model_copy(update={
                "updated_at": now.isoformat(),
                "lease_expires_at": (
                    now + timedelta(seconds=max(1, int(lease_seconds)))
                ).isoformat(),
            })
            self._write_task_unlocked(updated)
            return updated

    def record_invocation(
        self,
        task_id: str,
        invocation_id: str,
        status: str,
        result: Optional[Dict[str, Any]] = None,
    ) -> None:
        invocation = ToolInvocation(
            invocation_id=invocation_id,
            status=status,
            result=result,
        )
        with FileLock(str(self._lock_path)):
            self._read_task_unlocked(task_id)
            data = self._read_invocations_unlocked(task_id)
            data[invocation_id] = invocation.model_dump(mode="json")
            self._write_json_atomic(
                self._task_dir(task_id) / "invocations.json",
                data,
            )

    def invocation_completed(self, task_id: str, invocation_id: str) -> bool:
        with FileLock(str(self._lock_path)):
            data = self._read_invocations_unlocked(task_id)
        item = data.get(invocation_id)
        return isinstance(item, dict) and item.get("status") == "done"

    def invocation_result(
        self,
        task_id: str,
        invocation_id: str,
    ) -> Optional[Dict[str, Any]]:
        with FileLock(str(self._lock_path)):
            data = self._read_invocations_unlocked(task_id)
        item = data.get(invocation_id)
        if not isinstance(item, dict):
            return None
        result = item.get("result")
        return dict(result) if isinstance(result, dict) else None

    def _resume_unlocked(
        self,
        current: TriageTask,
        *,
        message: Optional[str] = None,
        task_updates: Optional[Dict[str, Any]] = None,
    ) -> TriageTask:
        task_id = current.task_id
        if current.status in {
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.KB_FAILED,
        }:
            self._close_pending_invocations_unlocked(task_id)
        if message is not None:
            self._append_event_unlocked(
                task_id,
                "user_message",
                {"message": message},
            )
        changed = TriageTask.model_validate({
            **current.model_dump(mode="json"),
            **(task_updates or {}),
            "status": TaskStatus.QUEUED.value,
            "execution_id": (
                self._new_execution_id()
                if current.status in {
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.KB_FAILED,
                }
                else current.execution_id
            ),
            "updated_at": self._now_iso(),
            "error": None,
            "failure_kind": None,
            "lease_owner": None,
            "lease_expires_at": None,
            "pending_invocation_id": None,
            "archived_at": None,
        })
        self._write_task_unlocked(changed)
        self._append_event_unlocked(
            task_id,
            "status",
            {"status": TaskStatus.QUEUED.value},
        )
        return changed

    @staticmethod
    def _new_execution_id() -> str:
        return f"exec-{secrets.token_hex(12)}"

    def _close_pending_invocations_unlocked(self, task_id: str) -> None:
        """Make interrupted tool history protocol-complete before resuming.

        ``assistant_message`` persists the whole tool-call batch before calls
        execute sequentially. A crash may therefore leave both a pending call
        and later calls that never received invocation records. Every call in
        the assistant message still needs a matching ``tool_result`` or the
        next model request is rejected with HTTP 400.
        """
        invocations = self._read_invocations_unlocked(task_id)
        events = self._events_unlocked(task_id)
        completed_event_ids = {
            str(event.payload.get("tool_call_id"))
            for event in events
            if event.type == "tool_result"
            and event.payload.get("tool_call_id")
        }
        started_calls = {
            str(event.payload.get("tool_call_id")): str(
                event.payload.get("name") or "unknown"
            )
            for event in events
            if event.type == "tool_call"
            and event.payload.get("tool_call_id")
        }
        assistant_calls: Dict[str, str] = {}
        for event in events:
            if event.type != "assistant_message":
                continue
            message = event.payload.get("message")
            if not isinstance(message, dict):
                continue
            calls = message.get("tool_calls")
            if not isinstance(calls, list):
                continue
            for call in calls:
                if not isinstance(call, dict) or not call.get("id"):
                    continue
                function = call.get("function")
                name = function.get("name") if isinstance(function, dict) else None
                assistant_calls[str(call["id"])] = str(name or "unknown")
        tool_names = {**assistant_calls, **started_calls}

        pending_ids = {
            str(invocation_id)
            for invocation_id, item in invocations.items()
            if isinstance(item, dict) and item.get("status") == "pending"
        }
        incomplete_ids = (set(assistant_calls) | pending_ids) - completed_event_ids
        changed = False
        for invocation_id in incomplete_ids:
            item = invocations.get(invocation_id)
            stored_result = item.get("result") if isinstance(item, dict) else None
            if isinstance(stored_result, dict) and item.get("status") != "pending":
                result = dict(stored_result)
            else:
                never_started = item is None and invocation_id not in started_calls
                result = {
                    "error": (
                        "上次工具调用尚未开始，恢复任务时已标记为中断"
                        if never_started else
                        "上次工具执行未完成，恢复任务时已标记为中断"
                    ),
                    "failed": True,
                    "interrupted": True,
                }
                if never_started:
                    result["skipped"] = True
                invocations[invocation_id] = ToolInvocation(
                    invocation_id=invocation_id,
                    status="failed",
                    result=result,
                ).model_dump(mode="json")
                changed = True
            self._append_event_unlocked(
                task_id,
                "tool_result",
                {
                    "tool_call_id": invocation_id,
                    "name": tool_names.get(invocation_id, "unknown"),
                    "result": self._normalize_recovery_result(
                        tool_names.get(invocation_id, "unknown"),
                        invocation_id,
                        result,
                    ),
                },
            )
        if changed:
            self._write_json_atomic(
                self._task_dir(task_id) / "invocations.json",
                invocations,
            )

    @staticmethod
    def _normalize_recovery_result(
        name: str,
        invocation_id: str,
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        # Import lazily because the store is also used by low-level migration
        # code; recovery should not make tool execution a module-level import.
        from .tool_results import normalize_tool_result

        return normalize_tool_result(
            name,
            result,
            call_id=invocation_id,
            provenance="none",
        )

    def _list_unlocked(self) -> List[TriageTask]:
        if not self.root.exists():
            return []
        tasks = []
        for path in sorted(self.root.glob("*/task.json")):
            try:
                tasks.append(TriageTask.model_validate_json(
                    path.read_text(encoding="utf-8")
                ))
            except Exception as exc:
                raise TaskStoreError(f"任务文件损坏: {path}") from exc
        return tasks

    def due_operations(self) -> List[str]:
        """到了跟踪轮询时间的 waiting_operation 任务(按轮询时间先后)。"""
        now = self._aware_now()
        with FileLock(str(self._lock_path)):
            due = [
                (poll_at or "", task_id)
                for status, _lease, task_id, poll_at in self._claim_view_unlocked()
                if status == TaskStatus.WAITING_OPERATION.value and _lease_expired(poll_at, now)
            ]
        return [task_id for _poll_at, task_id in sorted(due)]

    def _claim_view_unlocked(self) -> List[Tuple[str, Optional[str], str, Optional[str]]]:
        """claim_next 只需 (status, lease_expires_at, task_id, operation_poll_at),按 stat 缓存。

        空闲 worker 每秒领一次任务;task.json 没变就不重新解析,否则全量
        parse+校验的开销随任务数线性增长。
        """
        if not self.root.exists():
            return []
        fresh = {}
        for path in sorted(self.root.glob("*/task.json")):
            stat = path.stat()
            key = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
            cached = self._claim_cache.get(path)
            if cached is None or cached[0] != key:
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    cached = (key, (data["status"], data.get("lease_expires_at"), path.parent.name,
                                    data.get("operation_poll_at")))
                except Exception as exc:
                    raise TaskStoreError(f"任务文件损坏: {path}") from exc
            fresh[path] = cached
        self._claim_cache = fresh
        return [summary for _, summary in fresh.values()]

    def _read_task_unlocked(self, task_id: str) -> TriageTask:
        path = self._task_path(task_id)
        if not path.exists():
            raise TaskNotFound(f"任务不存在: {task_id}")
        try:
            return TriageTask.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise TaskStoreError(f"任务文件损坏: {task_id}") from exc

    def _write_task_unlocked(self, task: TriageTask) -> None:
        self._write_json_atomic(
            self._task_path(task.task_id),
            task.model_dump(mode="json"),
        )

    def _append_event_unlocked(
        self,
        task_id: str,
        event_type: str,
        payload: Dict[str, Any],
    ) -> TaskEvent:
        event = TaskEvent(
            event_id=f"ev-{secrets.token_hex(8)}",
            task_id=task_id,
            ts=self._now_iso(),
            type=event_type,
            payload=payload,
        )
        path = self._task_dir(task_id) / "messages.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(event.model_dump_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return event

    def _read_invocations_unlocked(self, task_id: str) -> Dict[str, Any]:
        self._read_task_unlocked(task_id)
        path = self._task_dir(task_id) / "invocations.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise TaskStoreError(f"invocations 文件损坏: {task_id}") from exc
        if not isinstance(data, dict):
            raise TaskStoreError(f"invocations 文件损坏: {task_id}")
        return data

    def _events_unlocked(self, task_id: str) -> List[TaskEvent]:
        path = self._task_dir(task_id) / "messages.jsonl"
        if not path.exists():
            return []
        return [
            TaskEvent.model_validate_json(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _pending_approval_id_unlocked(self, task_id: str) -> Optional[str]:
        decided = set()
        for event in reversed(self._events_unlocked(task_id)):
            approval_id = event.payload.get("approval_id")
            if not isinstance(approval_id, str) or not approval_id:
                continue
            if event.type == "approval_decided":
                decided.add(approval_id)
            elif event.type == "approval_requested" and approval_id not in decided:
                return approval_id
        return None

    @staticmethod
    def _write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
        # 不排序键:task.json 保持模型字段顺序,便于人工查看
        atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    def _task_dir(self, task_id: str) -> Path:
        return self.root / task_id

    def _task_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "task.json"

    def _aware_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    def _now_iso(self) -> str:
        return self._aware_now().isoformat()

    @staticmethod
    def _new_task_id() -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return f"tr-{stamp}-{secrets.token_hex(3)}"


def _lease_expired(value: Optional[str], now: datetime) -> bool:
    if not value:
        return True
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed <= now
