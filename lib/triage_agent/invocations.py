"""工具调用的落档与闭合:每个 tool_call_id 最终都要有一条 tool_result。

invocation 记录(pending/done/failed)保证已完成的调用重启后不重放;tool_result
事件统一写 ToolResult envelope。新旧运行时共用,工具元数据取自 ToolRegistry。
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

from lib.sanitize import redact_secrets

from .model_client import ToolCall
from .models import TriageTask
from .tool_registry import ToolAdmissionError
from .tool_results import elapsed_ms, normalize_tool_result, result_size


class ToolInvocations:
    def __init__(self, store, tool_registry):
        self.store = store
        self.tool_registry = tool_registry

    def skip(
        self,
        task_id: str,
        call: ToolCall,
        reason: str,
        *,
        batch_id: Optional[str] = None,
        batch_index: Optional[int] = None,
    ) -> None:
        # 每个 tool_call_id 必须有 tool_result，否则 retry 回放的消息序列会被 API 拒收
        result = normalize_tool_result(
            call.name,
            {"skipped": True, "reason": reason},
            call_id=call.call_id,
            target_kind="none",
            provenance="none",
        )
        payload = {
            "tool_call_id": call.call_id,
            "name": call.name,
            "result": result,
        }
        if batch_id:
            payload.update({
                "batch_id": batch_id,
                "batch_index": batch_index,
                "skip_reason": reason,
            })
        self.store.append_event(task_id, "tool_result", payload)

    def record_usage(
        self,
        task_id: str,
        name: str,
        started: float,
        result: Optional[Dict[str, Any]] = None,
    ) -> TriageTask:
        latest = self.store.get(task_id)
        raw_target = (result or {}).get("target") if isinstance(result, dict) else None
        node = (
            (result or {}).get("node")
            if isinstance(result, dict) else None
        ) or (
            raw_target.get("node") if isinstance(raw_target, dict) else None
        ) or latest.node
        platform = (
            (result or {}).get("platform")
            if isinstance(result, dict) else None
        ) or (
            raw_target.get("platform") if isinstance(raw_target, dict) else None
        ) or latest.platform
        return self.store.record_tool_usage(
            task_id,
            name,
            runtime_seconds=(time.monotonic() - started),
            output_bytes=result_size(result or {}),
            target_key=(f"{name}:{platform}:{node}" if node else None),
        )

    def record_success(
        self,
        task_id: str,
        call_id: str,
        name: str,
        result: Dict[str, Any],
        started: float,
        batch_fields: Dict[str, Any],
        *,
        context: Dict[str, Any],
        metadata=None,
        model_result: Optional[Dict[str, Any]] = None,
    ) -> None:
        """model_result 给定时替换默认投影(新运行时按档位控制投影长度)。"""
        if metadata is None:
            metadata = self.tool_registry.metadata(name)
        self.record_usage(task_id, name, started, result)
        self.store.record_invocation(task_id, call_id, "done", result)
        event_result = normalize_tool_result(
            name,
            result,
            call_id=call_id,
            version=metadata.version,
            target_kind=metadata.target_kind,
            evidence_eligible=metadata.evidence_policy == "runtime_output",
            provenance=(
                "runtime_tool"
                if metadata.evidence_policy == "runtime_output"
                else "none"
            ),
            duration_ms=elapsed_ms(started),
            context=context,
        )
        if model_result is not None:
            event_result["model_result"] = model_result
        self.store.append_event(
            task_id,
            "tool_result",
            {
                "tool_call_id": call_id,
                "name": name,
                "result": event_result,
                **batch_fields,
            },
        )

    def record_failure(
        self,
        task_id: str,
        invocation_id: str,
        name: str,
        error: Exception,
        *,
        batch_id: Optional[str] = None,
        batch_index: Optional[int] = None,
        duration_ms: Optional[int] = None,
    ) -> None:
        structured = getattr(error, "result", None)
        if isinstance(structured, dict):
            failed_result = dict(structured)
            failed_result["failed"] = True
        else:
            failed_result = {
                "error": redact_secrets(str(error)),
                "failed": True,
            }
        try:
            metadata = self.tool_registry.metadata(name)
        except ToolAdmissionError:
            metadata = None
        self.store.record_invocation(
            task_id,
            invocation_id,
            "failed",
            failed_result,
        )
        event_payload = {
            "tool_call_id": invocation_id,
            "name": name,
            "result": normalize_tool_result(
                name,
                failed_result,
                call_id=invocation_id,
                target_kind=metadata.target_kind if metadata is not None else "none",
                provenance="none",
                failure=error,
                duration_ms=duration_ms,
            ),
        }
        if batch_id:
            event_payload.update({
                "batch_id": batch_id,
                "batch_index": batch_index,
            })
        self.store.append_event(task_id, "tool_result", event_payload)

    def settle(
        self,
        task_id: str,
        call_id: str,
        name: str,
        result: Dict[str, Any],
    ) -> None:
        """审批兑现后补写挂起调用的 tool_result。"""
        self.store.record_invocation(task_id, call_id, "done", result)
        try:
            metadata = self.tool_registry.metadata(name)
            target_kind = metadata.target_kind
            evidence = metadata.evidence_policy == "runtime_output"
        except ToolAdmissionError:
            target_kind = "none"
            evidence = False
        self.store.append_event(
            task_id,
            "tool_result",
            {
                "tool_call_id": call_id,
                "name": name,
                "result": normalize_tool_result(
                    name,
                    result,
                    call_id=call_id,
                    target_kind=target_kind,
                    evidence_eligible=evidence,
                    provenance="runtime_tool" if evidence else "none",
                ),
            },
        )
