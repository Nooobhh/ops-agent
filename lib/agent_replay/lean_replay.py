"""A1 精简组转录 → 新运行时零成本回放(A3.3 验收)。

模型按转录原样回放当时的回复,shell / 诊断返回当时录下的输出;命令闸、三档策略、
审批挂起与兑现、records 落档、finish 核验、收口逻辑都走新运行时的真实代码。
审批由回放器自动「批准一次」,统计各类审批次数,即「人工要点几次」。

A1 的 finish 没有猜测结构,证据是原文片段字符串;回放时换成新契约的 facts(ref + match):
按片段在回放产生的 records 步骤里定位 ref,找不到的给 step:0(即摘录失败)。
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict, deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Deque, Dict, List, Tuple

from lib.agent_runtime import AgentRuntime
from lib.agent_runtime.finish import normalize
from lib.triage_agent.model_client import ModelReply, ToolCall
from lib.triage_agent.models import TaskStatus
from lib.triage_agent.targets import resolve_node, target_nodes
from lib.triage_agent.task_store import TaskStore

_FINISH_KEYS = ("answer", "confidence", "uncertain", "remediation")
_NO_RECORDING = "[回放:该命令在 A1 中没有录制输出]"


def load_transcript(path: Path) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]:
    question, assistants, results = "", [], []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record["type"] == "question":
            question = record["question"]
        elif record["type"] == "assistant":
            assistants.append(record)
        elif record["type"] == "tool_result":
            results.append(record)
    return question, assistants, results


class RecordedGateway:
    """按 (节点, 命令) 回放 A1 录下的 shell 输出;诊断按出现顺序回放。"""

    def __init__(self, repo_root: Path, work: Path, results: List[Dict[str, Any]], calls: List[Dict[str, Any]],
                 *, recall_index: Path, knowledge_root: Path):
        self.paths = SimpleNamespace(repo_root=repo_root, records_root=work / "records",
                                     recall_index_path=recall_index, knowledge_root=knowledge_root)
        self.repo_root = repo_root
        self.shell: Dict[Tuple[str, str], Deque[Dict[str, Any]]] = defaultdict(deque)
        self.diagnostics: Deque[Dict[str, Any]] = deque()
        self.catalog: List[Any] = []
        for call, result in zip(calls, results):
            payload = result.get("result") or {}
            if call["name"] == "shell":
                for item in payload.get("results") or []:
                    if "stdout" in item:
                        self.shell[(item["node"], call["arguments"].get("command", ""))].append(item)
            elif call["name"] == "diagnostic":
                self.diagnostics.append(payload)
            elif call["name"] == "diagnostic_catalog":
                self.catalog = payload.get("diagnostics") or []
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.finishes: List[Dict[str, Any]] = []
        self.missing: List[str] = []

    def known_nodes(self, platform):
        return target_nodes(self.repo_root, platform)

    def resolve_node(self, platform, value):
        return resolve_node(self.repo_root, platform, value)

    def start_session(self, *, platform, node, trigger, service="", author=None, **extra):
        run_id = f"replay-{len(self.sessions) + 1}"
        self.sessions[run_id] = {"run_id": run_id, "platform": platform, "node": node, "steps": []}
        return SimpleNamespace(run_id=run_id)

    def session_data(self, run_id):
        return self.sessions[run_id]

    def steps(self) -> List[Dict[str, Any]]:
        return [step for session in self.sessions.values() for step in session["steps"]]

    def _step(self, run_id, cmd, stdout, stderr="", exit_code=0, approved=False):
        self.sessions[run_id]["steps"].append(
            {"cmd": cmd, "exit_code": exit_code, "stdout": stdout, "stderr": stderr, "approved": approved})

    def record_write_operation(self, run_id, entry):
        self.sessions[run_id].setdefault("write_operations", []).append(dict(entry))

    def run_shell(self, run_id, *, platform, node, command, approved=False, write=True, execution_id=None):
        queue = self.shell.get((node, command))
        if queue:
            item = queue.popleft()
        else:
            self.missing.append(command[:200])
            item = {"exit_code": -1, "stdout": "", "stderr": _NO_RECORDING}
        self._step(run_id, f"[{node}] {command}", item.get("stdout") or "", item.get("stderr") or "",
                   item.get("exit_code") if item.get("exit_code") is not None else -1, approved and write)
        return {"exit_code": item.get("exit_code"), "stdout": item.get("stdout") or "",
                "stderr": item.get("stderr") or "", "timed_out": bool(item.get("timed_out")),
                "truncated": bool(item.get("truncated"))}

    def run_diagnostic(self, run_id, *, platform, node, diagnostic_id, params, execution_id=None):
        result = self.diagnostics.popleft() if self.diagnostics else {"error": _NO_RECORDING}
        execution = result.get("execution") or {}
        self._step(run_id, f"[{node}] diagnostic:{diagnostic_id}",
                   str(execution.get("stdout") or result.get("stdout") or ""),
                   str(execution.get("stderr") or result.get("stderr") or ""))
        return result

    def diagnostic_catalog(self, platform):
        return self.catalog

    def file_sizes(self, **_kwargs):
        return {}

    def record_finish(self, run_id, **fields):
        self.finishes.append(fields)
        return self.paths.records_root / run_id / "report.md"

    def record_turn_summary(self, run_id, summary):
        return self.paths.records_root / run_id / "report.md"


class ReplayModel:
    """按转录顺序给出 assistant 回复;finish 证据换成 {ref, quote}。"""

    def __init__(self, assistants: List[Dict[str, Any]], gateway: RecordedGateway):
        self.assistants = deque(assistants)
        self.gateway = gateway
        self.served = 0
        self._ids = 0

    def complete(self, messages, tools) -> ModelReply:
        if not self.assistants:
            raise RuntimeError("回放脚本用尽")
        record = self.assistants.popleft()
        self.served += 1
        calls = [self._call(item) for item in record.get("tool_calls") or []]
        message: Dict[str, Any] = {"role": "assistant", "content": record.get("content")}
        if calls:
            message["tool_calls"] = [
                {"id": call.call_id, "type": "function",
                 "function": {"name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False)}}
                for call in calls
            ]
        return ModelReply(content=record.get("content"), tool_calls=calls, assistant_message=message)

    def _call(self, item: Dict[str, Any]) -> ToolCall:
        self._ids += 1
        name, arguments = item["name"], dict(item.get("arguments") or {})
        arguments.pop("platform", None) if name in ("shell", "diagnostic", "diagnostic_catalog") else None
        if name == "finish":
            arguments = self._finish(arguments)
        return ToolCall(call_id=f"replay-{self._ids}", name=name, arguments=arguments)

    def _finish(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        steps = self.gateway.steps()
        facts = []
        for quote in arguments.get("evidence") or []:
            needle = normalize(str(quote))
            index = next((i for i, step in enumerate(steps, 1)
                          if needle and needle in normalize(f"{step['stdout']}\n{step['stderr']}")), 0)
            facts.append({"ref": f"step:{index}", "match": str(quote)})
        converted = {key: arguments[key] for key in _FINISH_KEYS if arguments.get(key) not in (None, "")}
        converted.setdefault("confidence", "medium")
        if arguments.get("root_cause"):
            converted["answer"] = f"{converted.get('answer', '')}\n根因:{arguments['root_cause']}".strip()
        converted["facts"] = facts
        return converted


def replay_case(transcript: Path, work: Path, *, repo_root: Path, recall_index: Path, knowledge_root: Path,
                command_mode: str = "whitelist", max_runs: int = 400) -> Dict[str, Any]:
    question, assistants, results = load_transcript(transcript)
    calls = [call for record in assistants for call in record.get("tool_calls") or []]
    first = next((call["arguments"] for call in calls if call["name"] in ("shell", "diagnostic")), {})
    platform = first.get("platform")
    gateway = RecordedGateway(repo_root, work, results, calls,
                              recall_index=recall_index, knowledge_root=knowledge_root)
    model = ReplayModel(assistants, gateway)
    store = TaskStore(work / "tasks")
    task = store.create_manual(question, platform=platform, scope="cluster", manual_artifacts=True,
                               command_mode=command_mode, task_type="investigation")
    approvals: Counter = Counter()
    for _ in range(max_runs):
        if store.claim_next("replay", lease_seconds=600) is None:
            break
        task = AgentRuntime(store=store, model=model, actions=gateway, retry_delays=(),
                            sleep=lambda _: None).run(task.task_id)
        if task.status is not TaskStatus.WAITING_APPROVAL:
            break
        request = [e.payload for e in store.events(task.task_id) if e.type == "approval_requested"][-1]
        approvals[request.get("kind")] += 1
        store.resolve_approval(task.task_id, request["approval_id"], decision="approve", scope="once")
    events = store.events(task.task_id)
    tool_results = [e.payload for e in events if e.type == "tool_result"]
    shell_results = [p for p in tool_results if p["name"] == "shell"]
    report = gateway.finishes[-1] if gateway.finishes else {}
    facts = [line for line in report.get("evidence") or [] if line.startswith("  [事实·")]
    return {
        "case": transcript.parent.parent.name,
        "status": task.status.value,
        "failure_kind": task.failure_kind,
        "error": task.error,
        "turns": task.tool_turns,
        "script_served": model.served,
        "script_left": len(model.assistants),
        "shell_calls": len(shell_results),
        "shell_errors": sum(1 for p in shell_results if p["result"].get("failed")),
        "node_steps": len(gateway.steps()),
        "missing_recordings": len(gateway.missing),
        "approvals": dict(approvals),
        "closing_rounds": sum(1 for e in events if e.type == "closing_round"),
        "verdict": report.get("verdict"),
        "facts": {"extracted": sum("·未摘录]" not in line for line in facts),
                  "not_found": sum("·未摘录]" in line for line in facts)},
    }
