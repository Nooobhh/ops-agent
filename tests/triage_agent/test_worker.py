import json
from pathlib import Path

import pytest

from lib.triage_agent.models import TaskStatus
from lib.triage_agent.supervisor import LocalProcessSupervisor
from lib.triage_agent.task_store import TaskStore
from lib.triage_agent.worker import (
    TriageWorker,
    WorkerAlreadyRunning,
)


class CompletingOrchestrator:
    def __init__(self, store):
        self.store = store
        self.calls = []

    def run(self, task_id):
        self.calls.append(task_id)
        return self.store.transition(
            task_id,
            TaskStatus.RUNNING,
            TaskStatus.COMPLETED,
            lease_owner=None,
            lease_expires_at=None,
        )


def test_once_claims_one_task_and_exits(tmp_path):
    store = TaskStore(tmp_path / "tasks")
    task = store.create_manual("node1 内存高")
    orchestrator = CompletingOrchestrator(store)
    worker = TriageWorker(
        store=store,
        orchestrator=orchestrator,
        state_dir=tmp_path,
        worker_id="worker-1",
    )

    assert worker.run_once() is True
    assert store.get(task.task_id).status is TaskStatus.COMPLETED
    assert worker.run_once() is False
    assert orchestrator.calls == [task.task_id]


def test_worker_builds_orchestrator_from_each_claimed_task(tmp_path):
    store = TaskStore(tmp_path / "tasks")
    one = store.create_manual(
        "one",
        model_profile_id="model-01",
        model_title="One",
        model_name="one",
        reasoning_effort="low",
    )
    two = store.create_manual(
        "two",
        model_profile_id="model-02",
        model_title="Two",
        model_name="two",
        reasoning_effort="high",
    )
    built = []

    def factory(task):
        built.append((
            task.task_id,
            task.model_profile_id,
            task.reasoning_effort,
        ))
        return CompletingOrchestrator(store)

    worker = TriageWorker(
        store=store,
        orchestrator_factory=factory,
        state_dir=tmp_path,
        worker_id="worker-1",
    )

    worker.run_once()
    worker.run_once()

    assert built == [
        (one.task_id, "model-01", "low"),
        (two.task_id, "model-02", "high"),
    ]


def test_second_worker_cannot_take_process_lock(tmp_path):
    first = TriageWorker(
        store=TaskStore(tmp_path / "tasks"),
        orchestrator=object(),
        state_dir=tmp_path,
        worker_id="worker-1",
    )
    second = TriageWorker(
        store=TaskStore(tmp_path / "tasks"),
        orchestrator=object(),
        state_dir=tmp_path,
        worker_id="worker-2",
    )

    with first.process_lock():
        with pytest.raises(WorkerAlreadyRunning):
            with second.process_lock():
                pass


def test_worker_status_file_contains_no_credentials(tmp_path):
    store = TaskStore(tmp_path / "tasks")
    store.create_manual("node1 内存高")
    worker = TriageWorker(
        store=store,
        orchestrator=CompletingOrchestrator(store),
        state_dir=tmp_path,
        worker_id="worker-1",
    )

    worker.run_once()

    status = json.loads(
        (tmp_path / "triage-worker-status.json").read_text(encoding="utf-8")
    )
    assert status["worker_id"] == "worker-1"
    assert status["state"] == "idle"
    assert "api_key" not in json.dumps(status)


def test_idle_polling_neither_rereads_unchanged_tasks_nor_rewrites_status(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "tasks")
    store.create_manual("node1 内存高")
    worker = TriageWorker(
        store=store,
        orchestrator=CompletingOrchestrator(store),
        state_dir=tmp_path,
        worker_id="worker-1",
    )
    assert worker.run_once() is True
    assert worker.run_once() is False
    status_path = tmp_path / "triage-worker-status.json"
    status_file = (status_path.stat().st_ino, status_path.stat().st_mtime_ns)
    task_reads = []
    real_read_text = Path.read_text

    def counting_read_text(self, *args, **kwargs):
        if self.name == "task.json":
            task_reads.append(self)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read_text)
    for _ in range(3):
        assert worker.run_once() is False

    assert task_reads == []
    assert (status_path.stat().st_ino, status_path.stat().st_mtime_ns) == status_file
    TaskStore(tmp_path / "tasks").create_manual("node2 磁盘满")  # Web 进程新建的任务照样领得到
    assert worker.run_once() is True


def test_worker_closes_all_remote_sessions_on_stop(tmp_path):
    closed = []
    reaped = []
    def stop_after_poll(_seconds):
        worker.stop()

    worker = TriageWorker(
        store=TaskStore(tmp_path / "tasks"),
        orchestrator=object(),
        state_dir=tmp_path,
        worker_id="worker-1",
        session_reaper=lambda: reaped.append(True),
        session_closer=lambda: closed.append(True),
        sleep=stop_after_poll,
    )

    worker.run_forever()

    assert reaped
    assert closed == [True]


class FakeProcess:
    def __init__(self):
        self.pid = 1234
        self.terminated = False
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout):
        return self.returncode


def test_supervisor_launches_only_the_fixed_worker_script(tmp_path):
    calls = []
    process = FakeProcess()

    def popen(argv, cwd):
        calls.append((argv, cwd))
        return process

    supervisor = LocalProcessSupervisor(
        repo_root=tmp_path,
        state_dir=tmp_path / "state",
        python_executable="/safe/python",
        popen=popen,
    )

    supervisor.start()
    health = supervisor.health()
    supervisor.stop()

    assert calls == [([
        "/safe/python",
        str(tmp_path / "scripts" / "triage_worker.py"),
        "--repo-root",
        str(tmp_path),
        "--state-dir",
        str(tmp_path / "state"),
    ], tmp_path)]
    assert health == {"state": "running", "pid": 1234}
    assert process.terminated is True
