import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib.triage_agent.model_client import ModelReply, ToolCall
from lib.triage_agent.task_store import TaskStore

REPO = Path(__file__).resolve().parents[2]


class FakeActions:
    """记录型网关:shell/诊断落 records 步骤,其余返回固定数据。"""

    def __init__(self, root: Path):
        self.paths = SimpleNamespace(
            repo_root=REPO, records_root=root / "records",
            recall_index_path=root / "kb-recall.sqlite", knowledge_root=root / "knowledge",
        )
        self.sessions = {}
        self.outputs = {}
        self.sizes = {}
        self.shell_calls = []
        self.finishes = []

    def known_nodes(self, platform):
        return ["node1", "node2"]

    def resolve_node(self, platform, value):
        return value if value in self.known_nodes(platform) else None

    def start_session(self, *, platform, node, trigger, service="", author=None, **extra):
        run_id = f"run-{len(self.sessions) + 1}"
        self.sessions[run_id] = {"run_id": run_id, "platform": platform, "node": node, "steps": []}
        return SimpleNamespace(run_id=run_id)

    def session_data(self, run_id):
        return self.sessions[run_id]

    def run_shell(self, run_id, *, platform, node, command, approved=False, write=True, execution_id=None):
        self.shell_calls.append((node, command, approved))
        stdout = self.outputs.get(command, f"{node} ok {command}\n")
        self.sessions[run_id]["steps"].append({
            "cmd": f"[{node}] {command}", "exit_code": 0, "stdout": stdout, "stderr": "",
            "approved": approved and write,
        })
        return {"exit_code": 0, "stdout": stdout, "stderr": "", "timed_out": False, "truncated": False}

    def file_sizes(self, *, platform, node, paths, execution_id):
        return {path: self.sizes[path] for path in paths if path in self.sizes}

    def record_finish(self, run_id, **fields):
        self.finishes.append(fields)
        report = self.paths.records_root / run_id / "report.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(fields, ensure_ascii=False), encoding="utf-8")
        return report

    def record_write_operation(self, run_id, entry):
        operations = [item for item in self.sessions[run_id].get("write_operations") or []
                      if item.get("id") != entry.get("id")]
        self.sessions[run_id]["write_operations"] = [*operations, dict(entry)]
        return self.paths.records_root / run_id / "report.md"

    def record_turn_summary(self, run_id, summary):
        return self.paths.records_root / run_id / "report.md"

    def latest_resource_snapshot(self, platform, node=None):
        return {"available": True, "platform": platform, "node": node, "collected_at": "2026-10-22T10:00:00+08:00"}

    def diagnostic_catalog(self, platform):
        return [{"id": "os.load"}]

    # ---- 类型材料用的替身 ----
    context_pack = None
    route = None
    playbook_results = {}

    def build_context(self, request):
        self.context_requests = [*getattr(self, "context_requests", []), request]
        return self.context_pack or {}

    def lookup_route(self, alertname, platform):
        return self.route

    def run_registered_playbook(self, *, run_id, platform, node, playbook, execution_id=None):
        result = {"playbook": playbook, **self.playbook_results[playbook]}
        self.sessions[run_id].setdefault("playbook_results", []).append(result)
        self.playbooks_run = [*getattr(self, "playbooks_run", []), playbook]
        return result

    def start_workspace(self, *, task_id, trigger, author=None):
        run_id = f"op-{task_id}"
        self.sessions[run_id] = {"run_id": run_id, "platform": "local", "node": "local", "steps": []}
        return SimpleNamespace(run_id=run_id)

    def create_task_artifact(self, run_id, *, filename, content, purpose):
        return {"path": f"records/troubleshoot/{run_id}/artifacts/{filename}", "purpose": purpose}

    def worklog_commit(self, run_id, *, kb_doc=None, author=None):
        self.worklogs = [*getattr(self, "worklogs", []), run_id]


class ScriptedModel:
    """按顺序返回预置回复;记录每次请求的 messages 与 tools。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append((json.loads(json.dumps(messages)), [t["function"]["name"] for t in tools]))
        if not self.replies:
            raise AssertionError("脚本回复用尽")
        return self.replies.pop(0)


def call(name, arguments=None, call_id=None, error=None):
    call_id = call_id or f"c-{name}-{id(arguments) % 10000}"
    return ToolCall(call_id=call_id, name=name, arguments=arguments or {}, arguments_error=error)


def reply(*calls, content=None, usage=None):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [
            {"id": item.call_id, "type": "function",
             "function": {"name": item.name, "arguments": json.dumps(item.arguments, ensure_ascii=False)}}
            for item in calls
        ]
    return ModelReply(content=content, tool_calls=list(calls), assistant_message=message, usage=usage)


@pytest.fixture
def env(tmp_path):
    store = TaskStore(tmp_path / "tasks")
    actions = FakeActions(tmp_path)
    return SimpleNamespace(store=store, actions=actions, tmp=tmp_path)


def new_task(store, **fields):
    store.create_manual(
        fields.pop("message", "node1 内存为什么高?"),
        platform="alpha", node="node1", manual_artifacts=True,
        task_type=fields.pop("task_type", "investigation"), **fields,
    )
    return store.claim_next("w1", lease_seconds=60)
