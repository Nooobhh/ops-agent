"""知识草稿与任务产物两个工具的参数契约与模型可见定义(新运行时沿用)。

A3.7 删除旧编排器时,其余结构档工具(run_command、finalize_report、假设与转交等)一并删除;
新运行时自己的工具清单见 lib/agent_runtime/tools.py。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, List, Literal, Optional, Type

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lib.envpack.fields import KnowledgePlatform
from lib.kb.frontmatter import SERVICES

from .tool_contracts import (
    ToolMetadata,
    render_model_tool_description,
)


class ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KnowledgeProposal(BaseModel):
    """知识草稿契约:schema 直接进工具定义,模型可见全部字段约束。"""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["new", "append_case"]
    title: Optional[str] = Field(default=None, min_length=1, max_length=160)
    platform: Optional[KnowledgePlatform] = None
    service: Optional[str] = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9-]*$",
        json_schema_extra={"enum": sorted(SERVICES)},
    )
    problem: Optional[str] = Field(
        default=None, pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$",
    )
    symptoms: List[str] = Field(default_factory=list)
    alerts: List[str] = Field(default_factory=list)
    summary: Optional[str] = Field(default=None, min_length=1, max_length=500)
    mechanism: Optional[str] = None
    # 人工审核可以在证据不足时同意落档;普通落档仍会在 sink 中给出证据问题。
    evidence: List[str] = Field(default_factory=list)
    remediation: List[str] = Field(default_factory=list)
    playbooks: List[Dict[str, Any]] = Field(default_factory=list)
    diagnostic_paths: List[Dict[str, Any]] = Field(default_factory=list)
    doc: Optional[str] = None

    @field_validator("service")
    @classmethod
    def _service_in_vocabulary(cls, value: Optional[str]) -> Optional[str]:
        # 与 kb-lint 同一封闭集:在此驳回可让模型自纠,漏到 lint 就是硬失败
        if value is not None and value not in SERVICES:
            raise ValueError(
                f"{value!r} 不在 service 封闭集,须取: {', '.join(sorted(SERVICES))}"
            )
        return value

    @model_validator(mode="after")
    def _mode_contract(self) -> "KnowledgeProposal":
        if self.mode == "append_case":
            if not self.doc:
                raise ValueError("append_case 必须提供 doc")
            return self
        required = {
            "title": self.title, "platform": self.platform,
            "service": self.service, "problem": self.problem,
            "mechanism": self.mechanism,
        }
        missing = [name for name, value in required.items()
                   if value is None or not str(value).strip()]
        if missing:
            raise ValueError(f"new 模式缺少字段: {', '.join(missing)}")
        if not self.symptoms:
            raise ValueError("new 模式至少需要一个 symptom")
        if self.doc is not None:
            raise ValueError("new 模式不得提供 doc")
        return self


class KbDraftArgs(ToolArguments):
    proposal: KnowledgeProposal


class CreateArtifactArgs(ToolArguments):
    filename: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*\.(?:md|sh|py)$",
    )
    content: str = Field(min_length=1, max_length=524288)
    purpose: str = Field(min_length=1, max_length=500)


_FLAT_NESTED_ARGUMENTS = {
    "create_kb_draft": ("proposal", frozenset(KnowledgeProposal.model_fields)),
}


def normalize_tool_arguments(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a safe flat nested-object payload before strict validation.

    Some OpenAI-compatible models flatten a single nested object even when the
    provider schema contains the wrapper field.  Only the known wrapper
    contract is adapted, and only when every supplied key belongs to the
    nested model.  Unknown keys still reach the strict validator and fail.
    """
    adapter = _FLAT_NESTED_ARGUMENTS.get(name)
    if adapter is None or not isinstance(arguments, dict):
        return arguments
    wrapper, allowed = adapter
    if wrapper in arguments or not arguments or not set(arguments) <= allowed:
        return arguments
    return {wrapper: dict(arguments)}


@dataclass(frozen=True)
class ToolSpec:
    arguments: Type[ToolArguments]
    description: str
    metadata: Optional[ToolMetadata] = None


def _meta(
    name: str,
    *,
    category: str,
    visibility: str = "agent",
    agents: tuple[str, ...] = (),
    sources: tuple[str, ...] = (),
    phases: tuple[str, ...] = ("any",),
    side_effect: str = "none",
    transport: str = "local",
    target: str = "none",
    requires: tuple[str, ...] = (),
    timeout: Optional[float] = None,
    output_bytes: Optional[int] = None,
    approval: str = "never",
    evidence: str = "none",
    retry: Optional[Dict[str, Any]] = None,
    budget: Optional[Dict[str, Any]] = None,
    failure: Optional[Dict[str, Any]] = None,
    failure_scope: str = "hard",
    continue_on_nonzero_exit: bool = False,
    requires_success_of: tuple[str, ...] = (),
    idempotency: str = "safe",
    purpose: str,
    use_when: str,
    do_not_use_when: str,
    target_description: str,
    input_description: str,
    execution_description: str,
    safety_description: str,
    output_description: str,
    failure_description: str,
    deprecation: Optional[Dict[str, Any]] = None,
) -> ToolMetadata:
    return ToolMetadata(
        name=name,
        category=category,
        visibility=visibility,
        allowed_agents=agents,
        allowed_sources=sources,
        allowed_phases=phases,
        side_effect=side_effect,
        transport=transport,
        target_kind=target,
        requires=requires,
        timeout_seconds=timeout,
        max_output_bytes=output_bytes,
        retry_policy=retry or {"mode": "none"},
        budget_cost=budget or {"tool_turns": 1},
        approval_policy=approval,
        evidence_policy=evidence,
        failure_policy=failure or {"default": "agent_retry"},
        failure_scope=failure_scope,
        continue_on_nonzero_exit=continue_on_nonzero_exit,
        requires_success_of=requires_success_of,
        idempotency=idempotency,
        purpose=purpose,
        use_when=use_when,
        do_not_use_when=do_not_use_when,
        target_description=target_description,
        input_description=input_description,
        execution_description=execution_description,
        safety_description=safety_description,
        output_description=output_description,
        failure_description=failure_description,
        deprecation=deprecation,
    )


TOOL_METADATA: Dict[str, ToolMetadata] = {
    "create_kb_draft": _meta(
        "create_kb_draft", category="knowledge",
        agents=("investigation-v1",), phases=("finalize",),
        side_effect="local_write", target="none", requires=("task.report_path", "task.run_id"),
        approval="manual_for_manual_task", evidence="report_only", idempotency="not_replay_safe",
        purpose="Create a knowledge-base draft from a completed troubleshooting report.",
        use_when="Use only after an execution report exists and the user requests knowledge persistence.",
        do_not_use_when="Do not create a draft from ContextPack or without recorded evidence.",
        target_description="No remote target. Writes through the knowledge sink after its approval gates.",
        input_description=(
            "Provide the canonical {proposal: {...}} arguments object containing "
            "a validated new-document or append-case proposal. Do not flatten "
            "proposal; the Harness accepts a safe flat legacy payload for compatibility."
        ),
        execution_description="The Harness previews, validates, and applies manual approval before persistence.",
        safety_description="Markdown lint, evidence provenance, and KB writer rules remain mandatory.",
        output_description="Returns preview, approval state, and the persisted document reference when successful.",
        failure_description="Invalid proposals are Agent-recoverable; sink/lint errors follow knowledge failure policy.",
    ),

    "create_task_artifact": _meta(
        "create_task_artifact", category="artifact",
        agents=("artifact-v1",), sources=("manual",), phases=("execute", "artifact"),
        side_effect="local_write", target="none", output_bytes=524288,
        evidence="none", idempotency="safe",
        purpose="Create a local Markdown, shell, or Python artifact for human review.",
        use_when="Use when the user explicitly requests a local document or review-only script.",
        do_not_use_when="Do not create an artifact as a substitute for executing an investigation.",
        target_description="No remote target. Writes into the current task artifact directory.",
        input_description="Provide a safe filename, bounded content, and purpose.",
        execution_description="The Harness validates content and versions duplicate filenames.",
        safety_description="Generated scripts are never executed and cannot contain destructive operations or credentials.",
        output_description="Returns the artifact path, filename, version, purpose, and size.",
        failure_description="Invalid filename or forbidden content is an Agent-recoverable error.",
    ),
}


TOOLS = {
    "create_kb_draft": ToolSpec(
        KbDraftArgs,
        "Persist the required knowledge draft after a deep dive report.",
    ),
    "create_task_artifact": ToolSpec(
        CreateArtifactArgs,
        "Create a versioned local task artifact (.md, .sh, or .py). Scripts are "
        "generated for human review only and are never executed automatically. "
        "Do not include service control, configuration mutation, deletion, or secrets.",
    ),
}
# 定义里带完整元数据:模型可见描述由元数据渲染
for _tool_name, _tool_spec in list(TOOLS.items()):
    TOOLS[_tool_name] = ToolSpec(_tool_spec.arguments, _tool_spec.description, TOOL_METADATA[_tool_name])


def validate_tool_call(name: str, arguments: Dict[str, Any]) -> ToolArguments:
    spec = TOOLS.get(name)
    if spec is None:
        raise ValueError(f"工具未登记: {name}")
    return spec.arguments.model_validate(normalize_tool_arguments(name, arguments))


def tool_definitions(allowed_tools: Optional[set[str]] = None) -> List[Dict[str, Any]]:
    return [{
        "type": "function",
        "function": {
            "name": name,
            "description": render_model_tool_description(spec.metadata),
            "parameters": deepcopy(_argument_schema(spec.arguments)),
        },
    } for name, spec in TOOLS.items() if allowed_tools is None or name in allowed_tools]


@lru_cache(maxsize=64)
def _argument_schema(arguments: Type[ToolArguments]) -> Dict[str, Any]:
    """Cache immutable base schemas; callers must deepcopy before mutation."""
    return arguments.model_json_schema()
