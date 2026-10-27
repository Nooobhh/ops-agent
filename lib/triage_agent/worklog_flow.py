"""任务 worklog 的生成与确认:Web 右侧入口触发增量概括,人工确认后追加。

新版 Web 人工任务:模型把上次游标之后的事件概括成 1~3 条,挂 worklog 审批;
确认即追加并推进 task.worklog_cursor,修改即带意见重生成,取消不消费。
升级前的人工任务仍走机械预览 + 一次性 worklog 闸(恢复语义不变)。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from .approvals import approval_reached_status, new_approval_id, pending_approval, request_approval
from .errors import OrchestrationError
from .models import TaskSource, TaskStatus, TriageTask

MANUAL_WORKLOG_TRIGGERS = {
    "请记录本次排查 worklog。",
    "请生成或更新本次任务的 worklog。",
}
_WORKLOG_EVENT_TYPES = {"user_message", "assistant_message", "tool_call", "tool_result"}


def _back_to(store, task_id: str, status: str) -> TriageTask:
    return store.transition(
        task_id, TaskStatus.RUNNING, TaskStatus(status),
        lease_owner=None, lease_expires_at=None,
    )


def _candidates(events) -> list:
    return [
        event for event in events
        if event.type in _WORKLOG_EVENT_TYPES
        and str(event.payload.get("message") or "").strip() not in MANUAL_WORKLOG_TRIGGERS
    ]


class WorklogFlow:
    def __init__(self, store, actions, model):
        self.store = store
        self.actions = actions
        self.model = model

    def consume_manual_trigger(self, task: TriageTask) -> Optional[TriageTask]:
        """Generate an incremental LLM worklog preview for an explicit Web action."""
        if task.source is not TaskSource.MANUAL or not task.manual_artifacts:
            return None
        events = self.store.events(task.task_id)
        latest_index = next(
            (
                index for index in range(len(events) - 1, -1, -1)
                if events[index].type == "user_message"
            ),
            None,
        )
        if latest_index is None:
            return None
        latest = events[latest_index]
        if str(latest.payload.get("message") or "").strip() not in MANUAL_WORKLOG_TRIGGERS:
            return None
        previous_status = self.status_before(events, latest_index)
        return_status = "completed" if previous_status == "completed" else "waiting_input"
        retry = self.retry_context(events, latest_index)
        from_cursor = retry.get("from_cursor") if retry else task.worklog_cursor
        worklog_status = (
            str(retry.get("worklog_status") or "WIP")
            if retry else ("DONE" if previous_status == "completed" else "WIP")
        )
        effective_return_status = (
            str(retry.get("return_status") or return_status) if retry else return_status
        )
        feedback = str(retry.get("feedback") or "") if retry else ""
        cutoff_index = latest_index
        if retry and retry.get("cutoff_event_id"):
            cutoff_index = next(
                (
                    index + 1 for index, event in enumerate(events[:latest_index])
                    if event.event_id == retry.get("cutoff_event_id")
                ),
                latest_index,
            )
        try:
            return self.request_incremental(
                task,
                from_cursor=from_cursor,
                cutoff_index=cutoff_index,
                worklog_status=worklog_status,
                return_status=effective_return_status,
                feedback=feedback,
            )
        except Exception as exc:  # noqa: BLE001
            self.record_retry(
                task,
                events=events,
                from_cursor=from_cursor,
                cutoff_index=cutoff_index,
                worklog_status=worklog_status,
                return_status=effective_return_status,
                feedback=feedback,
            )
            self.store.append_event(task.task_id, "error", {
                "message": f"worklog 预览生成失败，可重新点击重试：{exc}",
            })
            return _back_to(self.store, task.task_id, effective_return_status)

    @staticmethod
    def status_before(events, index: int) -> str:
        for event in reversed(events[:index]):
            if event.type == "status":
                return str(event.payload.get("status") or "")
        return "waiting_input"

    @staticmethod
    def retry_context(events, before_index: int) -> Optional[Dict[str, Any]]:
        """Keep the last failed preview until a replacement preview is created."""
        retry: Optional[Dict[str, Any]] = None
        for event in events[:before_index]:
            if event.type == "approval_requested" and event.payload.get("kind") == "worklog":
                retry = None
            elif event.type == "error" and isinstance(event.payload.get("worklog_retry"), dict):
                retry = dict(event.payload["worklog_retry"])
        return retry

    def record_retry(
        self,
        task: TriageTask,
        *,
        events,
        from_cursor: Optional[str],
        cutoff_index: int,
        worklog_status: str,
        return_status: str,
        feedback: str,
    ) -> None:
        candidates = _candidates(events[:cutoff_index])
        self.store.append_event(task.task_id, "error", {
            "message": "worklog 预览上下文已保留，下一次重试将继续使用本次修改意见。",
            "worklog_retry": {
                "from_cursor": from_cursor,
                "cutoff_event_id": candidates[-1].event_id if candidates else None,
                "worklog_status": worklog_status,
                "return_status": return_status,
                "feedback": feedback,
            },
        })

    def request_incremental(
        self,
        task: TriageTask,
        *,
        from_cursor: Optional[str],
        cutoff_index: int,
        worklog_status: str,
        return_status: str,
        feedback: str = "",
    ) -> TriageTask:
        events = self.store.events(task.task_id)
        start = 0
        if from_cursor:
            for index, event in enumerate(events):
                if event.event_id == from_cursor:
                    start = index + 1
                    break
        candidates = _candidates(events[start:cutoff_index])
        if not candidates:
            self.store.append_event(task.task_id, "assistant_message", {
                "message": {"role": "assistant", "content": "上次记录后没有新的工作内容，无需更新 worklog。"},
            })
            return _back_to(self.store, task.task_id, return_status)
        cutoff = candidates[-1].event_id
        entries = self.summarize_entries(task, candidates, feedback)
        source_prefix = f"auto:agent-ops:{task.task_id}:{cutoff}"
        preview = self.actions.render_worklog_entries(
            entries, status=worklog_status, source_prefix=source_prefix,
            author=task.created_by,
        )
        return request_approval(self.store, task.task_id, {
            "approval_id": new_approval_id("wl"),
            "kind": "worklog",
            "preview": preview,
            "entries": entries,
            "from_cursor": from_cursor,
            "cutoff_event_id": cutoff,
            "worklog_status": worklog_status,
            "return_status": return_status,
            "source_prefix": source_prefix,
        })

    def summarize_entries(
        self, task: TriageTask, events, feedback: str,
    ) -> List[Dict[str, Any]]:
        context = json.dumps([
            {"type": event.type, "payload": event.payload} for event in events
        ], ensure_ascii=False)
        if len(context) > 50000:
            context = context[-50000:]
        instruction = (
            "你是运维工作日志编辑。根据任务增量事件生成1到3条互不重复的日志。"
            "标题必须是自然简洁的中文成果描述，禁止复制原始长句，禁止manual:/survey:前缀。"
            "只返回严格JSON：{\"entries\":[{\"category\":\"OPS|BUGFIX|FEATURE|RESEARCH|DOC|MEETING\","
            "\"title\":\"...\",\"output\":\"...或-\",\"related\":\"...或-\","
            "\"process\":[\"步骤1\",\"步骤2\",\"步骤3\"]}]}。"
            "每条process为3到8项，事实只能来自事件，不同工作主题才拆条。"
        )
        if feedback:
            instruction += f"用户修改意见：{feedback}"
        reply = self.model.complete([{
            "role": "system", "content": instruction,
        }, {
            "role": "user", "content": json.dumps({
                "task": task.initial_message, "events": context,
            }, ensure_ascii=False),
        }], [])
        text = (reply.content or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
        try:
            value = json.loads(text)
            entries = value.get("entries") if isinstance(value, dict) else None
        except (TypeError, ValueError) as exc:
            raise OrchestrationError("worklog 概括模型未返回合法 JSON，可重试") from exc
        if not isinstance(entries, list) or not 1 <= len(entries) <= 3:
            raise OrchestrationError("worklog 概括必须生成 1 到 3 条日志")
        if any(not isinstance(item, dict) or not str(item.get("title") or "").strip() for item in entries):
            raise OrchestrationError("worklog 概括缺少有效标题")
        return entries

    def request_gate(self, task: TriageTask) -> Optional[TriageTask]:
        """升级前人工任务:挂 worklog 落档确认;session 未收口(无条目可写)返回 None 直接完成。"""
        try:
            preview = self.actions.worklog_preview(
                task.run_id, kb_doc=task.kb_doc, author=task.created_by,
            )
        except Exception:  # noqa: BLE001
            # worklog 是收口附属品,预览失败不该拦住任务完成(与静默 warn 同旨)
            return None
        if not preview:
            return None
        return request_approval(self.store, task.task_id, {
            "approval_id": new_approval_id("wl"),
            "kind": "worklog",
            "run_id": task.run_id,
            "preview": preview,
        })

    def consume_decision(self, task: TriageTask) -> Optional[TriageTask]:
        """兑现增量 worklog 预览：确认追加、修改重生成或取消。"""
        pending = pending_approval(
            self.store, task.task_id, kind="worklog", require_call=False,
        )
        if pending is None:
            return None
        request, decision, feedback, _scope = pending
        # 升级前人工任务仍使用机械预览和一次性 worklog 闸，保持其恢复语义。
        if "entries" not in request:
            if approval_reached_status(
                self.store,
                task.task_id,
                str(request.get("approval_id") or ""),
                TaskStatus.COMPLETED.value,
            ):
                return None
            if decision == "approve":
                try:
                    self.actions.worklog_commit(
                        task.run_id, kb_doc=task.kb_doc, author=task.created_by,
                    )
                except Exception:  # noqa: BLE001
                    pass
            return _back_to(self.store, task.task_id, TaskStatus.COMPLETED.value)
        approval_id = str(request.get("approval_id") or "")
        if any(
            event.type == "assistant_message"
            and event.payload.get("worklog_approval_id") == approval_id
            for event in self.store.events(task.task_id)
        ):
            return None
        return_status = str(request.get("return_status") or "waiting_input")
        worklog_status = str(request.get("worklog_status") or "WIP")
        if decision == "revise":
            events = self.store.events(task.task_id)
            cutoff_id = str(request.get("cutoff_event_id") or "")
            cutoff_index = next(
                (index + 1 for index, event in enumerate(events) if event.event_id == cutoff_id),
                len(events),
            )
            try:
                return self.request_incremental(
                    task,
                    from_cursor=request.get("from_cursor"),
                    cutoff_index=cutoff_index,
                    worklog_status=worklog_status,
                    return_status=return_status,
                    feedback=feedback,
                )
            except Exception as exc:  # noqa: BLE001
                self.record_retry(
                    task,
                    events=events,
                    from_cursor=request.get("from_cursor"),
                    cutoff_index=cutoff_index,
                    worklog_status=worklog_status,
                    return_status=return_status,
                    feedback=feedback,
                )
                self.store.append_event(task.task_id, "error", {
                    "message": f"worklog 重新生成失败，可再次点击更新：{exc}",
                })
                return _back_to(self.store, task.task_id, return_status)
        if decision == "approve":
            try:
                paths = self.actions.commit_worklog_entries(
                    list(request.get("entries") or []),
                    status=worklog_status,
                    source_prefix=str(
                        request.get("source_prefix")
                        or f"auto:agent-ops:{task.task_id}"
                    ),
                    author=task.created_by,
                )
            except Exception as exc:  # noqa: BLE001
                self.store.append_event(task.task_id, "error", {
                    "message": f"worklog 写入失败，未消费本次内容，可重试：{type(exc).__name__}: {exc}",
                })
                return _back_to(self.store, task.task_id, return_status)
            entry_count = len(request.get("entries") or [])
            task = self.store.update(
                task.task_id,
                expected=TaskStatus.RUNNING,
                worklog_cursor=str(request.get("cutoff_event_id") or "") or None,
                worklog_entries=task.worklog_entries + entry_count,
            )
            message = f"已追加 {entry_count} 条 worklog"
            if paths:
                message += "：" + ", ".join(paths)
        else:
            message = "已取消本次 worklog 写入。"
        self.store.append_event(task.task_id, "assistant_message", {
            "message": {"role": "assistant", "content": message},
            "artifact": "worklog",
            "worklog_approval_id": approval_id,
        })
        return _back_to(self.store, task.task_id, return_status)
