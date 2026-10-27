from __future__ import annotations

import os
import secrets
import tempfile
from pathlib import Path
from typing import Callable, Dict, Literal, Optional
from urllib.parse import urlparse

import yaml
from pydantic import (
    ConfigDict,
    BaseModel,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from lib.file_lock import FileLock

from .task_types import TASK_TYPES, TaskType


ReasoningEffort = Literal["none", "low", "medium", "high", "xhigh"]
WorkflowKind = Literal["auto", "manual", "weekly"]
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_WEEKLY_TIMEOUT_SECONDS = 300


class ModelRegistryError(RuntimeError):
    pass


class ModelNotFound(ModelRegistryError):
    pass


class ModelConflict(ModelRegistryError):
    pass


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelProfile(StrictModel):
    id: str
    title: str
    provider: Literal["openai_compatible"] = "openai_compatible"
    base_url: str
    api_key: SecretStr
    model: str
    default_reasoning_effort: ReasoningEffort = "none"


class ModelUpsert(StrictModel):
    title: str
    base_url: str
    api_key: str = ""
    model: str
    default_reasoning_effort: ReasoningEffort = "none"

    @field_validator("title", "model")
    @classmethod
    def nonblank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("字段不能为空")
        return stripped

    @field_validator("api_key")
    @classmethod
    def trim_key(cls, value: str) -> str:
        return value.strip()

    @field_validator("base_url")
    @classmethod
    def valid_base_url(cls, value: str) -> str:
        stripped = value.strip()
        try:
            parsed = urlparse(stripped)
            valid_port = parsed.port is None or 0 < parsed.port <= 65535
        except ValueError as exc:
            raise ValueError("Base URL 格式不合法") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or not valid_port
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Base URL 必须是无凭证的 HTTP(S) 地址")
        return stripped.rstrip("/")


class WorkflowDefault(StrictModel):
    model_id: str
    reasoning_effort: ReasoningEffort
    timeout_seconds: float = Field(default=DEFAULT_TIMEOUT_SECONDS, gt=0, le=600)


class AgentTypePolicy(StrictModel):
    """Per-task-type model and Loop limits."""

    model_id: str
    reasoning_effort: ReasoningEffort
    max_tool_turns: int = Field(default=24, ge=1, le=64)
    model_timeout_seconds: float = Field(default=DEFAULT_TIMEOUT_SECONDS, gt=0, le=600)


class WorkflowDefaults(StrictModel):
    auto: WorkflowDefault
    manual: WorkflowDefault
    weekly: WorkflowDefault
    # auto 模式下给自由命令做预筛的低价快模型(如 haiku)。可选:未配置时
    # 未登记的命令一律走人工裁决,不会因此放行任何东西
    reviewer: Optional[WorkflowDefault] = None

    @model_validator(mode="before")
    @classmethod
    def migrate_weekly_default(cls, value):
        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        if "weekly" not in migrated and "manual" in migrated:
            migrated["weekly"] = {
                **migrated["manual"],
                "timeout_seconds": DEFAULT_WEEKLY_TIMEOUT_SECONDS,
            }
        for source in ("auto", "manual", "reviewer"):
            workflow = migrated.get(source)
            if isinstance(workflow, dict) and "timeout_seconds" not in workflow:
                migrated[source] = {
                    **workflow,
                    "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
                }
        workflow = migrated.get("weekly")
        if isinstance(workflow, dict) and "timeout_seconds" not in workflow:
            migrated["weekly"] = {
                **workflow,
                "timeout_seconds": DEFAULT_WEEKLY_TIMEOUT_SECONDS,
            }
        return migrated


class ModelRegistryConfig(StrictModel):
    schema_version: Literal[1] = 1
    models: list[ModelProfile] = Field(default_factory=list)
    defaults: Optional[WorkflowDefaults] = None
    timeout_seconds: float = Field(default=DEFAULT_TIMEOUT_SECONDS, gt=0, le=600)
    weekly_timeout_seconds: float = Field(
        default=DEFAULT_WEEKLY_TIMEOUT_SECONDS,
        gt=0,
        le=600,
    )
    # 与诊断预算 16 对齐:每轮只发一个 tool_call 的模型,轮次才是真瓶颈——
    # 12 轮扣掉设定目标/召回/读文档/收口/沉淀,只剩 6-7 次采证
    max_tool_turns: int = Field(default=24, ge=1, le=64)
    # New task-type policies.  Empty means derive compatible defaults from the
    # legacy manual workflow until an operator saves the new section.
    agent_policies: Dict[TaskType, AgentTypePolicy] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def migrate_workflow_timeouts(cls, value):
        """Move legacy top-level timeouts into each workflow default."""
        if not isinstance(value, dict):
            return value
        raw = dict(value)
        defaults = raw.get("defaults")
        if not isinstance(defaults, dict):
            return raw
        migrated = dict(defaults)
        general_timeout = raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
        weekly_timeout = raw.get(
            "weekly_timeout_seconds",
            DEFAULT_WEEKLY_TIMEOUT_SECONDS,
        )
        if "weekly" not in migrated and "manual" in migrated:
            migrated["weekly"] = dict(migrated["manual"])
        for source in ("auto", "manual", "reviewer"):
            workflow = migrated.get(source)
            if isinstance(workflow, dict) and "timeout_seconds" not in workflow:
                migrated[source] = {
                    **workflow,
                    "timeout_seconds": general_timeout,
                }
        workflow = migrated.get("weekly")
        if isinstance(workflow, dict) and "timeout_seconds" not in workflow:
            migrated["weekly"] = {
                **workflow,
                "timeout_seconds": weekly_timeout,
            }
        raw["defaults"] = migrated
        return raw


class PublicModelProfile(StrictModel):
    id: str
    title: str
    base_url: str
    model: str
    api_key_configured: bool
    default_reasoning_effort: ReasoningEffort


class PublicModelSettings(StrictModel):
    schema_version: Literal[1] = 1
    models: list[PublicModelProfile] = Field(default_factory=list)
    defaults: Optional[WorkflowDefaults] = None
    agent_policies: Dict[TaskType, AgentTypePolicy] = Field(default_factory=dict)
    error: Optional[str] = None


class ModelSelection(StrictModel):
    model_profile_id: str
    model_title: str
    model_name: str
    reasoning_effort: ReasoningEffort


class ClientMaterial(StrictModel):
    base_url: str
    api_key: SecretStr
    timeout_seconds: float
    max_tool_turns: int


class ModelRegistry:
    def __init__(
        self,
        path: Path,
        *,
        id_factory: Optional[Callable[[], str]] = None,
    ):
        self.path = Path(path)
        self._lock_path = self.path.with_name(f".{self.path.name}.lock")
        self._id_factory = id_factory or (
            lambda: f"model-{secrets.token_hex(4)}"
        )

    def load(self) -> ModelRegistryConfig:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self._lock_path)):
            return self._load_unlocked()

    def is_configured(self) -> bool:
        try:
            config = self.load()
        except ModelRegistryError:
            return False
        return bool(config.models and config.defaults)

    def public_settings(self) -> PublicModelSettings:
        config = self.load()
        return self._public(config)

    def create_model(self, request: ModelUpsert) -> PublicModelSettings:
        if not request.api_key:
            raise ModelRegistryError("API Key 不能为空")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self._lock_path)):
            config = self._load_unlocked()
            self._require_unique_title(config, request.title)
            model_id = self._id_factory()
            if any(item.id == model_id for item in config.models):
                raise ModelConflict("模型 ID 已存在")
            profile = ModelProfile(
                id=model_id,
                title=request.title,
                base_url=request.base_url,
                api_key=request.api_key,
                model=request.model,
                default_reasoning_effort=request.default_reasoning_effort,
            )
            models = [*config.models, profile]
            defaults = config.defaults
            if defaults is None:
                auto_default = WorkflowDefault(
                    model_id=model_id,
                    reasoning_effort=request.default_reasoning_effort,
                )
                defaults = WorkflowDefaults(
                    auto=auto_default,
                    manual=auto_default,
                    weekly=WorkflowDefault(
                        model_id=model_id,
                        reasoning_effort=request.default_reasoning_effort,
                        timeout_seconds=300,
                    ),
                )
            changed = config.model_copy(update={
                "models": models,
                "defaults": defaults,
            })
            self._validate_config(changed)
            self._write_unlocked(changed)
            return self._public(changed)

    def update_model(
        self,
        model_id: str,
        request: ModelUpsert,
    ) -> PublicModelSettings:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self._lock_path)):
            config = self._load_unlocked()
            current = self._find(config, model_id)
            self._require_unique_title(
                config,
                request.title,
                excluding=model_id,
            )
            api_key = (
                request.api_key
                if request.api_key
                else current.api_key.get_secret_value()
            )
            replacement = ModelProfile(
                id=current.id,
                title=request.title,
                base_url=request.base_url,
                api_key=api_key,
                model=request.model,
                default_reasoning_effort=request.default_reasoning_effort,
            )
            changed = config.model_copy(update={
                "models": [
                    replacement if item.id == model_id else item
                    for item in config.models
                ],
            })
            self._validate_config(changed)
            self._write_unlocked(changed)
            return self._public(changed)

    def update_defaults(
        self,
        defaults: WorkflowDefaults,
    ) -> PublicModelSettings:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self._lock_path)):
            config = self._load_unlocked()
            changed = config.model_copy(update={"defaults": defaults})
            self._validate_config(changed)
            self._write_unlocked(changed)
            return self._public(changed)

    def update_agent_policies(
        self,
        policies: Dict[TaskType, AgentTypePolicy],
    ) -> PublicModelSettings:
        if set(policies) != set(TASK_TYPES):
            raise ModelRegistryError(
                "必须完整配置五类 Agent: " + ", ".join(TASK_TYPES)
            )
        with FileLock(str(self._lock_path)):
            config = self._load_unlocked()
            changed = config.model_copy(update={"agent_policies": dict(policies)})
            self._validate_config(changed)
            self._write_unlocked(changed)
            return self._public(changed)

    def delete_model(
        self,
        model_id: str,
        *,
        active_model_ids: set[str],
    ) -> PublicModelSettings:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self._lock_path)):
            config = self._load_unlocked()
            self._find(config, model_id)
            defaults = config.defaults
            default_model_ids = set()
            if defaults is not None:
                default_model_ids.update({
                    defaults.auto.model_id,
                    defaults.manual.model_id,
                    defaults.weekly.model_id,
                })
                if defaults.reviewer is not None:
                    default_model_ids.add(defaults.reviewer.model_id)
            default_model_ids.update(
                policy.model_id for policy in config.agent_policies.values()
            )
            if model_id in default_model_ids:
                raise ModelConflict("该模型正在被默认模型配置使用")
            if model_id in active_model_ids:
                raise ModelConflict("该模型正在被未完成任务使用")
            changed = config.model_copy(update={
                "models": [
                    item for item in config.models if item.id != model_id
                ],
            })
            self._validate_config(changed)
            self._write_unlocked(changed)
            return self._public(changed)

    def resolve(
        self,
        source: WorkflowKind,
        *,
        model_id: Optional[str] = None,
        reasoning_effort: Optional[ReasoningEffort] = None,
    ) -> ModelSelection:
        config = self.load()
        if config.defaults is None:
            raise ModelRegistryError("尚未配置默认排查模型")
        workflow = getattr(config.defaults, source)
        selected_id = model_id or workflow.model_id
        profile = self._find(config, selected_id)
        if reasoning_effort is not None:
            selected_effort = reasoning_effort
        elif model_id is not None:
            selected_effort = profile.default_reasoning_effort
        else:
            selected_effort = workflow.reasoning_effort
        return ModelSelection(
            model_profile_id=profile.id,
            model_title=profile.title,
            model_name=profile.model,
            reasoning_effort=selected_effort,
        )

    def resolve_for_task_type(
        self,
        task_type: TaskType,
        *,
        model_id: Optional[str] = None,
        reasoning_effort: Optional[ReasoningEffort] = None,
    ) -> ModelSelection:
        config = self.load()
        policy = self.effective_agent_policies(config).get(task_type)
        if policy is None:
            return self.resolve(
                "manual",
                model_id=model_id,
                reasoning_effort=reasoning_effort,
            )
        selected_id = model_id or policy.model_id
        profile = self._find(config, selected_id)
        selected_effort = (
            reasoning_effort
            if reasoning_effort is not None
            else profile.default_reasoning_effort
            if model_id is not None
            else policy.reasoning_effort
        )
        return ModelSelection(
            model_profile_id=profile.id,
            model_title=profile.title,
            model_name=profile.model,
            reasoning_effort=selected_effort,
        )

    def effective_agent_policies(
        self,
        config: Optional[ModelRegistryConfig] = None,
    ) -> Dict[TaskType, AgentTypePolicy]:
        config = config or self.load()
        policies = dict(config.agent_policies)
        workflow = config.defaults.manual if config.defaults is not None else None
        if workflow is None and config.models:
            profile = config.models[0]
            workflow = WorkflowDefault(
                model_id=profile.id,
                reasoning_effort=profile.default_reasoning_effort,
            )
        if workflow is None:
            return policies
        for task_type in TASK_TYPES:
            if task_type in policies:
                continue
            policies[task_type] = AgentTypePolicy(
                model_id=workflow.model_id,
                reasoning_effort=workflow.reasoning_effort,
                max_tool_turns=config.max_tool_turns,
                model_timeout_seconds=workflow.timeout_seconds,
            )
        return policies

    def agent_policy(
        self,
        task_type: TaskType,
    ) -> Optional[AgentTypePolicy]:
        return self.effective_agent_policies().get(task_type)

    def client_material_for_task(
        self,
        task,
        *,
        source: WorkflowKind = "manual",
    ) -> ClientMaterial:
        config = self.load()
        task_type = getattr(task, "task_type", None)
        policy = None
        snapshot = getattr(task, "agent_policy_snapshot", None)
        if isinstance(snapshot, dict):
            try:
                policy = AgentTypePolicy.model_validate(snapshot)
            except (TypeError, ValueError):
                policy = None
        if policy is None:
            policy = (
                self.effective_agent_policies(config).get(task_type)
                if task_type in TASK_TYPES
                else None
            )
        workflow = getattr(config.defaults, source, None) if config.defaults else None
        selected_model_id = (
            str(task.model_profile_id or "")
            or (policy.model_id if policy is not None else "")
            or (workflow.model_id if workflow is not None else "")
        )
        profile = self._find(config, selected_model_id)
        return ClientMaterial(
            base_url=profile.base_url,
            api_key=profile.api_key,
            timeout_seconds=(
                policy.model_timeout_seconds
                if policy is not None
                else workflow.timeout_seconds
                if workflow is not None
                else (
                    config.weekly_timeout_seconds
                    if source == "weekly"
                    else config.timeout_seconds
                )
            ),
            max_tool_turns=(
                policy.max_tool_turns
                if policy is not None
                else config.max_tool_turns
            ),
        )

    def client_material_for_selection(
        self,
        selection: ModelSelection | Dict[str, str],
        *,
        source: WorkflowKind = "manual",
    ) -> ClientMaterial:
        """Return transport material before a Task exists.

        Manual task creation uses this for the single no-tool classification
        request. It resolves the same selected profile without persisting or
        exposing the API key in the task record.
        """
        config = self.load()
        values = (
            selection.model_dump()
            if isinstance(selection, ModelSelection)
            else dict(selection)
        )
        profile = self._find(config, str(values.get("model_profile_id") or ""))
        workflow = getattr(config.defaults, source, None) if config.defaults else None
        return ClientMaterial(
            base_url=profile.base_url,
            api_key=profile.api_key,
            timeout_seconds=(
                workflow.timeout_seconds
                if workflow is not None
                else config.timeout_seconds
            ),
            max_tool_turns=config.max_tool_turns,
        )

    def _load_unlocked(self) -> ModelRegistryConfig:
        if not self.path.is_file():
            return ModelRegistryConfig()
        try:
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8"))
            if self._is_legacy(raw):
                return self._legacy_config(raw)
            config = ModelRegistryConfig.model_validate(raw)
            self._validate_config(config)
            return config
        except ModelRegistryError:
            raise
        except Exception as exc:
            raise ModelRegistryError(
                f"模型配置无效: {type(exc).__name__}"
            ) from exc

    @staticmethod
    def _is_legacy(raw) -> bool:
        return (
            isinstance(raw, dict)
            and "schema_version" not in raw
            and {"base_url", "api_key", "model"}.issubset(raw)
        )

    def _legacy_config(self, raw: dict) -> ModelRegistryConfig:
        request = ModelUpsert(
            title=str(raw.get("title") or raw["model"]),
            base_url=raw["base_url"],
            api_key=raw["api_key"],
            model=raw["model"],
            default_reasoning_effort=raw.get(
                "default_reasoning_effort",
                "none",
            ),
        )
        if not request.api_key:
            raise ModelRegistryError("模型配置无效: API Key 不能为空")
        profile = ModelProfile(
            id="model-legacy",
            title=request.title,
            provider=raw.get("provider", "openai_compatible"),
            base_url=request.base_url,
            api_key=request.api_key,
            model=request.model,
            default_reasoning_effort=request.default_reasoning_effort,
        )
        workflow_default = WorkflowDefault(
            model_id=profile.id,
            reasoning_effort=profile.default_reasoning_effort,
            timeout_seconds=raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
        )
        weekly_default = workflow_default.model_copy(update={
            "timeout_seconds": raw.get(
                "weekly_timeout_seconds", DEFAULT_WEEKLY_TIMEOUT_SECONDS,
            ),
        })
        return ModelRegistryConfig(
            models=[profile],
            defaults=WorkflowDefaults(
                auto=workflow_default,
                manual=workflow_default,
                weekly=weekly_default,
            ),
            timeout_seconds=raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
            weekly_timeout_seconds=raw.get(
                "weekly_timeout_seconds", DEFAULT_WEEKLY_TIMEOUT_SECONDS,
            ),
            max_tool_turns=raw.get("max_tool_turns", 24),
        )

    @staticmethod
    def _validate_config(config: ModelRegistryConfig) -> None:
        ids = [item.id for item in config.models]
        titles = [item.title for item in config.models]
        if len(ids) != len(set(ids)):
            raise ModelConflict("模型 ID 不能重复")
        if len(titles) != len(set(titles)):
            raise ModelConflict("模型标题不能重复")
        if config.models and config.defaults is None:
            raise ModelRegistryError("已配置模型时必须设置排查默认值")
        if config.defaults is not None:
            required = {
                config.defaults.auto.model_id,
                config.defaults.manual.model_id,
                config.defaults.weekly.model_id,
            }
            if config.defaults.reviewer is not None:
                required.add(config.defaults.reviewer.model_id)
            missing = required - set(ids)
            if missing:
                raise ModelRegistryError("默认排查模型不存在")
        missing_agent_models = {
            policy.model_id
            for policy in config.agent_policies.values()
            if policy.model_id not in ids
        }
        if missing_agent_models:
            raise ModelRegistryError("Agent 类型策略模型不存在")

    @staticmethod
    def _find(
        config: ModelRegistryConfig,
        model_id: str,
    ) -> ModelProfile:
        for item in config.models:
            if item.id == model_id:
                return item
        raise ModelNotFound("模型不存在")

    @staticmethod
    def _require_unique_title(
        config: ModelRegistryConfig,
        title: str,
        *,
        excluding: Optional[str] = None,
    ) -> None:
        if any(
            item.title == title and item.id != excluding
            for item in config.models
        ):
            raise ModelConflict("模型标题已存在")

    def _public(self, config: ModelRegistryConfig) -> PublicModelSettings:
        return PublicModelSettings(
            models=[
                PublicModelProfile(
                    id=item.id,
                    title=item.title,
                    base_url=item.base_url,
                    model=item.model,
                    api_key_configured=bool(
                        item.api_key.get_secret_value()
                    ),
                    default_reasoning_effort=(
                        item.default_reasoning_effort
                    ),
                )
                for item in config.models
            ],
            defaults=config.defaults,
            agent_policies=self.effective_agent_policies(config),
        )

    def _write_unlocked(self, config: ModelRegistryConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "models": [
                {
                    "id": item.id,
                    "title": item.title,
                    "provider": item.provider,
                    "base_url": item.base_url,
                    "api_key": item.api_key.get_secret_value(),
                    "model": item.model,
                    "default_reasoning_effort": (
                        item.default_reasoning_effort
                    ),
                }
                for item in config.models
            ],
            "defaults": (
                config.defaults.model_dump(mode="json")
                if config.defaults is not None
                else None
            ),
            "timeout_seconds": config.timeout_seconds,
            "weekly_timeout_seconds": config.weekly_timeout_seconds,
            "max_tool_turns": config.max_tool_turns,
            "agent_policies": {
                key: value.model_dump(mode="json")
                for key, value in config.agent_policies.items()
            },
        }
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                yaml.safe_dump(
                    payload,
                    handle,
                    allow_unicode=True,
                    sort_keys=False,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
            os.chmod(self.path, 0o600)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.remove(temp_name)
            except FileNotFoundError:
                pass
            raise
