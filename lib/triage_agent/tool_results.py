"""Versioned, provider-independent results for model tools.

The first implementation of the Agent loop used a different, flat result shape
for every action.  This module is the compatibility boundary: the canonical
``ToolResult`` is strict and typed, while ``normalize_tool_result`` keeps the
old fields at the top level during the migration so old prompts and records
remain readable.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Mapping, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

from lib.sanitize import redact_secrets


_SECRET_FIELD = {
    "password", "passwd", "pwd", "secret", "token", "authorization",
    "api_key", "apikey", "private_key", "private_key_path", "totp_secret",
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolRef(_StrictModel):
    name: str
    version: str = "1.0"


class ToolTarget(_StrictModel):
    kind: str = "none"
    platform: Optional[str] = None
    node: Optional[str] = None
    scope: Optional[str] = None


class ToolConnection(_StrictModel):
    status: str
    route: str
    channel: str
    reused: bool
    attempts: int = 0
    credential_status: str = "unknown"


class ToolEvidence(_StrictModel):
    eligible: bool = False
    refs: List[str] = Field(default_factory=list)
    provenance: str = "none"


class ToolMetrics(_StrictModel):
    duration_ms: int = 0
    output_bytes: int = 0
    attempts: int = 1


class ToolTiming(_StrictModel):
    admission_ms: int = 0
    dependency_resolution_ms: int = 0
    dispatch_ms: int = 0
    remote_exec_ms: int = 0
    session_write_ms: int = 0
    report_write_ms: int = 0
    task_store_ms: int = 0
    event_write_ms: int = 0
    total_ms: int = 0


class ToolError(_StrictModel):
    code: str
    phase: str
    retryable: bool = False
    disposition: str
    message: str
    details: Dict[str, Any] = Field(default_factory=dict)


class ToolResult(_StrictModel):
    # During the migration the event projection carries legacy fields such as
    # stdout/exit_code and string error.  The nested ``tool_result`` remains
    # strict; this outer adapter accepts those fields for old consumers.
    model_config = ConfigDict(extra="allow")
    schema_version: int = 1
    tool: ToolRef
    call_id: Optional[str] = None
    ok: bool
    outcome: str
    # Execution state is separate from command exit status. A command can
    # execute and return exit_code=1, while an admission rejection never runs.
    execution_state: str = "unknown"
    command_status: str = "not_applicable"
    skip: Optional[Dict[str, Any]] = None
    dependency: Dict[str, Any] = Field(default_factory=dict)
    timing: ToolTiming = Field(default_factory=ToolTiming)
    target: ToolTarget
    data: Dict[str, Any] = Field(default_factory=dict)
    connection: Optional[ToolConnection] = None
    evidence: ToolEvidence = Field(default_factory=ToolEvidence)
    metrics: ToolMetrics = Field(default_factory=ToolMetrics)
    warnings: List[str] = Field(default_factory=list)
    error: Optional[Union[ToolError, str]] = None


def _target(
    raw: Mapping[str, Any],
    *,
    target_kind: str = "none",
    context: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    value = raw.get("target")
    if isinstance(value, Mapping):
        result = dict(value)
        result.setdefault("kind", target_kind)
    else:
        result = {"kind": target_kind}
    context = context or {}
    for key in ("platform", "node", "scope"):
        if result.get(key) is None and context.get(key) is not None:
            result[key] = context[key]
    if result.get("kind") == "none" and result.get("node") and target_kind in {
        "task_node", "registered_node", "cluster_nodes",
    }:
        result["kind"] = "registered_node"
    return result


def _redact_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Remove credential-like values before they reach model history."""
    if depth > 8:
        return "[truncated]"
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, Mapping):
        result: Dict[str, Any] = {}
        for item_key, item_value in value.items():
            normalized = str(item_key).lower().replace("-", "_")
            if normalized in _SECRET_FIELD or normalized.endswith("_token"):
                result[str(item_key)] = "***REDACTED***"
            else:
                result[str(item_key)] = _redact_value(
                    item_value, key=normalized, depth=depth + 1,
                )
        return result
    if isinstance(value, list):
        return [_redact_value(item, depth=depth + 1) for item in value[:200]]
    if isinstance(value, tuple):
        return [_redact_value(item, depth=depth + 1) for item in value[:200]]
    return value


def _connection(raw: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    value = raw.get("connection")
    if not isinstance(value, Mapping):
        return None
    result = dict(value)
    result.setdefault("status", "unknown")
    result.setdefault("route", "unknown")
    result.setdefault("channel", "exec")
    result.setdefault("reused", result.get("status") == "reused")
    return result


def _output_bytes(raw: Mapping[str, Any]) -> int:
    execution = raw.get("execution")
    values = execution if isinstance(execution, Mapping) else raw
    return sum(
        len(str(values.get(key) or "").encode("utf-8"))
        for key in ("stdout", "stderr")
    )


def _timing(
    raw: Mapping[str, Any],
    *,
    duration_ms: int,
    error: Optional[ToolError],
) -> Dict[str, int]:
    """Return phase timings without attributing LLM transport to a tool."""
    source = raw.get("timing")
    if isinstance(source, Mapping):
        values = source
    else:
        values = {}
    execution = raw.get("execution")
    remote_ms = 0
    if isinstance(execution, Mapping):
        try:
            remote_ms = max(0, int(float(execution.get("duration") or 0) * 1000))
        except (TypeError, ValueError):
            remote_ms = 0
    phase = error.phase if error is not None else str(
        (raw.get("error") or {}).get("phase")
        if isinstance(raw.get("error"), Mapping) else ""
    )
    result: Dict[str, int] = {}
    for key in (
        "admission_ms", "dependency_resolution_ms", "dispatch_ms",
        "remote_exec_ms", "session_write_ms", "report_write_ms",
        "task_store_ms", "event_write_ms", "total_ms",
    ):
        value = values.get(key)
        try:
            result[key] = max(0, int(value)) if value is not None else 0
        except (TypeError, ValueError):
            result[key] = 0
    if not isinstance(source, Mapping):
        result["total_ms"] = max(0, int(duration_ms))
        result["remote_exec_ms"] = min(result["total_ms"], remote_ms)
        result["admission_ms"] = result["total_ms"] if phase in {"admission", "validation"} else 0
        result["dispatch_ms"] = max(
            0, result["total_ms"] - result["admission_ms"] - result["remote_exec_ms"]
        )
    else:
        result["total_ms"] = max(result["total_ms"], int(duration_ms))
    return result


def _outcome(raw: Mapping[str, Any]) -> tuple[bool, str]:
    explicit = str(raw.get("outcome") or "")
    if explicit:
        return bool(raw.get("ok", explicit in {"completed", "replayed"})), explicit
    if raw.get("skipped") is True:
        return False, "skipped"
    if raw.get("pending_approval") is True:
        return False, "pending_approval"
    if raw.get("needs_input") is True:
        return False, "needs_input"
    if raw.get("failed") is True or raw.get("error"):
        return False, "failed"
    execution = raw.get("execution")
    if isinstance(execution, Mapping) and (
        execution.get("timed_out") is True or execution.get("truncated") is True
    ):
        return True, "completed_partial"
    if raw.get("timed_out") is True or raw.get("truncated") is True:
        return True, "completed_partial"
    return True, "completed"


def _canonical(
    name: str,
    raw: Mapping[str, Any],
    *,
    call_id: Optional[str],
    version: str,
    target_kind: str,
    evidence_eligible: bool,
    evidence_refs: Optional[List[str]],
    provenance: str,
    duration_ms: Optional[int],
    error: Optional[ToolError] = None,
    context: Optional[Mapping[str, Any]] = None,
) -> ToolResult:
    ok, outcome = _outcome(raw)
    raw_error = raw.get("error")
    if error is None and raw_error:
        if isinstance(raw_error, Mapping):
            error = ToolError(
                code=str(raw_error.get("code") or raw_error.get("kind") or "TOOL_FAILED"),
                phase=str(raw_error.get("phase") or "execution"),
                retryable=bool(raw_error.get("retryable", False)),
                disposition=str(raw_error.get("disposition") or "task_fail"),
                message=str(raw_error.get("message") or "Tool execution failed"),
                details={
                    key: value for key, value in raw_error.items()
                    if key not in {"code", "kind", "phase", "retryable", "disposition", "message"}
                },
            )
        else:
            error = ToolError(
                code="TOOL_FAILED",
                phase="execution",
                retryable=False,
                disposition="task_fail",
                message=str(raw_error),
            )
    if error is not None:
        ok = False
        outcome = error.details.get("outcome", "failed")
    execution = raw.get("execution")
    has_execution = isinstance(execution, Mapping)
    if isinstance(raw.get("execution_state"), str):
        execution_state = str(raw["execution_state"])
    elif has_execution:
        execution_state = "executed"
    elif outcome in {"skipped", "denied", "rejected", "pending_approval", "needs_input"}:
        execution_state = "not_executed"
    else:
        execution_state = "unknown"
    if isinstance(raw.get("command_status"), str):
        command_status = str(raw["command_status"])
    elif has_execution:
        if execution.get("timed_out") is True:
            command_status = "timeout"
        elif execution.get("truncated") is True:
            command_status = "truncated"
        elif execution.get("exit_code") is None:
            command_status = "unknown"
        elif execution.get("exit_code") == 0:
            command_status = "zero"
        else:
            command_status = "nonzero"
    else:
        command_status = "not_applicable"
    skip_value = raw.get("skip")
    if not isinstance(skip_value, Mapping) and raw.get("skipped") is True:
        skip_value = {"reason": str(raw.get("reason") or "unspecified")}
    dependency_value = raw.get("dependency")
    if not isinstance(dependency_value, Mapping):
        dependency_value = {
            "requires_success_of": [],
            "satisfied": outcome not in {"skipped", "pending_approval", "needs_input"},
        }
    duration = duration_ms if duration_ms is not None else int(
        float(raw.get("duration") or raw.get("duration_seconds") or 0) * 1000
    )
    timing = _timing(raw, duration_ms=duration, error=error)
    return ToolResult(
        tool=ToolRef(name=name, version=version),
        call_id=call_id,
        ok=ok,
        outcome=outcome,
        execution_state=execution_state,
        command_status=command_status,
        skip=dict(skip_value) if isinstance(skip_value, Mapping) else None,
        dependency=dict(dependency_value),
        timing=ToolTiming.model_validate(timing),
        target=ToolTarget.model_validate(_target(raw, target_kind=target_kind, context=context)),
        data=dict(raw),
        connection=(
            ToolConnection.model_validate(_connection(raw))
            if _connection(raw) is not None else None
        ),
        evidence=ToolEvidence(
            eligible=bool(evidence_eligible and ok),
            refs=(
                list(evidence_refs)
                if evidence_refs is not None
                else list(raw.get("evidence_refs") or [])
                if isinstance(raw.get("evidence_refs"), list)
                else []
            ),
            provenance=provenance if evidence_eligible and ok else "none",
        ),
        metrics=ToolMetrics(
            duration_ms=max(0, duration),
            output_bytes=_output_bytes(raw),
        ),
        warnings=list(raw.get("warnings") or []) if isinstance(raw.get("warnings"), list) else [],
        error=error,
    )


def normalize_tool_result(
    name: str,
    result: Optional[Mapping[str, Any]],
    *,
    call_id: Optional[str] = None,
    version: str = "1.0",
    target_kind: str = "none",
    evidence_eligible: bool = False,
    evidence_refs: Optional[List[str]] = None,
    provenance: str = "runtime_tool",
    duration_ms: Optional[int] = None,
    failure: Optional[Exception] = None,
    context: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Return a canonical envelope plus legacy top-level fields.

    ``tool_result`` is the exact strict envelope.  The top-level compatibility
    projection intentionally retains fields such as ``stdout`` and a string
    ``error`` until the frontend and all persisted records have migrated.
    """
    raw = _redact_value(dict(result or {}))
    canonical = _canonical(
        name,
        raw,
        call_id=call_id,
        version=version,
        target_kind=target_kind,
        evidence_eligible=evidence_eligible,
        evidence_refs=evidence_refs,
        provenance=provenance,
        duration_ms=duration_ms,
        error=_exception_error(failure) if failure is not None else None,
        context=context,
    )
    output = dict(raw)
    output.update({
        "schema_version": 1,
        "tool": canonical.tool.model_dump(mode="json"),
        "call_id": call_id,
        "ok": canonical.ok,
        "outcome": canonical.outcome,
        "execution_state": canonical.execution_state,
        "command_status": canonical.command_status,
        "skip": canonical.skip,
        "dependency": canonical.dependency,
        "timing": canonical.timing.model_dump(mode="json"),
        "target": canonical.target.model_dump(mode="json"),
        "data": canonical.data,
        "connection": canonical.connection.model_dump(mode="json") if canonical.connection else None,
        "evidence": canonical.evidence.model_dump(mode="json"),
        "metrics": canonical.metrics.model_dump(mode="json"),
        "warnings": canonical.warnings,
        "tool_result": canonical.model_dump(mode="json"),
    })
    if canonical.error is not None:
        # Existing UI/records expect error to be a display string.  Keep that
        # projection while exposing the typed error as error_detail.
        output["error_detail"] = canonical.error.model_dump(mode="json")
    output["model_result"] = model_tool_result(output)
    return output


def model_tool_result(
    result: Optional[Mapping[str, Any]],
    *,
    max_preview_chars: int = 3000,
    max_content_chars: int = 12000,
) -> Dict[str, Any]:
    """Build the bounded ToolResult projection sent back through model history.

    TaskStore events retain the complete normalized result and legacy fields.
    This projection deliberately removes the full ``data`` payload and keeps
    only bounded content needed for the next reasoning step.  Exact evidence
    remains available through task-scoped evidence refs.

    Document/evidence reads keep their bodies, command-like results keep
    stdout/stderr previews, and every other tool (KB candidates, task
    dependencies, routes, snapshots, risks, playbooks, hypothesis phases)
    keeps a size-bounded copy of its payload: an empty ``data`` leaves the
    model blind to the result it just asked for.
    """
    if not isinstance(result, Mapping):
        return {"ok": False, "outcome": "failed", "data": {}}
    canonical = result.get("tool_result")
    if not isinstance(canonical, Mapping):
        canonical = result
    tool = canonical.get("tool") or result.get("tool") or {}
    name = str(tool.get("name") or result.get("operation") or "") if isinstance(tool, Mapping) else str(tool)
    data = canonical.get("data") if isinstance(canonical.get("data"), Mapping) else {}
    compact_data: Dict[str, Any] = {}
    if name in {"read_kb_document", "read_kb_chunks", "read_task_evidence"}:
        for key in ("doc", "chunks", "evidence", "kind", "provenance", "reference_only", "truncated"):
            if key in data:
                compact_data[key] = _bounded_model_value(data[key], max_content_chars)
        # The legacy result may carry a document body at the top level.
        if "content" in result:
            compact_data["content"] = _bounded_model_value(
                result.get("content"), max_content_chars,
            )
    elif not _is_command_like(name, data):
        compact_data = _bounded_model_payload(data, max_preview_chars, max_content_chars)
    else:
        execution = data.get("execution") if isinstance(data.get("execution"), Mapping) else {}
        for key in (
            "diagnostic_id", "command", "node", "exit_code", "timed_out", "truncated",
        ):
            if key in data:
                compact_data[key] = (
                    _short_model_text(data[key], max_preview_chars)
                    if key == "command" else data[key]
                )
            elif key in result:
                compact_data[key] = (
                    _short_model_text(result[key], max_preview_chars)
                    if key == "command" else result[key]
                )
        for key in ("stdout", "stderr"):
            value = execution.get(key) if key in execution else data.get(key, result.get(key))
            if value not in (None, ""):
                compact_data[key] = _short_model_text(value, max_preview_chars)
        if execution:
            for key in ("exit_code", "timed_out", "truncated"):
                if key in execution:
                    compact_data[key] = execution[key]

    projection = {
        "schema_version": canonical.get("schema_version", 1),
        "tool": tool,
        "call_id": canonical.get("call_id") or result.get("call_id"),
        "ok": bool(canonical.get("ok", result.get("ok", False))),
        "outcome": canonical.get("outcome", result.get("outcome", "failed")),
        "execution_state": canonical.get("execution_state", result.get("execution_state", "unknown")),
        "command_status": canonical.get("command_status", result.get("command_status", "not_applicable")),
        "dependency": canonical.get("dependency", result.get("dependency", {})),
        "timing": canonical.get("timing", result.get("timing", {})),
        "target": canonical.get("target", result.get("target", {"kind": "none"})),
        "data": compact_data,
        "evidence": canonical.get("evidence", result.get("evidence", {})),
        "metrics": {
            key: value
            for key, value in (canonical.get("metrics") or result.get("metrics") or {}).items()
            if key in {"duration_ms", "output_bytes", "attempts"}
        },
        "warnings": list(canonical.get("warnings") or result.get("warnings") or [])[:8],
    }
    error = canonical.get("error") or result.get("error_detail")
    if error:
        projection["error"] = _bounded_model_value(error, max_preview_chars)
    return projection


def _short_model_text(value: Any, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _bounded_model_value(value: Any, limit: int, items: int = 64) -> Any:
    if isinstance(value, str):
        return _short_model_text(value, limit)
    if isinstance(value, Mapping):
        # 键数只按固定上限截:收紧时也不能把 tasks/candidates 这类主字段整个丢掉
        return {str(key): _bounded_model_value(item, limit, items) for key, item in list(value.items())[:64]}
    if isinstance(value, list):
        return [_bounded_model_value(item, limit, items) for item in value[:items]]
    return value


_COMMAND_TOOLS = {"run_command", "run_diagnostic"}
# 只在审计里有用、对下一步推理没有信息量的字段
_MODEL_PAYLOAD_SKIP_KEYS = {"schema_version", "timing", "connection"}
# 先按常规上限截;整体仍超长时逐级收紧单值与列表长度
_MODEL_PAYLOAD_STEPS = ((None, 64), (600, 24), (200, 12), (120, 6))


def _is_command_like(name: str, data: Mapping[str, Any]) -> bool:
    return (
        name in _COMMAND_TOOLS
        or isinstance(data.get("execution"), Mapping)
        or "stdout" in data
    )


def _bounded_model_payload(data: Mapping[str, Any], preview_chars: int, total_chars: int) -> Dict[str, Any]:
    payload = {key: value for key, value in data.items() if key not in _MODEL_PAYLOAD_SKIP_KEYS}
    bounded: Dict[str, Any] = {}
    for string_limit, items in _MODEL_PAYLOAD_STEPS:
        bounded = _bounded_model_value(payload, string_limit or preview_chars, items)
        if len(json.dumps(bounded, ensure_ascii=False, default=str)) <= total_chars:
            if string_limit is not None:
                bounded["truncated"] = True
            return bounded
    text = json.dumps(bounded, ensure_ascii=False, default=str)
    return {"preview": _short_model_text(text, total_chars), "truncated": True}


def _exception_error(error: Exception) -> ToolError:
    error_codes = {
        "ValidationError": "INVALID_ARGUMENTS",
        "ValueError": "INVALID_ARGUMENTS",
        "ActionDenied": "ACTION_DENIED",
        "DiagnosticDenied": "DIAGNOSTIC_DENIED",
        "AmendmentRejected": "EVIDENCE_INVALID",
        "OrchestrationError": "ORCHESTRATION_ERROR",
        "RemoteExecutionError": "REMOTE_EXECUTION_FAILED",
    }
    disposition = getattr(error, "disposition", None)
    if disposition is None:
        disposition = (
            "agent_retry"
            if type(error).__name__ in {
                "ValidationError", "ValueError", "ActionDenied",
                "DiagnosticDenied", "AmendmentRejected", "OrchestrationError",
            }
            else "task_fail"
        )
    retryable = bool(getattr(error, "retryable", disposition == "harness_retry"))
    code = getattr(error, "code", None) or error_codes.get(
        type(error).__name__, type(error).__name__.upper()
    )
    phase = getattr(error, "phase", None) or "execution"
    return ToolError(
        code=str(code),
        phase=str(phase),
        retryable=retryable,
        disposition=str(disposition),
        message=redact_secrets(str(error)),
        details={"outcome": "denied" if disposition == "agent_retry" else "failed"},
    )


def error_tool_result(
    name: str,
    error: Exception,
    *,
    call_id: Optional[str] = None,
    target_kind: str = "none",
    context: Optional[Mapping[str, Any]] = None,
    code: Optional[str] = None,
    phase: str = "execution",
    retryable: bool = False,
    disposition: str = "task_fail",
    message: Optional[str] = None,
    outcome: str = "failed",
) -> Dict[str, Any]:
    """Build a safe structured failure without serializing exception details."""
    safe_message = redact_secrets(message or str(error))
    detail = _exception_error(error)
    detail = detail.model_copy(update={
        "code": code or detail.code,
        "phase": phase,
        "retryable": retryable,
        "disposition": disposition,
        "message": safe_message,
        "details": {"outcome": outcome},
    })
    raw = {"failed": True, "error": safe_message}
    canonical = _canonical(
        name,
        raw,
        call_id=call_id,
        version="1.0",
        target_kind=target_kind,
        evidence_eligible=False,
        evidence_refs=None,
        provenance="none",
        duration_ms=0,
        error=detail,
        context=context,
    )
    output = dict(raw)
    output.update({
        "schema_version": 1,
        "tool": canonical.tool.model_dump(mode="json"),
        "call_id": call_id,
        "ok": False,
        "outcome": outcome,
        "execution_state": canonical.execution_state,
        "command_status": canonical.command_status,
        "skip": canonical.skip,
        "dependency": canonical.dependency,
        "timing": canonical.timing.model_dump(mode="json"),
        "target": canonical.target.model_dump(mode="json"),
        "data": raw,
        "connection": None,
        "evidence": canonical.evidence.model_dump(mode="json"),
        "metrics": canonical.metrics.model_dump(mode="json"),
        "warnings": [],
        "error_detail": detail.model_dump(mode="json"),
        "tool_result": canonical.model_dump(mode="json"),
    })
    return output


def result_size(result: Mapping[str, Any]) -> int:
    """Bounded accounting size used by the per-task budget ledger."""
    try:
        return len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        return len(str(result).encode("utf-8"))


def elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))
