from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from lib.triage_agent.model_registry import (
    AgentTypePolicy,
    ModelConflict,
    ModelRegistry,
    ModelRegistryError,
    ModelUpsert,
    WorkflowDefaults,
)


def _model(
    *,
    title: str = "Primary",
    base_url: str = "https://relay.test/v1",
    api_key: str = "top-secret",
    model: str = "gpt-test",
    effort: str = "medium",
) -> ModelUpsert:
    return ModelUpsert(
        title=title,
        base_url=base_url,
        api_key=api_key,
        model=model,
        default_reasoning_effort=effort,
    )


def _write_legacy(path: Path) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(
        "provider: openai_compatible\n"
        "base_url: https://relay.test/v1\n"
        "api_key: legacy-secret\n"
        "model: legacy-model\n"
        "timeout_seconds: 60\n"
        "max_tool_turns: 8\n",
        encoding="utf-8",
    )


def test_missing_config_projects_an_empty_registry(tmp_path):
    registry = ModelRegistry(tmp_path / "config" / "model.local.yaml")

    public = registry.public_settings()

    assert public.schema_version == 1
    assert public.models == []
    assert public.defaults is None
    assert registry.is_configured() is False


def test_legacy_config_projects_one_safe_model_and_migrates_on_save(tmp_path):
    path = tmp_path / "config" / "model.local.yaml"
    _write_legacy(path)
    registry = ModelRegistry(path)

    public = registry.public_settings()

    assert public.models[0].id == "model-legacy"
    assert public.models[0].model == "legacy-model"
    assert public.models[0].api_key_configured is True
    assert public.defaults.manual.model_id == "model-legacy"
    assert public.defaults.weekly.model_id == "model-legacy"
    assert "legacy-secret" not in public.model_dump_json()

    registry.update_model(
        "model-legacy",
        _model(
            title="Legacy",
            api_key="",
            model="legacy-model",
            effort="high",
        ),
    )

    migrated = path.read_text(encoding="utf-8")
    assert "schema_version: 1" in migrated
    assert "legacy-secret" in migrated
    assert "default_reasoning_effort: high" in migrated


def test_first_model_becomes_both_defaults_and_file_is_private(tmp_path):
    registry = ModelRegistry(
        tmp_path / "config" / "model.local.yaml",
        id_factory=lambda: "model-01",
    )

    saved = registry.create_model(_model())

    assert saved.defaults.auto.model_id == "model-01"
    assert saved.defaults.manual.model_id == "model-01"
    assert saved.defaults.weekly.model_id == "model-01"
    assert saved.defaults.auto.reasoning_effort == "medium"
    assert "top-secret" not in saved.model_dump_json()
    assert registry.path.stat().st_mode & 0o777 == 0o600


def test_weekly_model_material_uses_its_separate_timeout(tmp_path):
    registry = ModelRegistry(
        tmp_path / "config" / "model.local.yaml",
        id_factory=lambda: "model-01",
    )
    registry.create_model(_model())
    task = SimpleNamespace(model_profile_id="model-01")

    assert registry.client_material_for_task(task).timeout_seconds == 120
    assert registry.client_material_for_task(task, source="weekly").timeout_seconds == 300


def test_task_type_policy_controls_model_timeout_and_turns(tmp_path):
    registry = ModelRegistry(
        tmp_path / "config" / "model.local.yaml",
        id_factory=lambda: "model-01",
    )
    registry.create_model(_model())
    policies = registry.effective_agent_policies()
    policies["investigation"] = AgentTypePolicy(
        model_id="model-01",
        reasoning_effort="high",
        max_tool_turns=40,
        model_timeout_seconds=180,
    )
    registry.update_agent_policies(policies)

    task = SimpleNamespace(
        task_type="investigation",
        model_profile_id="model-01",
    )
    material = registry.client_material_for_task(task)

    assert material.max_tool_turns == 40
    assert material.timeout_seconds == 180
    assert registry.public_settings().agent_policies["investigation"].max_tool_turns == 40
    with pytest.raises(ModelConflict, match="默认"):
        registry.delete_model("model-01", active_model_ids=set())


def test_update_preserves_blank_key_and_rejects_duplicate_title(tmp_path):
    identifiers = iter(["model-01", "model-02"])
    registry = ModelRegistry(
        tmp_path / "model.local.yaml",
        id_factory=lambda: next(identifiers),
    )
    registry.create_model(_model())
    registry.create_model(_model(
        title="Secondary",
        api_key="secondary-secret",
        model="secondary-model",
    ))

    registry.update_model(
        "model-02",
        _model(
            title="Secondary renamed",
            api_key="",
            model="secondary-model-v2",
        ),
    )
    raw = registry.load()
    secondary = next(item for item in raw.models if item.id == "model-02")

    assert secondary.api_key.get_secret_value() == "secondary-secret"
    assert secondary.model == "secondary-model-v2"
    with pytest.raises(ModelConflict, match="标题"):
        registry.update_model(
            "model-02",
            _model(title="Primary", api_key=""),
        )


def test_defaults_and_active_tasks_protect_model_deletion(tmp_path):
    identifiers = iter(["model-01", "model-02"])
    registry = ModelRegistry(
        tmp_path / "model.local.yaml",
        id_factory=lambda: next(identifiers),
    )
    registry.create_model(_model())
    registry.create_model(_model(
        title="Secondary",
        api_key="secondary-secret",
        model="secondary-model",
        effort="high",
    ))
    registry.update_defaults(WorkflowDefaults.model_validate({
        "auto": {"model_id": "model-02", "reasoning_effort": "high"},
        "manual": {"model_id": "model-02", "reasoning_effort": "xhigh"},
    }))

    with pytest.raises(ModelConflict, match="任务"):
        registry.delete_model("model-01", active_model_ids={"model-01"})

    registry.delete_model("model-01", active_model_ids=set())

    assert [item.id for item in registry.public_settings().models] == [
        "model-02",
    ]
    with pytest.raises(ModelConflict, match="默认"):
        registry.delete_model("model-02", active_model_ids=set())


def test_resolve_uses_workflow_defaults_and_allows_explicit_override(tmp_path):
    identifiers = iter(["model-01", "model-02"])
    registry = ModelRegistry(
        tmp_path / "model.local.yaml",
        id_factory=lambda: next(identifiers),
    )
    registry.create_model(_model())
    registry.create_model(_model(
        title="Secondary",
        api_key="secondary-secret",
        model="secondary-model",
        effort="xhigh",
    ))

    defaulted = registry.resolve("manual")
    overridden = registry.resolve(
        "manual",
        model_id="model-02",
        reasoning_effort="high",
    )

    assert defaulted.model_dump() == {
        "model_profile_id": "model-01",
        "model_title": "Primary",
        "model_name": "gpt-test",
        "reasoning_effort": "medium",
    }
    assert overridden.model_dump() == {
        "model_profile_id": "model-02",
        "model_title": "Secondary",
        "model_name": "secondary-model",
        "reasoning_effort": "high",
    }


def test_legacy_defaults_migrate_weekly_to_manual(tmp_path):
    path = tmp_path / "model.local.yaml"
    path.write_text(
        "schema_version: 1\n"
        "models:\n"
        "- id: model-01\n"
        "  title: Primary\n"
        "  provider: openai_compatible\n"
        "  base_url: https://relay.test/v1\n"
        "  api_key: secret\n"
        "  model: gpt-test\n"
        "  default_reasoning_effort: low\n"
        "defaults:\n"
        "  auto: {model_id: model-01, reasoning_effort: low}\n"
        "  manual: {model_id: model-01, reasoning_effort: high}\n",
        encoding="utf-8",
    )

    selection = ModelRegistry(path).resolve("weekly")

    assert selection.model_profile_id == "model-01"
    assert selection.reasoning_effort == "high"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("base_url", "ftp://relay.test/v1"),
        ("base_url", "https://user:pass@example.test/v1"),
        ("default_reasoning_effort", "max"),
    ],
)
def test_invalid_model_input_is_rejected(field, value):
    payload = _model().model_dump()
    payload[field] = value

    with pytest.raises(ValidationError):
        ModelUpsert.model_validate(payload)


def test_corrupt_config_has_a_sanitized_error(tmp_path):
    path = tmp_path / "model.local.yaml"
    path.write_text(
        "schema_version: 1\nmodels:\n  - api_key: leaked-secret\n",
        encoding="utf-8",
    )

    with pytest.raises(ModelRegistryError) as caught:
        ModelRegistry(path).load()

    assert "leaked-secret" not in str(caught.value)
