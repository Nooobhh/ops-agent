"""自主 / 引导档运行时(A2 §5):模型 → 工具批 → 结果 → 模型,finish 收口。

模型负责规划、顺序和证据推理;本循环只守边界(命令闸、预算、审批)、落档与恢复。
循环状态全部在 TaskStore 事件里,审批挂起后 worker 重启可从事件流接着跑。
"""
from __future__ import annotations

import re
import secrets
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import ValidationError

from lib.command_guard import HEAVY_FILE_BYTES, HEAVY_FILE_HINT, CommandGuard, decide, full_scan_reads
from lib.kb.bases import resolve_doc_id
from lib.kb.browse import browse_search
from lib.triage_agent.budget_gate import consume_budget_decision, request_budget_gate
from lib.triage_agent.command_approvals import CommandApprovals
from lib.triage_agent.command_policy import CommandPolicyError, PolicyConfig
from lib.triage_agent.diagnostics import DiagnosticDenied
from lib.triage_agent.errors import (
    ActionDenied,
    ApprovalPending,
    BudgetExhausted,
    KbProposalRejected,
    KnowledgeFailure,
    OrchestrationError,
)
from lib.triage_agent.invocations import ToolInvocations
from lib.triage_agent.kb_review import KbReview
from lib.triage_agent.model_client import ModelReply, ModelTransientError, ToolCall
from lib.triage_agent.models import TaskSource, TaskStatus, TriageTask
from lib.triage_agent.remote_session import RemoteExecutionError
from lib.triage_agent.report_gate import pending_report_gate, request_report_gate
from lib.triage_agent.sessions import ensure_session, sync_live_report
from lib.triage_agent.targets import target_keys
from lib.record import OplogMissing
from lib.triage_agent.tool_batch import run_tool_batch
from lib.triage_agent.tool_registry import ToolRegistry
from lib.triage_agent.tools import validate_tool_call
from lib.triage_agent.worklog_flow import WorklogFlow
from lib.triage_agent.write_operations import WriteOperations
from lib.plugins.exposure import apply_scope, check_arguments

from .finish import FINISH_SCHEMA, STATUS_LABELS, FinishArgs, evidence_sources, report_fields, step_index, verify
from .messages import build_messages, opening_notice, system_prompt
from .openers import build_materials
from .tiers import (
    HARD_BUDGET,
    HARD_INPUT_TOKENS,
    INVALID_ARGUMENT_LIMIT,
    AUTONOMOUS,
    Budget,
    soft_budget,
    switches_from,
)
from .tools import EVIDENCE_TOOLS, KB_DRAFT_SCHEMA, PLUGIN_PREFIX, plugin_tools, project, task_tool_schemas

# 右侧「生成知识草稿」入口发出的固定消息(前端 Triage.tsx);这一轮只开放 create_kb_draft
KB_DRAFT_TRIGGER = "请基于当前执行报告生成知识库草稿，等待我确认后再落档。"
KB_GREP_LIMIT = 200
_QUESTION_END = ("?", "？")


class AgentRuntime:
    def __init__(
        self,
        *,
        store,
        model,
        actions,
        knowledge_sink=None,
        command_policy=None,
        command_reviewer=None,
        guard: Optional[CommandGuard] = None,
        retry_delays: Tuple[float, ...] = (5.0, 15.0),
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.store = store
        self.model = model
        self.actions = actions
        self.knowledge_sink = knowledge_sink
        self.command_policy = command_policy
        self.command_reviewer = command_reviewer
        self.guard = guard or CommandGuard()
        self.retry_delays = tuple(retry_delays)
        self.sleep = sleep
        self.clock = clock
        self.repo_root = Path(actions.paths.repo_root)
        self.invocations = ToolInvocations(store, ToolRegistry())
        self.write_ops = WriteOperations(
            store, self.invocations, actions, knowledge_sink=knowledge_sink, notice=self._notice,
            sync_report=lambda task: sync_live_report(self.store, self.actions, task, self._display_path),
            steps=self._steps,
        )
        self._round_end: Optional[TriageTask] = None

    # ---- 入口 ----

    def run(self, task_id: str) -> TriageTask:
        execution_id: Optional[str] = None
        try:
            task = self.store.get(task_id)
            if task.status is not TaskStatus.RUNNING:
                raise OrchestrationError("任务必须先由 worker claim")
            execution_id = task.execution_id or task.run_id
            for consume in (
                # 写操作最先:跟踪中 / 待对账的写动作必须先于一切收尾与续跑
                self.write_ops.consume,
                self._consume_worklog,
                self._consume_budget,
                self._consume_kb,
                self._consume_report_gate,
                self._consume_command,
            ):
                settled = consume(self.store.get(task_id))
                if settled is not None:
                    return settled
            return self._loop(self._prepare(self.store.get(task_id)))
        except KnowledgeFailure as exc:
            return self._fail(task_id, str(exc), TaskStatus.KB_FAILED, "knowledge")
        except Exception as exc:  # noqa: BLE001 — 落 failed,人工任务可重试
            return self._fail(task_id, str(exc), TaskStatus.FAILED, getattr(exc, "failure_kind", None))
        finally:
            if execution_id:
                self._close_terminal_session(task_id, execution_id)

    def _prepare(self, task: TriageTask) -> TriageTask:
        """首次执行:固化档位开关与插件工具,落任务开场(只追加,保证前缀稳定)。"""
        if task.harness_switches is None:
            switches = AUTONOMOUS
            task = self.store.update(
                task.task_id, expected=TaskStatus.RUNNING, harness_switches=switches.snapshot(),
                plugin_tools=self._plugin_snapshot(task),
            )
        if not any(event.type == "agent_opening" for event in self.store.events(task.task_id)):
            # 材料先备齐再落开场事件:中途崩溃时重跑,已完成的 playbook 不重放
            task, materials = build_materials(self, task)
            materials = [*materials, *self._plugin_notices(task)]
            self.store.append_event(task.task_id, "agent_opening", {
                "tier": task.harness_tier, "task_type": task.task_type, "materials": len(materials),
            })
            self._notice(task, opening_notice(task, materials))
        return task

    # ---- 主循环 ----

    def _loop(self, task: TriageTask) -> TriageTask:
        switches = switches_from(task.harness_switches)
        system = system_prompt(task, self.repo_root)
        regular_tools = task_tool_schemas(task)
        started, base_seconds = self.clock(), task.tool_runtime_seconds
        invalid_streak = 0
        while True:
            task = self.store.get(task.task_id)
            events = self.store.events(task.task_id)
            closing = self._closing_active(events)
            if closing and self._closing_turn_used(events):
                return self._unanswered(task, "收口回合未调用 finish")
            if not closing:
                elapsed = base_seconds + self.clock() - started
                hard = self._exceeded(task, HARD_BUDGET, elapsed, events, tokens=True)
                soft = self._exceeded(task, soft_budget(switches, task.budget_grants), elapsed, events)
                if hard:
                    self._enter_closing(task, f"{hard[1]}达到硬上限")
                    continue
                if soft:
                    kind, label = soft
                    budget = soft_budget(switches, task.budget_grants)
                    if task.source is TaskSource.MANUAL:
                        return request_budget_gate(
                            self.store, task, f"{label}达到本档软预算", kind,
                            turn_budget=budget.turns, diagnostic_budget=budget.node_commands,
                        )
                    self._enter_closing(task, f"{label}达到本档软预算")
                    continue
            kb_turn = not closing and self._latest_user_message(events) == KB_DRAFT_TRIGGER
            tools = [FINISH_SCHEMA] if closing else [KB_DRAFT_SCHEMA] if kb_turn else regular_tools
            allowed = {schema["function"]["name"] for schema in tools}
            reply = self._complete(task, build_messages(system, events), tools)
            task = self.store.update(task.task_id, expected=TaskStatus.RUNNING, tool_turns=task.tool_turns + 1)
            self.store.append_event(task.task_id, "assistant_message", {"message": reply.assistant_message})
            if not reply.tool_calls:
                if closing:
                    return self._unanswered(task, "收口回合未调用 finish")
                return self._text_reply(task, reply)
            invalid_streak = invalid_streak + 1 if any(
                call.arguments_error or call.name not in allowed for call in reply.tool_calls
            ) else 0
            self._round_end = None
            batch_id = f"batch-{secrets.token_hex(8)}"
            outcome = run_tool_batch(
                task,
                reply.tool_calls,
                batch_id=batch_id,
                handle=lambda task, call, **fields: self._handle(task, call, switches, allowed, **fields),
                skip=self.invocations.skip,
                refresh=self.store.get,
                is_recoverable=_is_recoverable,
                continue_after_error=lambda name, exc: not isinstance(exc, RemoteExecutionError),
                is_dependent=lambda name: name == "finish",
            )
            if outcome.budget_exhausted is not None:
                self._enter_closing(outcome.task, str(outcome.budget_exhausted))
                continue
            if self._round_end is not None:
                return self._finish_round(self._round_end)
            if outcome.terminal is not None:
                return outcome.terminal
            if invalid_streak >= INVALID_ARGUMENT_LIMIT and not closing:
                self._enter_closing(outcome.task, f"连续 {invalid_streak} 轮工具参数不合法")

    def _complete(self, task: TriageTask, messages, tools) -> ModelReply:
        attempts = len(self.retry_delays) + 1
        for attempt in range(1, attempts + 1):
            try:
                reply = self.model.complete(messages, tools)
                break
            except ModelTransientError as exc:
                if attempt == attempts:
                    error = ModelTransientError(f"模型服务连续 {attempts} 次请求失败({exc}),可点击重试继续排查")
                    error.failure_kind = "model_unavailable"
                    raise error from exc
                delay = self.retry_delays[attempt - 1]
                self.store.append_event(task.task_id, "model_retry", {
                    "attempt": attempt + 1, "max_attempts": attempts, "wait_seconds": delay, "error": str(exc),
                })
                self.sleep(delay)
        if reply.usage:
            self.store.append_event(task.task_id, "model_usage", _usage(reply.usage))
        return reply

    # ---- 预算与收口回合 ----

    def _exceeded(self, task, budget: Budget, elapsed: float, events, *, tokens: bool = False):
        if task.tool_turns >= budget.turns:
            return "turn_cap", "模型轮次"
        if elapsed >= budget.seconds:
            return "time_limit", "运行时长"
        if task.diagnostic_calls >= budget.node_commands:
            return "diagnostic_cap", "节点命令数"
        if tokens and _input_tokens(events) >= HARD_INPUT_TOKENS:
            return "turn_cap", "输入 token"
        return None

    def _enter_closing(self, task: TriageTask, reason: str) -> None:
        self.store.append_event(task.task_id, "closing_round", {"reason": reason})
        self._notice(task, (
            f"[收口回合] {reason}。请基于已采到的证据调用 finish 收口,"
            "未完成的排查写进 uncertain。本轮只开放 finish。"
        ))

    @staticmethod
    def _closing_active(events) -> bool:
        for event in reversed(events):
            if event.type == "closing_round":
                return True
            if event.type == "user_message" or (
                event.type == "status" and event.payload.get("status") == "waiting_input"
            ):
                return False
        return False

    @staticmethod
    def _closing_turn_used(events) -> bool:
        index = max(i for i, event in enumerate(events) if event.type == "closing_round")
        return any(event.type == "assistant_message" for event in events[index + 1:])

    def _unanswered(self, task: TriageTask, reason: str) -> TriageTask:
        """收口回合仍不收口:用已采步骤生成「未收口」报告,按 insufficient 落档。"""
        steps = self._steps(task)
        if task.run_id:
            report = self.actions.record_finish(
                task.run_id,
                verdict="insufficient",
                severity="ok",
                root_cause=f"未收口:{reason}",
                evidence=[f"[已采·step:{index}] {step.get('cmd', '')}" for index, step in enumerate(steps, 1)],
                remediation=[],
                finish={"answer": "模型在收口回合未给出结论;以下是已采到的操作记录,需人工判断。",
                        "confidence": "low", "window": None, "uncertain": [reason], "hypotheses": []},
            )
            task = self.store.update(task.task_id, expected=TaskStatus.RUNNING,
                                     report_path=self._display_path(report))
        self.store.append_event(task.task_id, "closing_round_unanswered", {"reason": reason})
        task = self.store.update(task.task_id, expected=TaskStatus.RUNNING,
                                 failure_kind="closing_round_unanswered")
        return self._finish_round(task)

    # ---- 无工具回复与回合结束 ----

    def _text_reply(self, task: TriageTask, reply: ModelReply) -> TriageTask:
        content = (reply.content or "").strip()
        needs_input = content.endswith(_QUESTION_END)
        if needs_input and task.source is TaskSource.AUTO:
            # 自动任务无人回答:不等待,转收口回合用已有证据作答
            self._enter_closing(task, "自动任务不能等待用户补充信息")
            return self._loop(self.store.get(task.task_id))
        self.store.append_event(task.task_id, "agent_decision", {
            "action": "needs_input" if needs_input else "answer", "answer": content,
        })
        if task.run_id and content:
            report = self.actions.record_turn_summary(task.run_id, content)
            task = self.store.update(task.task_id, expected=TaskStatus.RUNNING,
                                     report_path=self._display_path(report))
        if needs_input:
            # 澄清往返把控制权交还用户,不占轮次预算
            return self.store.transition(
                task.task_id, TaskStatus.RUNNING, TaskStatus.WAITING_INPUT,
                lease_owner=None, lease_expires_at=None, tool_turns=max(0, task.tool_turns - 1),
            )
        return self._finish_round(task)

    def _finish_round(self, task: TriageTask) -> TriageTask:
        """一轮结束:新版 Web 人工任务停 waiting_input 等用户点完成;旧版人工任务走 worklog 闸。"""
        task = self.store.get(task.task_id)
        if task.status is not TaskStatus.RUNNING:
            return task
        if task.source is TaskSource.MANUAL and not task.manual_artifacts and task.run_id:
            waiting = WorklogFlow(self.store, self.actions, self.model).request_gate(task)
            if waiting is not None:
                return waiting
        target = TaskStatus.WAITING_INPUT if task.source is TaskSource.MANUAL and task.manual_artifacts \
            else TaskStatus.COMPLETED
        return self.store.transition(task.task_id, TaskStatus.RUNNING, target,
                                     lease_owner=None, lease_expires_at=None)

    # ---- 单个工具调用 ----

    def _handle(self, task, call: ToolCall, switches: TierSwitches, allowed, *, batch_id, batch_index, batch_size):
        batch_fields = {"batch_id": batch_id, "batch_index": batch_index, "batch_size": batch_size,
                        "batch_policy": "dependency_aware_sequential"}
        self.store.append_event(task.task_id, "tool_call", {
            "tool_call_id": call.call_id, "name": call.name, "arguments": call.arguments, **batch_fields,
        })
        if self.store.invocation_completed(task.task_id, call.call_id):
            result = self.store.invocation_result(task.task_id, call.call_id) or {}
            self._record(task, call.call_id, call.name, {**result, "replayed": True}, self.clock(),
                         batch_fields, switches)
            return self.store.get(task.task_id), False
        self.store.record_invocation(task.task_id, call.call_id, "pending")
        started = self.clock()
        try:
            if call.arguments_error:
                raise OrchestrationError(f"工具参数不合法,请修正后重试: {call.arguments_error}")
            if call.name not in allowed:
                raise OrchestrationError(f"本轮不开放工具 {call.name},可用: {', '.join(sorted(allowed))}")
            handler = self._tool_plugin if call.name.startswith(PLUGIN_PREFIX) \
                else getattr(self, f"_tool_{call.name}")
            task, result, terminal = handler(task, call)
        except ApprovalPending:
            self.invocations.record_usage(task.task_id, call.name, started, {})
            return self.store.get(task.task_id), True
        except Exception as exc:
            self.invocations.record_usage(task.task_id, call.name, started, {})
            self.invocations.record_failure(task.task_id, call.call_id, call.name, exc,
                                            batch_id=batch_id, batch_index=batch_index)
            raise
        self._record(task, call.call_id, call.name, result, started, batch_fields, switches)
        return self.store.get(task.task_id), terminal

    def _record(self, task, call_id: str, name: str, result: Dict[str, Any], started: float,
                batch_fields: Dict[str, Any], switches: TierSwitches) -> None:
        plugin = name.startswith(PLUGIN_PREFIX)
        evidence = name in EVIDENCE_TOOLS or plugin
        metadata = SimpleNamespace(
            version="2.0",
            target_kind="plugin" if plugin else "registered_node" if evidence else "none",
            evidence_policy="runtime_output" if evidence else "none",
        )
        self.invocations.record_success(
            task.task_id, call_id, name, result, started, batch_fields,
            context={"task_id": task.task_id, "platform": task.platform, "node": task.node,
                     "harness_tier": task.harness_tier},
            metadata=metadata,
            model_result=project(result, switches.projection_chars),
        )

    # ---- 节点命令 ----

    def _tool_shell(self, task: TriageTask, call: ToolCall):
        nodes, command = call.arguments.get("nodes"), call.arguments.get("command")
        if not isinstance(nodes, list) or not nodes or not isinstance(command, str) or not command.strip():
            raise OrchestrationError("shell 需要 nodes(非空列表)与 command(非空字符串)")
        platform, nodes = self._resolve_nodes(task, [str(item) for item in nodes])
        if task.diagnostic_calls + len(nodes) > HARD_BUDGET.node_commands:
            raise BudgetExhausted("节点命令总数达到硬上限", "diagnostic_cap", code="NODE_COMMAND_HARD_CAP")
        verdict = self.guard.check(command)
        config = self._policy_config()
        decision = decide(
            verdict, task.command_mode or config.mode,
            allow=config.allow, deny=config.deny, task_defaults=task.command_defaults,
        )
        CommandApprovals(self.store, self.invocations, policy=self.command_policy,
                         reviewer=self.command_reviewer).resolve(
            task, decision,
            call_id=call.call_id, command=command,
            signature="\n".join(verdict.unregistered or verdict.signatures),
            node=",".join(nodes), targets=nodes,
            deny_guidance=f"。{verdict.hint}" if verdict.hint else "",
        )
        self._check_heavy_reads(task, platform, nodes, command)
        return self._run_shell(task, platform, nodes, command, approved=False)

    def _check_heavy_reads(self, task, platform: str, nodes: List[str], command: str) -> None:
        paths = full_scan_reads(command)
        if not paths:
            return
        for node in nodes:
            try:
                sizes = self.actions.file_sizes(platform=platform, node=node, paths=paths,
                                                execution_id=self._execution_id(task))
            except Exception:  # noqa: BLE001 — 探测失败不挡命令,超时与 1MB 上限仍兜底
                continue
            heavy = {path: size for path, size in sizes.items() if size >= HEAVY_FILE_BYTES}
            if heavy:
                detail = ", ".join(f"{path}({size / 1024 ** 3:.1f}GB)" for path, size in heavy.items())
                raise ActionDenied(f"{node} 上整份读取大文件:{detail}。{HEAVY_FILE_HINT}")

    def _run_shell(self, task: TriageTask, platform: str, nodes: List[str], command: str, *, approved: bool,
                   write: bool = False):
        task = ensure_session(self.store, self.actions, task, node=nodes[0])
        results = []
        for node in nodes:
            try:
                output = self.actions.run_shell(
                    task.run_id, platform=platform, node=node, command=command,
                    approved=approved, write=write, execution_id=self._execution_id(task),
                )
            except (RemoteExecutionError, ActionDenied) as exc:
                # 单节点失败不影响其他节点,结果里显式标注
                results.append({"node": node, "error": str(exc)[:500]})
                continue
            results.append({
                "node": node,
                "ref": f"step:{len(self._steps(task))}",
                "exit_code": output.get("exit_code"),
                "stdout": output.get("stdout"),
                "stderr": output.get("stderr"),
                "timed_out": output.get("timed_out"),
                "truncated": output.get("truncated"),
            })
        task = self.store.update(task.task_id, expected=TaskStatus.RUNNING, deep_dive=True,
                                 diagnostic_calls=task.diagnostic_calls + len(nodes))
        task = sync_live_report(self.store, self.actions, task, self._display_path)
        return task, {"platform": platform, "command": command, "results": results}, False

    def _execute_approved_shell(self, task: TriageTask, call_id: str, signature: str) -> None:
        """批准即由 Harness 执行挂起的那条原调用;命令须与批准时的签名一致。"""
        call_event = next(
            (event.payload for event in self.store.events(task.task_id)
             if event.type == "tool_call" and str(event.payload.get("tool_call_id") or "") == call_id),
            {},
        )
        batch_fields = {key: call_event[key] for key in ("batch_id", "batch_index", "batch_size", "batch_policy")
                        if key in call_event}
        switches = switches_from(task.harness_switches)
        started = self.clock()
        try:
            arguments = call_event.get("arguments") or {}
            command = str(arguments.get("command") or "")
            verdict = self.guard.check(command)
            if "\n".join(verdict.unregistered or verdict.signatures) != signature:
                raise OrchestrationError("待执行命令与批准时的签名不一致,未执行")
            platform, nodes = self._resolve_nodes(task, [str(item) for item in arguments.get("nodes") or []])
            # 只有红线写命令算写操作(要 oplog);批准的未登记只读、凭据读取不算
            write = verdict.category == "write"
            task, result, _terminal = self._run_shell(task, platform, nodes, command, approved=True, write=write)
            if write:
                try:
                    self.write_ops.record_shell_write(task, call_id=call_id, command=command,
                                                      results=result["results"])
                except Exception as exc:  # noqa: BLE001 — 命令已执行,落档失败不改其结果;收口时 oplog 闸兜底
                    self._notice(task, f"【写操作】命令已执行,但写操作记录落档失败({exc}),收口前须补 oplog")
        except Exception as exc:
            self.invocations.record_failure(task.task_id, call_id, "shell", exc,
                                            batch_id=batch_fields.get("batch_id"),
                                            batch_index=batch_fields.get("batch_index"))
            if not _is_recoverable(exc):
                raise
            return
        self._record(task, call_id, "shell", result, started, batch_fields, switches)

    def _tool_diagnostic_catalog(self, task: TriageTask, call: ToolCall):
        platform = self._platform(task)
        return task, {"platform": platform, "diagnostics": self.actions.diagnostic_catalog(platform)}, False

    def _tool_diagnostic(self, task: TriageTask, call: ToolCall):
        diagnostic_id = call.arguments.get("diagnostic_id")
        if not isinstance(diagnostic_id, str) or not diagnostic_id:
            raise OrchestrationError("diagnostic 需要 diagnostic_id")
        params = call.arguments.get("params") or {}
        if not isinstance(params, dict):
            raise OrchestrationError("params 必须是对象")
        platform, nodes = self._resolve_nodes(task, [str(call.arguments.get("node") or task.node or "")])
        if task.diagnostic_calls + 1 > HARD_BUDGET.node_commands:
            raise BudgetExhausted("节点命令总数达到硬上限", "diagnostic_cap", code="NODE_COMMAND_HARD_CAP")
        task = ensure_session(self.store, self.actions, task, node=nodes[0])
        before = len(self._steps(task))
        result = self.actions.run_diagnostic(
            task.run_id, platform=platform, node=nodes[0], diagnostic_id=diagnostic_id,
            params=params, execution_id=self._execution_id(task),
        )
        after = len(self._steps(task))
        task = self.store.update(task.task_id, expected=TaskStatus.RUNNING, deep_dive=True,
                                 diagnostic_calls=task.diagnostic_calls + 1)
        task = sync_live_report(self.store, self.actions, task, self._display_path)
        return task, {"ref": f"step:{after}" if after > before else None, **result}, False

    # ---- 插件(MCP)工具:外部系统的实时只读查询,结果落 records 步骤 ----

    def _tool_plugin(self, task: TriageTask, call: ToolCall):
        spec = next(item for item in plugin_tools(task) if item["name"] == call.name)
        if spec.get("role") == "write_plan" and task.source is not TaskSource.MANUAL:
            raise ActionDenied("自动任务没有人工审批通道,写操作一律驳回")
        problem = check_arguments(spec["schema"]["function"]["parameters"], call.arguments)
        if problem:
            raise OrchestrationError(f"{call.name} 参数不合法: {problem}")
        platform = self._platform(task)
        if spec.get("cluster") and spec["cluster"] != platform:
            raise OrchestrationError(
                f"插件 {spec['plugin']} 属于集群 {spec['cluster']},本任务是 {platform};一个任务只对应一个集群"
            )
        # 全局插件:受约束工具补上 / 校验本集群过滤,实际参数进 records 步骤
        arguments, problem = apply_scope(spec, call.arguments, platform)
        if problem:
            raise OrchestrationError(problem)
        if task.diagnostic_calls + 1 > HARD_BUDGET.node_commands:
            raise BudgetExhausted("节点命令总数达到硬上限", "diagnostic_cap", code="NODE_COMMAND_HARD_CAP")
        task = ensure_session(self.store, self.actions, task,
                              node=task.node or (self.actions.known_nodes(platform) or [None])[0])
        before = len(self._steps(task))
        result = self.actions.run_plugin_tool(
            task.run_id, plugin_id=spec["plugin"], tool=spec["tool"], arguments=arguments,
            content_hash=spec["hash"],
        )
        after = len(self._steps(task))
        task = self.store.update(task.task_id, expected=TaskStatus.RUNNING, deep_dive=True,
                                 diagnostic_calls=task.diagnostic_calls + 1)
        task = sync_live_report(self.store, self.actions, task, self._display_path)
        ref = f"step:{after}" if after > before else None
        if spec.get("role") == "write_plan":
            # 计划即提出写操作:正常计划挂人工审批(ApprovalPending),失败 / 无需执行原样回模型
            return task, self.write_ops.propose(task, call, spec, result, ref), False
        return task, {"ref": ref, **result}, False

    def _plugin_snapshot(self, task: TriageTask) -> Optional[Dict[str, Any]]:
        snapshot = getattr(self.actions, "plugin_snapshot", None)
        if not callable(snapshot):
            return None
        try:
            # 写操作计划工具只给有人工审批通道的任务
            return snapshot(task.platform, writable=task.source is TaskSource.MANUAL)
        except Exception as exc:  # noqa: BLE001 — 插件配置坏了不挡任务,开场显式告知
            return {"version": 1, "plugins": [], "tools": [], "notices": [f"插件配置读取失败({exc}),本任务无插件工具"]}

    def _plugin_notices(self, task: TriageTask) -> List[str]:
        """开场写明插件状态:启用但没有工具清单的、当前探测不可用的,不让模型以为一切正常。"""
        snapshot = task.plugin_tools or {}
        notices = list(snapshot.get("notices") or [])
        health = getattr(self.actions, "plugin_health", None)
        for item in snapshot.get("plugins") or []:
            state = health(item["id"]) if callable(health) else {"state": "ok"}
            if state.get("state") != "ok":
                notices.append(f"插件 {item['title']} 当前不可用({state.get('detail') or state.get('state')}),"
                               "调用其工具会失败")
        return [f"【插件】{text}" for text in notices]

    # ---- 知识与本地数据(参考,不是实时证据) ----

    def _tool_kb_search(self, task: TriageTask, call: ToolCall):
        query = str(call.arguments.get("query") or "").strip()
        if not query:
            raise OrchestrationError("kb_search 需要 query")
        limit = max(1, min(50, int(call.arguments.get("limit") or 20)))
        hits = browse_search(
            self.actions.paths.recall_index_path, query,
            platform=call.arguments.get("platform"), service=call.arguments.get("service"), limit=limit,
        )
        return task, {"hits": [
            {"path": hit.path, "title": hit.display_title, "type": hit.doc_type,
             "status": hit.status, "snippet": hit.snippet}
            for hit in hits
        ]}, False

    def _tool_kb_grep(self, task: TriageTask, call: ToolCall):
        try:
            regex = re.compile(str(call.arguments.get("pattern") or ""), re.IGNORECASE)
        except re.error as exc:
            raise OrchestrationError(f"正则不合法: {exc}") from exc
        root = Path(self.actions.paths.knowledge_root)
        lines: List[str] = []
        for path in sorted(root.glob(str(call.arguments.get("path_glob") or "**/*.md"))):
            if not path.is_file() or path.suffix != ".md" or not path.resolve().is_relative_to(root.resolve()):
                continue
            relative = path.relative_to(root).as_posix()
            for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if regex.search(line):
                    lines.append(f"{relative}:{number}: {line.strip()[:300]}")
                    if len(lines) >= KB_GREP_LIMIT:
                        return task, {"matches": lines, "truncated": True}, False
        return task, {"matches": lines, "truncated": False}, False

    def _tool_kb_read(self, task: TriageTask, call: ToolCall):
        root = Path(self.actions.paths.knowledge_root).resolve()
        doc = resolve_doc_id(root, str(call.arguments.get("doc") or "").removeprefix("knowledge/"))
        path = (root / doc).resolve()
        if not path.is_relative_to(root) or path.suffix != ".md" or not path.is_file():
            raise OrchestrationError(f"知识文档不存在: {doc}")
        if doc not in task.consulted_docs:
            task = self.store.update(task.task_id, expected=TaskStatus.RUNNING,
                                     consulted_docs=[*task.consulted_docs, doc])
        return task, {"doc": doc, "content": path.read_text(encoding="utf-8")}, False

    def _tool_read_task_evidence(self, task: TriageTask, call: ToolCall):
        refs = call.arguments.get("refs")
        if not isinstance(refs, list) or not refs:
            raise OrchestrationError("read_task_evidence 需要 refs")
        budget = max(1000, min(200000, int(call.arguments.get("max_chars") or 20000)))
        steps, items = self._steps(task), []
        for ref in refs[:20]:
            index = step_index(str(ref))
            if index is None or not 1 <= index <= len(steps):
                items.append({"ref": ref, "error": "本任务没有这个步骤"})
                continue
            step = steps[index - 1]
            share = budget // max(1, len(refs))
            items.append({"ref": f"step:{index}", "cmd": step.get("cmd"), "exit_code": step.get("exit_code"),
                          "stdout": str(step.get("stdout") or "")[:share],
                          "stderr": str(step.get("stderr") or "")[:2000]})
        return task, {"items": items}, False

    def _tool_read_resource_snapshot(self, task: TriageTask, call: ToolCall):
        platform = self._platform(task)
        node = call.arguments.get("node") or task.node
        return task, {"kind": "resource_polling_snapshot",
                      **self.actions.latest_resource_snapshot(platform, node)}, False

    def _tool_read_resource_risk(self, task: TriageTask, call: ToolCall):
        risk_ref = str(call.arguments.get("risk_ref") or "")
        if task.risk_seed and risk_ref != task.risk_seed.get("risk_ref"):
            raise OrchestrationError("资源风险引用不是当前任务绑定的风险")
        if not task.platform or not task.node:
            raise OrchestrationError("读取资源风险前需要明确集群和节点")
        return task, self.actions.read_resource_risk(
            risk_ref=risk_ref, platform=task.platform, node=task.node,
            window_hours=int(call.arguments.get("window_hours") or 24),
            max_samples=int(call.arguments.get("max_samples") or 120),
        ), False

    def _tool_read_task_dependencies(self, task: TriageTask, call: ToolCall):
        process_id = call.arguments.get("process_id")
        if not isinstance(process_id, int):
            raise OrchestrationError("read_task_dependencies 需要整数 process_id")
        reader = getattr(self.actions, "task_dependencies", None)
        if not callable(reader):
            raise OrchestrationError("当前环境不支持任务依赖查询")
        result = reader(process_id=process_id, task_id=call.arguments.get("task_id"))
        return task, {"kind": "task_dependencies", **result}, False

    def _tool_lookup_alert_route(self, task: TriageTask, call: ToolCall):
        alertname = str(call.arguments.get("alertname") or "")
        if not alertname:
            raise OrchestrationError("lookup_alert_route 需要 alertname")
        return task, {"hit": self.actions.lookup_route(alertname, task.platform)}, False

    def _tool_set_task_context(self, task: TriageTask, call: ToolCall):
        platform = str(call.arguments.get("platform") or "")
        if task.platform:
            raise OrchestrationError(f"任务已绑定集群 {task.platform};一个任务只对应一个集群")
        known = target_keys(self.repo_root)
        if platform not in known:
            raise OrchestrationError(f"集群须取: {', '.join(known)}")
        task = self.store.update(task.task_id, expected=TaskStatus.RUNNING, platform=platform)
        return task, {"platform": platform, "nodes": self.actions.known_nodes(platform)}, False

    def _tool_create_task_artifact(self, task: TriageTask, call: ToolCall):
        args = validate_tool_call("create_task_artifact", call.arguments)
        if task.source is not TaskSource.MANUAL:
            raise OrchestrationError("自动任务不能生成任务产物")
        if not task.run_id:
            session = self.actions.start_workspace(
                task_id=task.task_id, trigger=task.initial_message, author=task.created_by,
            )
            task = self.store.update(task.task_id, expected=TaskStatus.RUNNING, run_id=session.run_id)
        result = self.actions.create_task_artifact(
            task.run_id, filename=args.filename, content=args.content, purpose=args.purpose,
        )
        path = result.get("path") if isinstance(result, dict) else None
        if path and path not in task.artifact_refs:
            task = self.store.update(task.task_id, expected=TaskStatus.RUNNING,
                                     artifact_refs=[*task.artifact_refs, path])
        return task, result, False

    def _tool_create_kb_draft(self, task: TriageTask, call: ToolCall):
        args = validate_tool_call("create_kb_draft", call.arguments)
        if not task.report_path or not task.run_id:
            raise OrchestrationError("只有存在服务器执行报告的任务可以写知识库")
        if self.knowledge_sink is None:
            raise KnowledgeFailure("知识库 sink 未配置")
        self._kb_review().request_review(task, args.proposal.model_dump(), call.call_id)
        raise AssertionError("unreachable")  # request_review 总以 ApprovalPending 结束

    # ---- 收口 ----

    def _tool_finish(self, task: TriageTask, call: ToolCall):
        args = FinishArgs.model_validate(call.arguments)
        verification = verify(args, self._sources(task))
        if task.source is TaskSource.MANUAL and task.task_type != "artifact" and not verification["grounded"]:
            # 结论没有服务器事实依据(没有成立的猜测,或无猜测时没有摘录成功的事实)才转发起人审;
            # 产物任务的成果是产物本身,不按排查结论设卡
            request_report_gate(
                self.store, task, call_id=call.call_id,
                reason="结论没有服务器事实依据:没有成立的猜测或可摘录的事实",
                proposal={"finish": args.model_dump(), "verification": verification},
            )
        task, result = self._write_report(task, args, verification)
        self._round_end = task
        return task, result, True

    def _write_report(self, task: TriageTask, args: FinishArgs, verification) -> Tuple[TriageTask, Dict[str, Any]]:
        fields = report_fields(args, verification)
        if not task.run_id and task.platform and (task.node or task.scope == "cluster"):
            node = task.node or (self.actions.known_nodes(task.platform) or [None])[0]
            task = ensure_session(self.store, self.actions, task, node=node)
        report = None
        if task.run_id:
            self.write_ops.retry_oplogs(task)
            try:
                report = self.actions.record_finish(task.run_id, **fields)
            except (ValueError, OplogMissing) as exc:  # 报告契约问题(如缺 oplogs)回模型自纠
                raise OrchestrationError(f"报告未写入: {exc}") from exc
            task = self.store.update(task.task_id, expected=TaskStatus.RUNNING,
                                     report_path=self._display_path(report))
            if task.source is TaskSource.AUTO:
                # 自动任务保持收口即落 worklog(静默,不挡收口)
                try:
                    self.actions.worklog_commit(task.run_id, kb_doc=task.kb_doc, author=task.created_by)
                except Exception:  # noqa: BLE001
                    pass
        self.store.append_event(task.task_id, "assistant_message", {
            "message": {"role": "assistant", "content": args.answer},
            "finish": {"verdict": fields["verdict"], "hypotheses": fields["finish"]["hypotheses"]},
        })
        return task, {
            "verdict": fields["verdict"],
            "grounded": verification["grounded"],
            "hypotheses": [
                {"statement": item["statement"], "status": item["status"], "declared": item["declared"],
                 "status_label": STATUS_LABELS[item["status"]], "downgraded": item["reasons"],
                 "evidence": [_evidence_view(check) for check in item["evidence"]],
                 "related": [_evidence_view({**check, "role": "related"}) for check in item["related"]]}
                for item in verification["hypotheses"]
            ],
            "facts": [_evidence_view({**item, "role": "fact"}) for item in verification["facts"]],
            "report": task.report_path,
        }

    # ---- 挂起裁决的兑现 ----

    def _consume_worklog(self, task: TriageTask) -> Optional[TriageTask]:
        flow = WorklogFlow(self.store, self.actions, self.model)
        return flow.consume_decision(task) or flow.consume_manual_trigger(self.store.get(task.task_id))

    def _consume_budget(self, task: TriageTask) -> Optional[TriageTask]:
        decision = consume_budget_decision(self.store, task)
        if decision is not None and not decision.approved:
            # 人工拒绝续期:进入收口回合,不直接失败(A2 §5.3)
            self._enter_closing(task, f"{decision.reason},人工确认不再续期")
        return None

    def _consume_kb(self, task: TriageTask) -> Optional[TriageTask]:
        review = self._kb_review()
        return review.consume_proposal(task, finish=self._finish_round) or \
            review.consume_draft(self.store.get(task.task_id), finish=self._finish_round)

    def _consume_report_gate(self, task: TriageTask) -> Optional[TriageTask]:
        gate = pending_report_gate(self.store, task.task_id)
        if gate is None:
            return None
        if gate.decision == "approve":
            args = FinishArgs.model_validate(gate.proposal.get("finish") or {})
            verification = gate.proposal.get("verification") or verify(args, self._sources(task))
            task, result = self._write_report(task, args, verification)
            self.invocations.settle(task.task_id, gate.call_id, "finish", result)
            return self._finish_round(task)
        if gate.decision == "reject":
            reason = gate.feedback or "发起人终止:收口没有可核验的证据"
            self.invocations.settle(task.task_id, gate.call_id, "finish", {"error": reason, "failed": True})
            return self._fail(task.task_id, f"报告被人工终止: {reason}", TaskStatus.FAILED, "rejections")
        self.invocations.settle(task.task_id, gate.call_id, "finish",
                                {"error": gate.revision_error(), "failed": True})
        return None

    def _consume_command(self, task: TriageTask) -> Optional[TriageTask]:
        CommandApprovals(self.store, self.invocations, policy=self.command_policy,
                         reviewer=self.command_reviewer).consume(
            task, tool_name="shell", execute=self._execute_approved_shell,
        )
        return None

    # ---- 辅助 ----

    def _kb_review(self) -> KbReview:
        return KbReview(self.store, self.invocations, self.knowledge_sink, self._display_path)

    def _policy_config(self) -> PolicyConfig:
        if self.command_policy is None:
            return PolicyConfig()
        try:
            return self.command_policy.load()
        except CommandPolicyError as exc:
            raise OrchestrationError(f"命令策略不可用: {exc}") from exc

    def _platform(self, task: TriageTask) -> str:
        if not task.platform:
            raise OrchestrationError("任务尚未确定集群,先调用 set_task_context")
        return task.platform

    def _resolve_nodes(self, task: TriageTask, raw: List[str]) -> Tuple[str, List[str]]:
        platform = self._platform(task)
        known = list(self.actions.known_nodes(platform))
        if raw == ["all"]:
            return platform, known
        resolved = []
        for value in raw:
            node = self.actions.resolve_node(platform, value) if value else None
            if node is None or node not in known:
                raise OrchestrationError(f"节点 {value or '(未指定)'} 不在集群 {platform},可选: {', '.join(known)}")
            resolved.append(node)
        return platform, list(dict.fromkeys(resolved))

    def _session(self, task: TriageTask) -> Dict[str, Any]:
        if not task.run_id:
            return {}
        try:
            return self.actions.session_data(task.run_id) or {}
        except ActionDenied:
            return {}

    def _steps(self, task: TriageTask) -> List[Dict[str, Any]]:
        return list(self._session(task).get("steps") or [])

    def _sources(self, task: TriageTask) -> Dict[str, Dict[str, Any]]:
        return evidence_sources(self._session(task))

    def _notice(self, task: TriageTask, message: str) -> None:
        self.store.append_event(task.task_id, "harness_notice", {"message": message})

    @staticmethod
    def _latest_user_message(events) -> str:
        for event in reversed(events):
            if event.type == "user_message":
                return str(event.payload.get("message") or "").strip()
        return ""

    @staticmethod
    def _execution_id(task: TriageTask) -> str:
        return task.execution_id or task.run_id or task.task_id

    def _display_path(self, path) -> str:
        try:
            return Path(path).resolve().relative_to(self.repo_root.resolve()).as_posix()
        except ValueError:
            return str(path)

    def _fail(self, task_id: str, message: str, status: TaskStatus, failure_kind=None) -> TriageTask:
        task = self.store.get(task_id)
        self.store.append_event(task_id, "error", {"message": message})
        if task.status is TaskStatus.RUNNING:
            return self.store.transition(task_id, TaskStatus.RUNNING, status, error=message,
                                         failure_kind=failure_kind, lease_owner=None, lease_expires_at=None)
        return task

    def _close_terminal_session(self, task_id: str, execution_id: str) -> None:
        try:
            latest = self.store.get(task_id)
        except Exception:  # noqa: BLE001
            return
        if latest.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.KB_FAILED, TaskStatus.CANCELLED}:
            close = getattr(self.actions, "close_task_session", None)
            if callable(close):
                close(execution_id)


def _is_recoverable(exc: Exception) -> bool:
    """模型可自纠的业务驳回回 error tool result;知识库环境性失败与需人工的保持失败语义。"""
    if isinstance(exc, KnowledgeFailure):
        return False
    if getattr(exc, "disposition", None) in {"task_fail", "needs_user"}:
        return False
    return isinstance(exc, (OrchestrationError, ValidationError, DiagnosticDenied, ActionDenied,
                            KbProposalRejected, RemoteExecutionError, ValueError))


def _evidence_view(check: Dict[str, Any]) -> Dict[str, Any]:
    """收口结果里一条证据的展示形态:摘录到的原文或未摘录的原因(前端显示核验标记)。"""
    return {key: check.get(key) for key in ("role", "ref", "found", "text", "reason", "value") if key in check}


def _usage(raw: Dict[str, Any]) -> Dict[str, Any]:
    details = raw.get("prompt_tokens_details") or {}
    completion = raw.get("completion_tokens_details") or {}
    return {
        "prompt_tokens": int(raw.get("prompt_tokens") or 0),
        "completion_tokens": int(raw.get("completion_tokens") or 0),
        "cached_tokens": int(details.get("cached_tokens") or raw.get("prompt_cache_hit_tokens") or 0),
        "reasoning_tokens": int(completion.get("reasoning_tokens") or 0),
    }


def _input_tokens(events) -> int:
    return sum(int(event.payload.get("prompt_tokens") or 0) for event in events if event.type == "model_usage")
