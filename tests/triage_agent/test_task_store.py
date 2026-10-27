import json
from datetime import datetime, timedelta, timezone

import pytest

from lib.triage_agent.models import TaskSource, TaskStatus
from lib.triage_agent.task_store import (
    InvalidTransition,
    TaskNotFound,
    TaskStore,
)


class Clock:
    def __init__(self):
        self.value = datetime(2026, 7, 25, 1, 0, tzinfo=timezone.utc)

    def now(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    ids = iter(["tr-1", "tr-2", "tr-3"])
    return TaskStore(
        tmp_path / "triage-tasks",
        now=clock.now,
        id_factory=lambda: next(ids),
    )


def test_create_manual_persists_snapshot_and_initial_message(store):
    task = store.create_manual(
        "node1 内存高",
        platform="alpha",
        node="node1",
        model_profile_id="model-01",
        model_title="Primary",
        model_name="primary-model",
        reasoning_effort="medium",
    )

    assert task.source is TaskSource.MANUAL
    assert task.model_profile_id == "model-01"
    assert task.reasoning_effort == "medium"
    assert store.get(task.task_id) == task
    events = store.events(task.task_id)
    assert [(item.type, item.payload) for item in events] == [
        ("user_message", {"message": "node1 内存高"}),
        ("status", {"status": "queued"}),
    ]


def test_create_manual_persists_command_mode_override(store):
    task = store.create_manual("调查未知命令", command_mode="bypass")

    assert task.command_mode == "bypass"
    assert store.get(task.task_id).command_mode == "bypass"


def test_auto_task_is_idempotent_per_occurrence(store):
    event = {
        "source": "prometheus",
        "platform": "alpha",
        "alertname": "CPUHigh",
        "host": "node1",
    }

    first, created = store.enqueue_auto(
        "alert-key@interval-start",
        event,
        model_profile_id="model-02",
        model_title="Automatic",
        model_name="automatic-model",
        reasoning_effort="high",
    )
    second, created_again = store.enqueue_auto(
        "alert-key@interval-start",
        event,
        model_profile_id="model-02",
        model_title="Automatic",
        model_name="automatic-model",
        reasoning_effort="high",
    )

    assert created is True
    assert created_again is False
    assert second.task_id == first.task_id
    assert first.model_profile_id == "model-02"
    assert first.reasoning_effort == "high"
    assert len(store.list()) == 1


def test_claim_is_atomic_and_stale_running_lease_can_be_reclaimed(store, clock):
    task = store.create_manual("node1 内存高")

    claimed = store.claim_next(worker_id="w1", lease_seconds=30)

    assert claimed.task_id == task.task_id
    assert claimed.status is TaskStatus.RUNNING
    assert store.claim_next(worker_id="w2", lease_seconds=30) is None

    clock.advance(31)
    reclaimed = store.claim_next(worker_id="w2", lease_seconds=30)

    assert reclaimed.task_id == task.task_id
    assert reclaimed.lease_owner == "w2"


def test_reclaim_stale_running_closes_every_unpaired_assistant_tool_call(
        store, clock):
    task = store.create_manual("node1 内存高")
    store.claim_next(worker_id="w1", lease_seconds=30)
    store.append_event(task.task_id, "assistant_message", {
        "message": {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call-started",
                    "type": "function",
                    "function": {"name": "run_command", "arguments": "{}"},
                },
                {
                    "id": "call-not-started",
                    "type": "function",
                    "function": {"name": "run_diagnostic", "arguments": "{}"},
                },
            ],
        },
    })
    store.append_event(task.task_id, "tool_call", {
        "tool_call_id": "call-started",
        "name": "run_command",
        "arguments": {},
    })
    store.record_invocation(task.task_id, "call-started", "pending")
    clock.advance(31)

    reclaimed = store.claim_next(worker_id="w2", lease_seconds=30)

    assert reclaimed.task_id == task.task_id
    results = {
        event.payload["tool_call_id"]: event.payload
        for event in store.events(task.task_id)
        if event.type == "tool_result"
    }
    assert set(results) == {"call-started", "call-not-started"}
    assert results["call-started"]["result"]["interrupted"] is True
    assert results["call-not-started"]["name"] == "run_diagnostic"
    assert results["call-not-started"]["result"]["skipped"] is True


def test_claim_prefers_oldest_queued_task(store):
    first = store.create_manual("first")
    second = store.create_manual("second")

    assert store.claim_next("worker").task_id == first.task_id
    store.transition(
        first.task_id,
        TaskStatus.RUNNING,
        TaskStatus.COMPLETED,
        lease_owner=None,
        lease_expires_at=None,
    )
    assert store.claim_next("worker").task_id == second.task_id


def test_heartbeat_extends_the_current_workers_lease(store, clock):
    task = store.create_manual("node1 内存高")
    claimed = store.claim_next(worker_id="w1", lease_seconds=30)
    first_expiry = claimed.lease_expires_at
    clock.advance(10)

    refreshed = store.heartbeat(task.task_id, worker_id="w1", lease_seconds=30)

    assert refreshed.lease_expires_at > first_expiry


def test_update_persists_fields_without_changing_status(store):
    task = store.create_manual("node1 内存高")
    store.claim_next(worker_id="w1")

    updated = store.update(
        task.task_id,
        expected=TaskStatus.RUNNING,
        run_id="ts-1",
        report_path="records/troubleshoot/ts-1/report.md",
    )

    assert updated.status is TaskStatus.RUNNING
    assert store.get(task.task_id).run_id == "ts-1"


def test_invalid_transition_and_missing_task_are_explicit(store):
    task = store.create_manual("node1 内存高")

    with pytest.raises(InvalidTransition):
        store.transition(task.task_id, TaskStatus.QUEUED, TaskStatus.COMPLETED)
    with pytest.raises(TaskNotFound):
        store.get("missing")


def test_events_can_be_read_after_a_cursor(store):
    task = store.create_manual("node1 内存高")
    first = store.events(task.task_id)[-1]
    appended = store.append_event(
        task.task_id,
        "assistant_message",
        {"message": "正在检查"},
    )

    assert store.events(task.task_id, after=first.event_id) == [appended]


def test_completed_invocation_is_not_replayed(store):
    task = store.create_manual("node1 内存高")

    store.record_invocation(
        task.task_id,
        invocation_id="call-1",
        status="done",
        result={"stdout": "ok"},
    )

    assert store.invocation_completed(task.task_id, "call-1") is True
    assert store.invocation_result(task.task_id, "call-1") == {"stdout": "ok"}


def test_task_and_invocation_files_are_valid_json(store):
    task = store.create_manual("node1 内存高")
    store.record_invocation(task.task_id, "call-1", "pending")
    task_dir = store.root / task.task_id

    assert json.loads((task_dir / "task.json").read_text())["task_id"] == task.task_id
    assert json.loads((task_dir / "invocations.json").read_text())["call-1"][
        "status"
    ] == "pending"


def test_resume_with_message_appends_message_and_requeues_under_one_operation(store):
    task = store.create_manual("node1 内存高")
    store.transition(task.task_id, TaskStatus.QUEUED, TaskStatus.RUNNING)
    store.transition(
        task.task_id,
        TaskStatus.RUNNING,
        TaskStatus.WAITING_INPUT,
        lease_owner=None,
        lease_expires_at=None,
    )

    resumed = store.resume_with_message(task.task_id, "alpha 平台，node1")

    assert resumed.status is TaskStatus.QUEUED
    assert [(event.type, event.payload) for event in store.events(task.task_id)[-2:]] == [
        ("user_message", {"message": "alpha 平台，node1"}),
        ("status", {"status": "queued"}),
    ]


def test_resume_with_message_rejects_wrong_state_without_writing(store):
    task = store.create_manual("node1 内存高")
    before = store.events(task.task_id)

    with pytest.raises(InvalidTransition):
        store.resume_with_message(task.task_id, "继续")

    assert store.events(task.task_id) == before
    assert store.get(task.task_id).status is TaskStatus.QUEUED


@pytest.mark.parametrize("failed_status", [TaskStatus.FAILED, TaskStatus.KB_FAILED])
def test_failed_manual_task_resumes_atomically_and_closes_pending_invocation(
        store, failed_status):
    task = store.create_manual("node1 内存高")
    store.transition(task.task_id, TaskStatus.QUEUED, TaskStatus.RUNNING)
    store.append_event(task.task_id, "assistant_message", {
        "message": {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "run_registered_playbook",
                        "arguments": "{}",
                    },
                },
                {
                    "id": "call-2",
                    "type": "function",
                    "function": {"name": "run_diagnostic", "arguments": "{}"},
                },
            ],
        },
    })
    store.append_event(task.task_id, "tool_call", {
        "tool_call_id": "call-1",
        "name": "run_registered_playbook",
        "arguments": {"playbook": "check.sh"},
    })
    store.record_invocation(task.task_id, "call-1", "pending")
    store.transition(
        task.task_id,
        TaskStatus.RUNNING,
        failed_status,
        error="旧错误",
        lease_owner="worker",
        lease_expires_at="2026-07-25T01:01:00+00:00",
    )

    resumed = store.resume_with_message(task.task_id, "使用新凭证继续")

    invocations = json.loads(
        (store.root / task.task_id / "invocations.json").read_text()
    )
    assert invocations["call-1"]["status"] == "failed"
    assert invocations["call-1"]["result"]["interrupted"] is True
    assert invocations["call-2"]["status"] == "failed"
    assert invocations["call-2"]["result"]["skipped"] is True
    assert resumed.status is TaskStatus.QUEUED
    assert resumed.error is None
    assert resumed.lease_owner is None
    assert resumed.lease_expires_at is None
    tail = store.events(task.task_id)[-4:]
    assert [event.type for event in tail] == [
        "tool_result", "status", "user_message", "status",
    ]
    recovered = [
        event for event in store.events(task.task_id)
        if event.type == "tool_result"
    ]
    assert {event.payload["tool_call_id"] for event in recovered} == {"call-1", "call-2"}


def test_retry_clears_failure_kind_for_failed_statuses(store):
    for failed_status, failure_kind in [
        (TaskStatus.FAILED, "turn_cap"),
        (TaskStatus.KB_FAILED, "knowledge"),
    ]:
        task = store.create_manual(f"{failed_status.value} task")
        store.transition(task.task_id, TaskStatus.QUEUED, TaskStatus.RUNNING)
        failed = store.transition(
            task.task_id,
            TaskStatus.RUNNING,
            failed_status,
            error="旧错误",
            failure_kind=failure_kind,
        )

        resumed = store.retry_failed(task.task_id)

        assert failed.failure_kind == failure_kind
        assert resumed.status is TaskStatus.QUEUED
        assert resumed.error is None
        assert resumed.failure_kind is None


def test_retry_failed_manual_task_without_new_message(store):
    task = store.create_manual("node1 内存高")
    store.transition(task.task_id, TaskStatus.QUEUED, TaskStatus.RUNNING)
    store.transition(
        task.task_id,
        TaskStatus.RUNNING,
        TaskStatus.FAILED,
        error="失败",
    )

    resumed = store.retry_failed(task.task_id)

    assert resumed.status is TaskStatus.QUEUED
    assert resumed.error is None
    assert [event.type for event in store.events(task.task_id)[-1:]] == ["status"]


def test_completed_manual_task_can_resume_by_message_but_not_retry(store):
    task = store.create_manual("node1 内存高")
    store.transition(task.task_id, TaskStatus.QUEUED, TaskStatus.RUNNING)
    store.transition(task.task_id, TaskStatus.RUNNING, TaskStatus.COMPLETED)

    resumed = store.resume_with_message(task.task_id, "补充发现")

    assert resumed.status is TaskStatus.QUEUED
    with pytest.raises(InvalidTransition):
        store.retry_failed(task.task_id)


def test_cancelled_manual_task_cannot_resume_or_retry(store):
    task = store.create_manual("node1 内存高")
    store.transition(task.task_id, TaskStatus.QUEUED, TaskStatus.CANCELLED)

    with pytest.raises(InvalidTransition):
        store.resume_with_message(task.task_id, "继续")
    with pytest.raises(InvalidTransition):
        store.retry_failed(task.task_id)


def test_completed_resume_closes_pending_invocations_defensively(store):
    task = store.create_manual("node1 内存高")
    store.transition(task.task_id, TaskStatus.QUEUED, TaskStatus.RUNNING)
    store.transition(task.task_id, TaskStatus.RUNNING, TaskStatus.COMPLETED)
    store.record_invocation(task.task_id, "call-pending", "pending")

    resumed = store.resume_with_message(task.task_id, "继续核对")

    assert resumed.status is TaskStatus.QUEUED
    assert store.invocation_completed(task.task_id, "call-pending") is False
    assert store.invocation_result(task.task_id, "call-pending")["interrupted"] is True


def test_automatic_failed_task_cannot_resume_or_retry(store):
    task, _ = store.enqueue_auto(
        "occurrence-1",
        {"alertname": "MemoryHigh", "platform": "alpha", "host": "node1"},
    )
    store.transition(task.task_id, TaskStatus.QUEUED, TaskStatus.RUNNING)
    store.transition(task.task_id, TaskStatus.RUNNING, TaskStatus.FAILED)

    with pytest.raises(InvalidTransition):
        store.resume_with_message(task.task_id, "继续")
    with pytest.raises(InvalidTransition):
        store.retry_failed(task.task_id)


def test_resolve_approval_requires_latest_matching_request(store):
    task = store.create_manual("node1 内存高")
    store.transition(task.task_id, TaskStatus.QUEUED, TaskStatus.RUNNING)
    store.append_event(
        task.task_id,
        "approval_requested",
        {"approval_id": "approval-1", "action_id": "system.inspect"},
    )
    store.transition(
        task.task_id,
        TaskStatus.RUNNING,
        TaskStatus.WAITING_APPROVAL,
        lease_owner=None,
        lease_expires_at=None,
    )
    before = store.events(task.task_id)

    with pytest.raises(InvalidTransition):
        store.resolve_approval(
            task.task_id, "approval-other", decision="approve",
        )

    assert store.events(task.task_id) == before
    resolved = store.resolve_approval(
        task.task_id,
        "approval-1",
        decision="revise",
        feedback="补充证据",
    )
    assert resolved.status is TaskStatus.QUEUED
    assert store.events(task.task_id)[-2].payload == {
        "approval_id": "approval-1",
        "approved": False,
        "decision": "revise",
        "feedback": "补充证据",
        "scope": None,
    }
