import json
from pathlib import Path

from lib.agent_runtime import AgentRuntime
from lib.record import lint_records
from lib.ssh_pool import CmdResult
from lib.triage_agent.actions import ActionGateway
from lib.triage_agent.diagnostics import DiagnosticRegistry
from lib.triage_agent.model_client import ModelReply, ToolCall
from lib.triage_agent.models import TaskStatus
from lib.triage_agent.paths import AppPaths
from lib.triage_agent.task_store import TaskStore
from lib.triage_agent.worker import TriageWorker
from tests.envpack_helpers import as_env_pack


PLAYBOOK_JSON = json.dumps({
    "playbook": "alpha/system/check_memory",
    "node": "node1",
    "verdict": "needs_llm",
    "severity": "P1",
    "conclusion_hint": "memory pressure",
    "steps": [{
        "name": "memory",
        "cmd": "free -m",
        "expect": "healthy",
        "actual": "memory actual",
        "pass": False,
        "raw": "memory actual",
    }],
    "metrics": {"used_pct": 95},
})


class FixturePool:
    def __init__(self):
        self.calls = []
        self.pushes = []

    def run(self, node, cmd, timeout=30, approved=False):
        self.calls.append((node, cmd, timeout, approved))
        if "bash /tmp/opsagent/" in cmd:
            stdout = PLAYBOOK_JSON
        elif cmd == "free -m":
            stdout = "memory actual"
        else:
            stdout = ""
        return CmdResult(node, cmd, 0, stdout, "", 0.1)

    def push(self, node, local, remote):
        self.pushes.append((node, local, remote))

    def components(self, node):
        return ["DATANODE"]


class ScriptedModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        return self.replies.pop(0)


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


def _repo(tmp_path, *, with_route):
    as_env_pack(tmp_path)
    paths = AppPaths.from_repo_root(tmp_path)
    paths.state_dir.mkdir()
    paths.knowledge_root.mkdir()
    (tmp_path / "config" / "agent-diagnostics.yaml").write_text(
        """
version: 1
diagnostics:
  - id: system.memory_summary
    platforms: [alpha]
    params: {}
    command: "free -m"
    timeout_seconds: 15
approved_actions: []
""",
        encoding="utf-8",
    )
    (tmp_path / "config" / "platform-alpha.yaml").write_text(
        "platform: alpha\ncollected_at: '2026-10-01'\nnodes:\n  node1: {host: 198.51.100.101, components: [NAMENODE]}\n",
        encoding="utf-8",
    )
    playbook = tmp_path / "playbooks" / "alpha" / "system" / "check_memory.sh"
    playbook.parent.mkdir(parents=True)
    playbook.write_text("#!/bin/bash\n", encoding="utf-8")
    build = tmp_path / "build"
    build.mkdir()
    route = {
        "doc": "alpha/system/memory.md",
        "playbook": "playbooks/alpha/system/check_memory.sh",
        "platform": "alpha",
        "service": "system",
    }
    (build / "routing.json").write_text(json.dumps({
        "alerts": {"alpha": {"MemoryHigh": route}} if with_route else {},
        "docs": [{
            "path": "alpha/system/memory.md",
            "title": "Memory",
            "type": "troubleshoot",
            "platform": "alpha",
            "service": "system",
            "problem": "memory",
            "symptoms": ["memory"],
            "playbook": route["playbook"],
            "status": "draft",
        }] if with_route else [],
    }), encoding="utf-8")
    return paths


def _worker(paths, model, pool):
    store = TaskStore(paths.tasks_root)
    actions = ActionGateway(
        paths,
        DiagnosticRegistry.load(paths.diagnostics_config),
        pool_factory=lambda _platform: pool,
    )
    runtime = AgentRuntime(store=store, model=model, actions=actions, retry_delays=(), sleep=lambda _: None)
    return TriageWorker(store=store, orchestrator=runtime, state_dir=paths.state_dir, worker_id="test-worker"), store


def _alert_task(store):
    task, _created = store.enqueue_auto("alert@start", {
        "key": "prometheus:alpha:MemoryHigh:node1",
        "source": "prometheus",
        "platform": "alpha",
        "alertname": "MemoryHigh",
        "host": "node1",
        "service": "system",
    })
    # 生产里由服务层分类;这里直接给出分类结果
    return store.update(task.task_id, expected=task.status, task_type="alert", platform="alpha", node="node1")


def _finish(ref, match):
    return tool_reply("finish", {
        "answer": "node1 内存压力", "confidence": "high", "facts": [{"ref": ref, "match": match}],
    }, "final")


def test_route_playbook_prerun_then_one_model_call_report_and_worklog(tmp_path):
    paths = _repo(tmp_path, with_route=True)
    pool = FixturePool()
    model = ScriptedModel([_finish("playbook:1.1", "memory actual")])
    worker, store = _worker(paths, model, pool)
    task = _alert_task(store)

    assert worker.run_once() is True

    saved = store.get(task.task_id)
    assert saved.status is TaskStatus.COMPLETED
    assert model.calls == 1
    assert saved.report_path is not None
    assert list(paths.worklog_root.glob("*.md"))
    assert lint_records(paths.records_root) == []
    assert len(pool.pushes) == 2


def test_route_miss_model_investigates_with_diagnostic_then_reports(tmp_path):
    paths = _repo(tmp_path, with_route=False)
    pool = FixturePool()
    model = ScriptedModel([
        tool_reply("diagnostic", {"node": "node1", "diagnostic_id": "system.memory_summary", "params": {}},
                   "diagnostic"),
        _finish("step:1", "memory actual"),
    ])
    worker, store = _worker(paths, model, pool)
    task = _alert_task(store)

    assert worker.run_once() is True

    saved = store.get(task.task_id)
    assert saved.status is TaskStatus.COMPLETED
    assert saved.deep_dive is True and saved.report_path is not None
    assert ("node1", "free -m") in [(node, cmd) for node, cmd, _timeout, _approved in pool.calls]
    assert lint_records(paths.records_root) == []
