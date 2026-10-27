from pathlib import Path

import pytest
from pydantic import ValidationError

from lib.triage_agent.models import TaskSource, TaskStatus, TriageTask
from lib.triage_agent.paths import AppPaths


def test_app_paths_derive_every_runtime_path_from_repo_root(tmp_path):
    paths = AppPaths.from_repo_root(tmp_path)

    assert paths.tasks_root == tmp_path / "state" / "triage-tasks"
    assert paths.records_root == tmp_path / "records" / "troubleshoot"
    assert paths.worklog_root == tmp_path / "records" / "worklog"
    assert paths.knowledge_root == tmp_path / "knowledge"
    assert paths.routing_path == tmp_path / "build" / "routing.json"
    assert paths.recall_index_path == tmp_path / "build" / "kb-recall.sqlite"
    assert paths.model_config == tmp_path / "config" / "model.local.yaml"


def test_app_paths_accept_an_injected_state_directory(tmp_path):
    state_dir = tmp_path / "runtime"

    paths = AppPaths.from_repo_root(tmp_path, state_dir=state_dir)

    assert paths.state_dir == state_dir
    assert paths.tasks_root == state_dir / "triage-tasks"


def test_task_schema_is_versioned_and_rejects_unknown_status():
    task = TriageTask.new_manual("tr-test", message="node1 内存高")

    assert task.schema_version == 1
    assert task.source is TaskSource.MANUAL
    assert task.status is TaskStatus.QUEUED
    with pytest.raises(ValidationError):
        TriageTask.model_validate({**task.model_dump(), "status": "mystery"})


def test_task_schema_rejects_unknown_fields():
    task = TriageTask.new_manual("tr-test", message="node1 内存高")

    with pytest.raises(ValidationError):
        TriageTask.model_validate({**task.model_dump(), "unexpected": True})


def test_task_paths_are_absolute(tmp_path):
    relative = Path("relative-repo")

    paths = AppPaths.from_repo_root(tmp_path / relative)

    assert paths.repo_root.is_absolute()


def test_task_can_persist_report_and_knowledge_outputs():
    task = TriageTask.new_manual("tr-test", message="node1 内存高")

    updated = TriageTask.model_validate({
        **task.model_dump(),
        "report_path": "records/troubleshoot/ts-1/report.md",
        "kb_doc": "alpha/system/alpha-system-memory.md",
    })

    assert updated.report_path.endswith("report.md")
    assert updated.kb_doc.endswith(".md")


def test_task_model_selection_snapshot_is_optional_and_non_secret():
    legacy = TriageTask.new_manual("tr-legacy", message="检查旧任务")
    selected = TriageTask.model_validate({
        **legacy.model_dump(),
        "model_profile_id": "model-01",
        "model_title": "Primary",
        "model_name": "primary-model",
        "reasoning_effort": "high",
    })

    assert legacy.model_profile_id is None
    assert selected.model_profile_id == "model-01"
    assert selected.model_title == "Primary"
    assert selected.model_name == "primary-model"
    assert selected.reasoning_effort == "high"
    assert "api_key" not in selected.model_dump()
    assert "base_url" not in selected.model_dump()
