"""Static metadata and provider-facing descriptions for Agent tools."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Sequence, Tuple, Type


@dataclass(frozen=True)
class ToolMetadata:
    name: str
    version: str = "1.0"
    category: str = "control"
    visibility: str = "agent"
    allowed_agents: Tuple[str, ...] = ()
    allowed_sources: Tuple[str, ...] = ()
    allowed_phases: Tuple[str, ...] = ("any",)
    side_effect: str = "none"
    transport: str = "local"
    target_kind: str = "none"
    requires: Tuple[str, ...] = ()
    timeout_seconds: Optional[float] = None
    max_output_bytes: Optional[int] = None
    retry_policy: Dict[str, Any] = field(default_factory=dict)
    budget_cost: Dict[str, Any] = field(default_factory=dict)
    approval_policy: str = "never"
    evidence_policy: str = "none"
    failure_policy: Dict[str, Any] = field(default_factory=dict)
    # Controls whether a recoverable error stops the remaining calls in the
    # current sequential batch. This is a continuation policy, not a safety
    # override: admission and redline gates still run for every call.
    failure_scope: str = "hard"
    continue_on_nonzero_exit: bool = False
    requires_success_of: Tuple[str, ...] = ()
    idempotency: str = "safe"
    purpose: str = ""
    use_when: str = ""
    do_not_use_when: str = ""
    target_description: str = ""
    input_description: str = ""
    execution_description: str = ""
    safety_description: str = ""
    output_description: str = ""
    failure_description: str = ""
    deprecation: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class ToolSpecV2:
    """Provider-independent tool definition.

    ``arguments`` is kept as Any to avoid importing the Pydantic models here;
    the compatibility layer in tools.py supplies the concrete model class.
    """

    arguments: Type[Any]
    metadata: ToolMetadata
    description: str



def render_model_tool_description(metadata: ToolMetadata) -> str:
    """Render a compact provider-facing description.

    Model schemas only need the operational purpose, input shape, key safety
    boundary, output semantics, and failure recovery hint.  Admission remains
    authoritative in code; this is a context optimization, not a permission
    change.
    """
    sections = (
        ("Purpose", metadata.purpose),
        ("Input", metadata.input_description),
        ("Safety", metadata.safety_description),
        ("Output", metadata.output_description),
        ("Failure", metadata.failure_description),
    )
    return "\n\n".join(
        f"{title}:\n{body.strip()}"
        for title, body in sections
        if body and body.strip()
    )


def tuple_of(values: Optional[Sequence[str]]) -> Tuple[str, ...]:
    return tuple(str(value) for value in (values or ()))
