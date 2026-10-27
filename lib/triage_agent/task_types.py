"""Task type, alert envelope, and agent selection contracts.

This module is intentionally independent from the execution gateway.  Task
classification may inspect user-provided text and structured alert metadata,
but it must not be able to execute a diagnostic action.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Literal, Optional, Tuple

from lib.envpack.fields import TargetPlatform
from lib.sanitize import redact_secrets
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


TaskType = Literal["qa", "investigation", "alert", "artifact", "default"]
TaskTypeSource = Literal["structured", "rule", "model", "user_confirmed"]
ArtifactKind = Literal["script", "report", "document", "table"]

TASK_TYPES: Tuple[str, ...] = (
    "qa", "investigation", "alert", "artifact", "default",
)
_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|password|passwd|secret|token|authorization|credential|"
    r"private[_-]?key|ssh[_-]?key|key[_-]?file|webhook[_-]?key|totp)",
    re.IGNORECASE,
)


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AlertEnvelope(ContractModel):
    """Normalized alert input (any configured alert source) used by task creation.

    ``labels`` and ``annotations`` preserve the source fields in a bounded,
    structured form.  Routing must continue to use only ``platform`` and
    ``alertname``; execution-specific fields such as task_id stay metadata.
    """

    schema_version: Literal[1] = 1
    # 告警源类型:核心的 prometheus,或环境包 alert_sources 扩展声明的类型
    source: str = Field(default="prometheus", pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    fingerprint: Optional[str] = Field(default=None, max_length=256)
    alertname: str = Field(default="", max_length=512)
    labels: Dict[str, Any] = Field(default_factory=dict)
    annotations: Dict[str, Any] = Field(default_factory=dict)
    platform: Optional[TargetPlatform] = None
    host_raw: Optional[str] = Field(default=None, max_length=256)
    node: Optional[str] = Field(default=None, max_length=256)
    service: Optional[str] = Field(default=None, max_length=256)
    severity: Optional[str] = Field(default=None, max_length=64)
    state: Optional[str] = Field(default=None, max_length=64)
    active_at: Optional[str] = Field(default=None, max_length=128)
    value: Optional[str] = Field(default=None, max_length=512)
    detail: Optional[str] = Field(default=None, max_length=8000)
    raw_ref: Optional[str] = Field(default=None, max_length=512)

    @field_validator("platform", mode="before")
    @classmethod
    def normalize_platform(cls, value: Any) -> Any:
        return value.lower() if isinstance(value, str) else value

    @model_validator(mode="after")
    def normalize(self) -> "AlertEnvelope":
        self.alertname = self.alertname.strip()
        if self.platform:
            self.platform = self.platform.lower()  # type: ignore[assignment]
        for field in (
            "fingerprint", "host_raw", "node", "service", "severity",
            "state", "active_at", "value", "detail", "raw_ref",
        ):
            value = getattr(self, field)
            if isinstance(value, str):
                cleaned = redact_secrets(value.strip())
                setattr(self, field, cleaned or None)
        self.labels = _safe_metadata(self.labels)
        self.annotations = _safe_metadata(self.annotations)
        return self


class TaskTypeDecision(ContractModel):
    """Persisted result of the no-tool task classifier."""

    schema_version: Literal[1] = 1
    task_type: TaskType
    confidence: float = Field(ge=0, le=1)
    source: TaskTypeSource
    # The title and intent are model-assisted presentation/routing metadata.
    # They are optional for old persisted decisions and deterministic fallback
    # classifiers, but the v2 classifier asks the model to provide both.
    title: Optional[str] = Field(default=None, max_length=160)
    intent: Optional[str] = Field(default=None, max_length=160)
    reason_codes: List[str] = Field(default_factory=list, max_length=12)
    platform: Optional[TargetPlatform] = None
    service: Optional[str] = Field(default=None, max_length=256)
    component: Optional[str] = Field(default=None, max_length=256)
    node: Optional[str] = Field(default=None, max_length=256)
    alertname: Optional[str] = Field(default=None, max_length=512)
    artifact_kind: Optional[ArtifactKind] = None
    scope: Optional[Literal["node", "cluster"]] = None
    needs_clarification: List[str] = Field(default_factory=list, max_length=12)
    question: Optional[str] = Field(default=None, max_length=1000)
    options: List[str] = Field(default_factory=list, max_length=4)
    task_type_confirmed: bool = True
    classifier_version: str = Field(default="task-type-v1", max_length=64)

    @model_validator(mode="after")
    def validate_question(self) -> "TaskTypeDecision":
        if self.options and len(self.options) not in {2, 3, 4}:
            raise ValueError("AskUserQuestion 选项必须为 2 至 4 个")
        if self.options and not self.question:
            raise ValueError("存在选项时必须提供 question")
        if self.options and self.task_type_confirmed:
            self.task_type_confirmed = False
        return self


class TaskClassificationOutcome(ContractModel):
    """Auditable result of program pre-classification plus one model pass."""

    schema_version: Literal[1] = 1
    program: TaskTypeDecision
    model: Optional[TaskTypeDecision] = None
    final: TaskTypeDecision
    display_title: str = Field(min_length=1, max_length=160)
    intent: str = Field(min_length=1, max_length=160)
    conflicts: List[str] = Field(default_factory=list, max_length=12)
    needs_review: bool = False
    classifier_version: str = Field(default="task-classification-v2", max_length=64)


def envelope_from_mapping(value: Any) -> Optional[AlertEnvelope]:
    """Validate an optional alert payload without accepting arbitrary fields."""
    if value is None:
        return None
    if isinstance(value, AlertEnvelope):
        return value
    if not isinstance(value, dict):
        raise ValueError("alert_context 必须是 object")
    return AlertEnvelope.model_validate(value)


def _safe_metadata(value: Dict[str, Any]) -> Dict[str, Any]:
    """Keep alert metadata bounded and drop credential-shaped keys."""
    safe: Dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key)[:128]
        if _SENSITIVE_KEY.search(key):
            continue
        if isinstance(raw_value, (str, int, float, bool)) or raw_value is None:
            safe[key] = redact_secrets(str(raw_value))[:1024] if isinstance(raw_value, str) else raw_value
        else:
            safe[key] = redact_secrets(str(raw_value))[:1024]
    return safe
