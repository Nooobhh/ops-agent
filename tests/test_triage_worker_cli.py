import json

from lib.triage_agent.model_registry import ModelRegistry, ModelUpsert
from lib.triage_agent.models import TaskStatus
from lib.triage_agent.paths import AppPaths
from lib.triage_agent.task_store import TaskStore
from scripts import triage_worker


class FakeWorker:
    def __init__(self, had_task):
        self.had_task = had_task
        self.once_calls = 0

    def run_once(self):
        self.once_calls += 1
        return self.had_task


def test_once_cli_emits_json_and_does_not_enter_forever_loop(
    tmp_path,
    monkeypatch,
    capsys,
):
    worker = FakeWorker(had_task=True)
    monkeypatch.setattr(triage_worker, "build_worker", lambda _paths: worker)

    rc = triage_worker.run([
        "--repo-root", str(tmp_path),
        "--state-dir", str(tmp_path / "state"),
        "--once",
    ])

    assert rc == 0
    assert worker.once_calls == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "processed": True,
    }


def test_once_cli_reports_configuration_failure_without_secret(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(
        triage_worker,
        "build_worker",
        lambda _paths: (_ for _ in ()).throw(ValueError("invalid config")),
    )

    rc = triage_worker.run([
        "--repo-root", str(tmp_path),
        "--state-dir", str(tmp_path / "state"),
        "--once",
    ])

    output = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert output["ok"] is False
    assert output["error"] == "ValueError"


def test_build_worker_snapshots_missing_model_and_runs_new_runtime(
    tmp_path,
    monkeypatch,
):
    paths = AppPaths.from_repo_root(tmp_path)
    registry = ModelRegistry(
        paths.model_config,
        id_factory=lambda: "model-01",
    )
    registry.create_model(ModelUpsert(
        title="Primary",
        base_url="https://relay.test/v1",
        api_key="top-secret",
        model="primary-model",
        default_reasoning_effort="high",
    ))
    store = TaskStore(paths.tasks_root, id_factory=lambda: "tr-new")
    task = store.create_manual("检查任务")
    built = []

    class Completing:
        def __init__(self, **kwargs):
            built.append(kwargs)
            self.store = kwargs["store"]

        def run(self, task_id):
            self.store.transition(
                task_id,
                TaskStatus.RUNNING,
                TaskStatus.COMPLETED,
                lease_owner=None,
                lease_expires_at=None,
            )

    monkeypatch.setattr(
        triage_worker.LogCatalog,
        "load",
        lambda _path: object(),
    )
    monkeypatch.setattr(
        triage_worker.DiagnosticRegistry,
        "load",
        lambda _path, log_catalog=None, repo_root=None: object(),
    )
    monkeypatch.setattr(
        triage_worker,
        "ActionGateway",
        lambda _paths, _diagnostics: object(),
    )
    monkeypatch.setattr(
        triage_worker,
        "KnowledgeSink",
        lambda _paths: object(),
    )
    monkeypatch.setattr(triage_worker, "AgentRuntime", Completing)

    worker = triage_worker.build_worker(paths)

    assert worker.run_once() is True
    persisted = store.get(task.task_id)
    assert persisted.model_profile_id == "model-01"
    assert persisted.model_title == "Primary"
    assert persisted.model_name == "primary-model"
    assert persisted.reasoning_effort == "high"
    assert built[0]["model"].config.model == "primary-model"
    assert built[0]["model"].config.reasoning_effort == "high"
    assert built[0]["model"].lenient is True


def test_build_worker_injection_replaces_only_environment_sink_and_transport(
    tmp_path,
    monkeypatch,
):
    paths = AppPaths.from_repo_root(tmp_path)
    ModelRegistry(paths.model_config, id_factory=lambda: "model-01").create_model(ModelUpsert(
        title="Primary",
        base_url="https://relay.test/v1",
        api_key="top-secret",
        model="primary-model",
        default_reasoning_effort="high",
    ))
    store = TaskStore(paths.tasks_root, id_factory=lambda: "tr-injected")
    store.create_manual("检查注入")
    built = []

    class Completing:
        def __init__(self, **kwargs):
            built.append(kwargs)
            self.store = kwargs["store"]

        def run(self, task_id):
            self.store.transition(
                task_id, TaskStatus.RUNNING, TaskStatus.COMPLETED,
                lease_owner=None, lease_expires_at=None,
            )

    monkeypatch.setattr(triage_worker.LogCatalog, "load", lambda _path: object())
    monkeypatch.setattr(
        triage_worker.DiagnosticRegistry, "load", lambda _path, log_catalog=None, repo_root=None: "registry",
    )
    monkeypatch.setattr(triage_worker, "AgentRuntime", Completing)
    actions, sink, transport = object(), object(), object()
    factory_calls = []

    worker = triage_worker.build_worker(
        paths,
        actions_factory=lambda got_paths, diagnostics: factory_calls.append(
            (got_paths, diagnostics)
        ) or actions,
        knowledge_sink=sink,
        model_transport=transport,
    )

    assert worker.run_once() is True
    assert factory_calls == [(paths, "registry")]
    assert built[0]["actions"] is actions
    assert built[0]["knowledge_sink"] is sink
    assert built[0]["model"]._client._transport is transport


def test_legacy_task_reaching_the_queue_is_closed_without_running(tmp_path, monkeypatch):
    """A3.7 之前由旧编排器执行的任务(harness_tier 为空)再排队时直接关闭并说明,不建模型客户端。"""
    paths = AppPaths.from_repo_root(tmp_path)
    store = TaskStore(paths.tasks_root, id_factory=lambda: "tr-legacy")
    task = store.create_manual("旧任务")
    store.update(task.task_id, expected=TaskStatus.QUEUED, harness_tier=None)
    monkeypatch.setattr(triage_worker.LogCatalog, "load", lambda _path: object())
    monkeypatch.setattr(triage_worker.DiagnosticRegistry, "load", lambda _path, log_catalog=None, repo_root=None: object())

    worker = triage_worker.build_worker(paths, actions_factory=lambda _paths, _diagnostics: object(),
                                        knowledge_sink=object())

    assert worker.run_once() is True
    closed = store.get(task.task_id)
    assert closed.status is TaskStatus.FAILED and closed.failure_kind == "legacy_runtime_retired"
    assert "旧版 Agent 已下线" in closed.error
