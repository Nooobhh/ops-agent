from lib.triage_agent.models import TaskStatus
from lib.triage_agent.task_store import TaskStore


def test_claim_assigns_execution_id(tmp_path):
    store = TaskStore(tmp_path / "tasks")
    created = store.create_manual("检查 node1", platform="alpha", node="node1")
    claimed = store.claim_next("worker")

    assert claimed.task_id == created.task_id
    assert claimed.execution_id.startswith("exec-")
    assert store.get(created.task_id).execution_id == claimed.execution_id


def test_waiting_input_resume_keeps_execution_id_but_terminal_resume_rotates_it(tmp_path):
    store = TaskStore(tmp_path / "tasks")
    created = store.create_manual("检查 node1", platform="alpha", node="node1")
    claimed = store.claim_next("worker")
    execution_id = claimed.execution_id
    store.transition(
        created.task_id,
        TaskStatus.RUNNING,
        TaskStatus.WAITING_INPUT,
        lease_owner=None,
        lease_expires_at=None,
    )
    waiting_resume = store.resume_with_message(created.task_id, "继续")
    assert waiting_resume.execution_id == execution_id

    store.transition(
        created.task_id,
        TaskStatus.QUEUED,
        TaskStatus.RUNNING,
        lease_owner="worker",
        lease_expires_at=None,
    )
    store.transition(
        created.task_id,
        TaskStatus.RUNNING,
        TaskStatus.WAITING_INPUT,
        lease_owner=None,
        lease_expires_at=None,
    )
    store.transition(
        created.task_id,
        TaskStatus.WAITING_INPUT,
        TaskStatus.COMPLETED,
        lease_owner=None,
        lease_expires_at=None,
    )
    terminal_resume = store.resume_with_message(created.task_id, "追加证据")
    assert terminal_resume.execution_id != execution_id
    assert terminal_resume.status is TaskStatus.QUEUED
