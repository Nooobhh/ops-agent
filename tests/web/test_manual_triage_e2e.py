import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from lib.agent_runtime import AgentRuntime
from lib.agent_runtime.loop import KB_DRAFT_TRIGGER

from lib.triage_agent.model_client import ModelReply, ToolCall
from lib.triage_agent.model_registry import ModelRegistry, ModelUpsert
from lib.triage_agent.models import TaskStatus
from lib.triage_agent.task_store import TaskStore
from lib.triage_agent.worker import TriageWorker
from web.server.app import create_app


def text_reply(content):
    return ModelReply(
        content=content,
        tool_calls=[],
        assistant_message={"role": "assistant", "content": content},
    )


def tool_reply(name, arguments, call_id):
    return ModelReply(
        content=None,
        tool_calls=[ToolCall(
            call_id=call_id,
            name=name,
            arguments=arguments,
        )],
        assistant_message={
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }],
        },
    )


class ScriptedModel:
    base_url = "https://model.internal.example/v1"
    api_key = "must-not-leak"

    def __init__(self, replies):
        self.replies = list(replies)

    def complete(self, _messages, _tools):
        return self.replies.pop(0)


class FakeActions:
    def __init__(self, repo_root):
        self.paths = SimpleNamespace(repo_root=Path(repo_root))
        self.evidence = []
        self.diagnostic_calls = []

    def known_nodes(self, _platform):
        return ["node1", "node12"]

    def resolve_node(self, _platform, value):
        return value if value in self.known_nodes(_platform) else None

    def start_session(self, **_kwargs):
        run_id = "ts-manual-e2e"
        (self.paths.repo_root / "records" / "troubleshoot" / run_id).mkdir(
            parents=True,
        )
        return SimpleNamespace(run_id=run_id)

    def run_diagnostic(
        self,
        _run_id,
        *,
        platform,
        node,
        diagnostic_id,
        params,
        execution_id=None,
    ):
        self.diagnostic_calls.append(
            (platform, node, diagnostic_id, params)
        )
        result = {
            "diagnostic_id": diagnostic_id,
            "node": node,
            "exit_code": 0,
            "stdout": "MemAvailable: 1024 MB",
            "stderr": "",
            "duration_seconds": 0.1,
        }
        self.evidence.append(result)
        return result

    def record_finish(self, run_id, **_fields):
        report = (
            self.paths.repo_root / "records" / "troubleshoot"
            / run_id / "report.md"
        )
        report.write_text("# report\n", encoding="utf-8")
        return report

    def record_turn_summary(self, run_id, _summary):
        return self.paths.repo_root / "records" / "troubleshoot" / run_id / "report.md"

    def session_data(self, _run_id):
        return {"steps": self.evidence}


class FakeKnowledgeSink:
    def __init__(self, repo_root):
        self.repo_root = Path(repo_root)

    def preview(self, _run_id, proposal):
        return SimpleNamespace(
            path=self.repo_root / "knowledge" / "alpha" / "system" / "memory.md",
            mode=proposal.get("mode", "new"),
            markdown="# memory draft\n",
        )

    def persist(self, _run_id, _proposal):
        path = self.repo_root / "knowledge" / "alpha" / "system" / "memory.md"
        path.parent.mkdir(parents=True)
        path.write_text("# memory\n", encoding="utf-8")
        return SimpleNamespace(path=path)


class FakeSupervisor:
    def reconcile(self, configured):
        if configured:
            self.start()
        else:
            self.stop()

    def start(self):
        pass

    def stop(self):
        pass

    def health(self):
        return {"state": "running", "pid": 123}


def test_manual_api_worker_diagnostic_finish_kb_draft_and_complete(
    tmp_path,
    monkeypatch,
):
    from web.server import app as app_module

    monkeypatch.setattr(app_module.sys, "platform", "linux")
    state_dir = tmp_path / "state"
    store = TaskStore(state_dir / "triage-tasks")
    actions = FakeActions(tmp_path)
    model = ScriptedModel([
        tool_reply(
            "diagnostic",
            {"node": "node12", "diagnostic_id": "system.memory_summary", "params": {}},
            "diagnostic-1",
        ),
        tool_reply(
            "finish",
            {
                "answer": "node12 内存紧张",
                "confidence": "high",
                "facts": [{"ref": "step:1", "match": "MemAvailable: 1024 MB"}],
            },
            "final-1",
        ),
        tool_reply(
            "create_kb_draft",
            {
                "proposal": {
                    "mode": "new",
                    "title": "memory pressure",
                    "platform": "alpha",
                    "service": "system",
                    "problem": "memory",
                    "symptoms": ["memory high"],
                    "alerts": [],
                    "mechanism": "pressure",
                    "evidence": ["MemAvailable: 1024 MB"],
                    "remediation": ["observe"],
                },
            },
            "kb-1",
        ),
        text_reply("知识草稿已落档。"),
    ])
    worker = TriageWorker(
        store=store,
        orchestrator=AgentRuntime(
            store=store,
            model=model,
            actions=actions,
            knowledge_sink=FakeKnowledgeSink(tmp_path),
            retry_delays=(),
            sleep=lambda _: None,
        ),
        state_dir=state_dir,
        worker_id="e2e-worker",
    )
    ModelRegistry(tmp_path / "config" / "model.local.yaml").create_model(
        ModelUpsert(
            title="Primary",
            base_url="https://relay.test/v1",
            api_key="secret",
            model="primary",
            default_reasoning_effort="medium",
        )
    )
    (tmp_path / "config" / "platform-alpha.yaml").write_text(
        "platform: alpha\ncollected_at: '2026-10-01'\nnodes:\n"
        "  node1: {host: 198.51.100.101, components: [NAMENODE]}\n  node12: {host: 198.51.100.112, components: [DATANODE]}\n",
        encoding="utf-8",
    )
    app = create_app(
        state_dir=state_dir,
        repo_root=tmp_path,
        triage_supervisor=FakeSupervisor(),
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/triage-tasks",
            json={"message": "检查 alpha node12 内存压力", "platform": "alpha", "node": "node12",
                  "manual_artifacts": True},
        )
        assert created.status_code == 201
        task_id = created.json()["task"]["task_id"]
        assert created.json()["task"]["harness_tier"] == "autonomous"

        assert worker.run_once() is True
        answered = client.get(f"/api/triage-tasks/{task_id}").json()
        assert answered["task"]["status"] == "waiting_input"
        assert answered["task"]["report_path"].endswith("report.md")

        # 右侧「生成知识草稿」入口:这一轮只开放 create_kb_draft,人工同意即落档
        resumed = client.post(f"/api/triage-tasks/{task_id}/messages", json={"message": KB_DRAFT_TRIGGER})
        assert resumed.status_code == 200 and resumed.json()["task"]["status"] == "queued"
        assert worker.run_once() is True
        pending = client.get(f"/api/triage-tasks/{task_id}").json()
        assert pending["task"]["status"] == "waiting_approval"
        review = next(
            event["payload"] for event in reversed(pending["events"])
            if event["type"] == "approval_requested"
        )
        assert review["kind"] == "kb_draft" and review["markdown"] == "# memory draft\n"

        decided = client.post(
            f"/api/triage-tasks/{task_id}/approvals/{review['approval_id']}",
            json={"decision": "approve"},
        )
        assert decided.status_code == 200 and decided.json()["task"]["status"] == "queued"
        assert worker.run_once() is True
        draft = client.get(f"/api/triage-tasks/{task_id}/kb-doc")
        assert draft.status_code == 200 and draft.text == "# memory\n"

        done = client.post(f"/api/triage-tasks/{task_id}/complete")
        assert done.status_code == 200
        completed = client.get(f"/api/triage-tasks/{task_id}").json()

    task = completed["task"]
    assert task["status"] == "completed"
    assert task["platform"] == "alpha" and task["node"] == "node12"
    assert task["kb_doc"].endswith("memory.md")
    assert actions.diagnostic_calls == [("alpha", "node12", "system.memory_summary", {})]

    events = completed["events"]
    event_ids = [event["event_id"] for event in events]
    assert len(event_ids) == len(set(event_ids))
    assert any(
        event["type"] == "tool_result"
        and event["payload"].get("tool_call_id") == "diagnostic-1"
        for event in events
    )
    encoded = json.dumps(completed, ensure_ascii=False)
    assert model.api_key not in encoded
    assert model.base_url not in encoded


def test_completed_manual_task_can_be_resumed_and_claimed(tmp_path):
    state_dir = tmp_path / "state"
    store = TaskStore(state_dir / "triage-tasks")
    task = store.create_manual("已完成排查")
    store.transition(task.task_id, TaskStatus.QUEUED, TaskStatus.RUNNING)
    store.transition(task.task_id, TaskStatus.RUNNING, TaskStatus.COMPLETED)

    from web.server.app import create_app
    with TestClient(create_app(state_dir=state_dir, repo_root=tmp_path)) as client:
        response = client.post(
            f"/api/triage-tasks/{task.task_id}/messages",
            json={"message": "继续追问"},
        )

    assert response.status_code == 200
    assert response.json()["task"]["status"] == "queued"
    assert store.claim_next("worker").task_id == task.task_id


def test_legacy_task_is_read_only(tmp_path):
    """A3.7 之前由旧编排器执行的任务(harness_tier 为空):可查看,不能追加消息、重试或改运行时控制项。"""
    state_dir = tmp_path / "state"
    store = TaskStore(state_dir / "triage-tasks")
    task = store.create_manual("旧任务")
    store.update(task.task_id, expected=TaskStatus.QUEUED, harness_tier=None)
    store.transition(task.task_id, TaskStatus.QUEUED, TaskStatus.RUNNING)
    store.transition(task.task_id, TaskStatus.RUNNING, TaskStatus.COMPLETED)

    with TestClient(create_app(state_dir=state_dir, repo_root=tmp_path)) as client:
        assert client.get(f"/api/triage-tasks/{task.task_id}").status_code == 200
        response = client.post(f"/api/triage-tasks/{task.task_id}/messages", json={"message": "继续追问"})

    assert response.status_code == 409 and "旧版 Agent 已下线" in response.text
    assert store.get(task.task_id).status is TaskStatus.COMPLETED
