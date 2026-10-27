import pytest
from pydantic import ValidationError

from lib.triage_agent.model_registry import (
    ModelRegistry,
    ModelUpsert,
    WorkflowDefaults,
)
from lib.triage_agent.models import TaskStatus
from lib.triage_agent.service import (
    CreateTaskRequest,
    TaskConflict,
    TaskListRequest,
    TriageTaskService,
)
from lib.triage_agent.task_store import TaskStore


def service_at(tmp_path):
    return TriageTaskService(TaskStore(tmp_path / "triage-tasks"))


def configured_service_at(tmp_path):
    ids = iter(["model-01", "model-02"])
    registry = ModelRegistry(
        tmp_path / "config" / "model.local.yaml",
        id_factory=lambda: next(ids),
    )
    registry.create_model(ModelUpsert(
        title="Primary",
        base_url="https://primary.test/v1",
        api_key="secret-primary",
        model="primary-model",
        default_reasoning_effort="low",
    ))
    registry.create_model(ModelUpsert(
        title="Automatic",
        base_url="https://automatic.test/v1",
        api_key="secret-automatic",
        model="automatic-model",
        default_reasoning_effort="xhigh",
    ))
    registry.update_defaults(WorkflowDefaults(
        auto={"model_id": "model-02", "reasoning_effort": "high"},
        manual={"model_id": "model-01", "reasoning_effort": "medium"},
    ))
    return TriageTaskService(
        TaskStore(tmp_path / "triage-tasks"),
        model_registry=registry,
    )


def test_create_manual_accepts_optional_platform_and_node(tmp_path):
    service = service_at(tmp_path)

    detail = service.create_manual(CreateTaskRequest(
        message="检查内存压力",
        platform="alpha",
        node="node12",
    ))

    assert detail.schema_version == 1
    assert detail.task.status is TaskStatus.QUEUED
    assert detail.task.platform == "alpha"
    assert detail.task.node == "node12"
    assert detail.events[0].type == "user_message"


def test_create_manual_only_requires_a_nonblank_message(tmp_path):
    service = service_at(tmp_path)

    detail = service.create_manual(CreateTaskRequest(message="检查未知故障"))

    assert detail.task.platform is None
    assert detail.task.node is None
    with pytest.raises(ValidationError):
        CreateTaskRequest(message="   ")


def test_create_manual_snapshots_manual_artifact_mode(tmp_path):
    service = service_at(tmp_path)

    detail = service.create_manual(CreateTaskRequest(
        message="检查未知故障",
        manual_artifacts=True,
    ))

    assert detail.task.manual_artifacts is True


def test_manual_default_and_explicit_override_are_snapshotted(tmp_path):
    service = configured_service_at(tmp_path)

    defaulted = service.create_manual(CreateTaskRequest(message="检查默认模型"))
    overridden = service.create_manual(CreateTaskRequest(
        message="检查备用模型",
        model_id="model-02",
        reasoning_effort="xhigh",
    ))

    assert (
        defaulted.task.model_profile_id,
        defaulted.task.model_title,
        defaulted.task.model_name,
        defaulted.task.reasoning_effort,
    ) == ("model-01", "Primary", "primary-model", "medium")
    assert (
        overridden.task.model_profile_id,
        overridden.task.reasoning_effort,
    ) == ("model-02", "xhigh")
    serialized = overridden.task.model_dump_json()
    assert "secret-automatic" not in serialized
    assert "https://automatic.test/v1" not in serialized


def test_auto_task_uses_auto_workflow_default(tmp_path):
    service = configured_service_at(tmp_path)

    detail, created = service.enqueue_auto(
        "occurrence-1",
        {
            "key": "prometheus:alpha:A:node1",
            "alertname": "A",
            "platform": "alpha",
        },
    )

    assert created is True
    assert detail.task.model_profile_id == "model-02"
    assert detail.task.reasoning_effort == "high"


def test_add_message_requeues_waiting_input(tmp_path):
    service = service_at(tmp_path)
    task = service.store.create_manual("内存异常")
    service.store.transition(task.task_id, TaskStatus.QUEUED, TaskStatus.RUNNING)
    service.store.transition(
        task.task_id,
        TaskStatus.RUNNING,
        TaskStatus.WAITING_INPUT,
        lease_owner=None,
        lease_expires_at=None,
    )

    detail = service.add_message(task.task_id, "alpha 平台，node12")

    assert detail.task.status is TaskStatus.QUEUED
    assert detail.events[-2].type == "user_message"
    assert detail.events[-1].payload == {"status": "queued"}


@pytest.mark.parametrize("failed_status", [TaskStatus.FAILED, TaskStatus.KB_FAILED])
def test_add_message_and_retry_requeue_failed_manual_task(
        tmp_path, failed_status):
    service = service_at(tmp_path)
    message_task = service.store.create_manual("消息恢复")
    service.store.transition(
        message_task.task_id, TaskStatus.QUEUED, TaskStatus.RUNNING,
    )
    service.store.transition(
        message_task.task_id, TaskStatus.RUNNING, failed_status, error="失败",
    )
    retry_task = service.store.create_manual("按钮重试")
    service.store.transition(
        retry_task.task_id, TaskStatus.QUEUED, TaskStatus.RUNNING,
    )
    service.store.transition(
        retry_task.task_id, TaskStatus.RUNNING, failed_status, error="失败",
    )

    messaged = service.add_message(message_task.task_id, "继续检查")
    retried = service.retry(retry_task.task_id)

    assert messaged.task.status is TaskStatus.QUEUED
    assert messaged.events[-2].payload == {"message": "继续检查"}
    assert retried.task.status is TaskStatus.QUEUED
    assert retried.task.error is None


def test_completed_task_resumes_by_message_but_cannot_be_cancelled(tmp_path):
    """已完成任务可续查(追加消息回 queued),但不能被取消——两者语义不同。"""
    service = service_at(tmp_path)
    task = service.store.create_manual("检查")
    service.store.transition(task.task_id, TaskStatus.QUEUED, TaskStatus.RUNNING)
    service.store.transition(task.task_id, TaskStatus.RUNNING, TaskStatus.COMPLETED)

    resumed = service.add_message(task.task_id, "继续")
    assert resumed.task.status is TaskStatus.QUEUED
    service.store.transition(task.task_id, TaskStatus.QUEUED, TaskStatus.RUNNING)
    service.store.transition(task.task_id, TaskStatus.RUNNING, TaskStatus.COMPLETED)

    with pytest.raises(TaskConflict):
        service.cancel(task.task_id)


def test_automatic_failed_task_rejects_message_and_retry(tmp_path):
    service = service_at(tmp_path)
    task, _ = service.store.enqueue_auto(
        "occurrence-1",
        {"alertname": "MemoryHigh", "platform": "alpha", "host": "node1"},
    )
    service.store.transition(task.task_id, TaskStatus.QUEUED, TaskStatus.RUNNING)
    service.store.transition(task.task_id, TaskStatus.RUNNING, TaskStatus.FAILED)

    with pytest.raises(TaskConflict):
        service.add_message(task.task_id, "继续")
    with pytest.raises(TaskConflict):
        service.retry(task.task_id)


def test_archive_requires_terminal_status_and_roundtrips(tmp_path):
    service = service_at(tmp_path)
    task = service.store.create_manual("检查")

    with pytest.raises(TaskConflict):
        service.set_archived(task.task_id, True)

    service.store.transition(task.task_id, TaskStatus.QUEUED, TaskStatus.RUNNING)
    service.store.transition(task.task_id, TaskStatus.RUNNING, TaskStatus.COMPLETED)

    archived = service.set_archived(task.task_id, True)
    assert archived.task.archived_at is not None

    restored = service.set_archived(task.task_id, False)
    assert restored.task.archived_at is None


def test_rename_updates_display_title_without_changing_request(tmp_path):
    service = service_at(tmp_path)
    task = service.store.create_manual("检查 node1 内存")

    renamed = service.rename(task.task_id, "node1 内存检查")

    assert renamed.task.display_title == "node1 内存检查"
    assert renamed.task.initial_message == "检查 node1 内存"


def test_resume_clears_archived_flag(tmp_path):
    service = service_at(tmp_path)
    task = service.store.create_manual("检查")
    service.store.transition(task.task_id, TaskStatus.QUEUED, TaskStatus.RUNNING)
    service.store.transition(
        task.task_id, TaskStatus.RUNNING, TaskStatus.FAILED, error="失败",
    )
    service.set_archived(task.task_id, True)

    resumed = service.retry(task.task_id)

    assert resumed.task.status is TaskStatus.QUEUED
    assert resumed.task.archived_at is None


def test_approval_requires_waiting_state_and_matching_id(tmp_path):
    service = service_at(tmp_path)
    task = service.store.create_manual("检查")

    with pytest.raises(TaskConflict):
        service.decide_approval(
            task.task_id, "approval-1", decision="approve",
        )


def test_revise_decision_requires_feedback(tmp_path):
    service = service_at(tmp_path)
    task = service.store.create_manual("检查")

    with pytest.raises(TaskConflict):
        service.decide_approval(
            task.task_id, "approval-1", decision="revise", feedback="  ",
        )


def test_list_filters_source_status_and_alert_key(tmp_path):
    service = service_at(tmp_path)
    manual = service.store.create_manual("人工检查")
    auto, _ = service.store.enqueue_auto(
        "occurrence-1",
        {"key": "prometheus:alpha:A:node1", "alertname": "A", "platform": "alpha"},
    )

    response = service.list_tasks(TaskListRequest(alert_key=auto.alert_key))

    assert [item.task_id for item in response.items] == [auto.task_id]
    assert manual.task_id not in {item.task_id for item in response.items}
