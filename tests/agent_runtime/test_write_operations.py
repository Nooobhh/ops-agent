"""M6 写操作经新运行时的端到端行为:计划 → 人工审批 → Harness 签令牌执行 → 跟踪 → 落档。

会话、records、插件调用与 oplog 走真实网关与知识 sink(落 tmp),插件是 tests/plugins/fake_ops_mcp
(与 ops-mcp 同逻辑验签)。覆盖:拒绝 / 打回 / 自动任务、计划过期重批、密钥不符、状态变化、冲突、
执行失败 / 部分失败 / 未复核 / 跟踪超时、三处中断恢复不重复下发、密钥与令牌零落档。
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib.agent_runtime import AgentRuntime
from lib.kb.lint import lint_doc
from lib.plugins import PluginService
from lib.triage_agent import actions as actions_module
from lib.triage_agent.actions import ActionGateway
from lib.triage_agent.diagnostics import DiagnosticRegistry
from lib.triage_agent.knowledge_sink import KnowledgeSink
from lib.triage_agent.models import TaskStatus
from lib.triage_agent.paths import AppPaths
from lib.triage_agent.service import TaskConflict, TriageTaskService
from lib.triage_agent.task_store import InvalidTransition, TaskStore
from lib.triage_agent.write_operations import POLL_FAST_SECONDS, WriteOperations, load_operation
from tests.plugins.fake_ops_mcp import PLUGIN, Clock, FakeOpsMcp, install_plugin, transport_timeout

from .conftest import FakeActions, ScriptedModel, call, new_task, reply

SECRET = "c3" * 32
PLAN = f"mcp__{PLUGIN}__plan_operation"
ARGS = {"action": "restart", "scope": "component", "component": "LOGSEARCH_LOGFEEDER", "host": "node11",
        "reason": "配置已修改需重启生效"}
HAZE = {"id": "alice", "name": "alice"}


class GatewayActions(FakeActions):
    """会话、records、插件调用走真实 ActionGateway;其余(检索材料等)沿用替身。"""

    def __init__(self, root, gateway):
        super().__init__(root)
        self.gateway = gateway
        self.paths = SimpleNamespace(**{**vars(self.paths), "records_root": gateway.paths.records_root})

    def start_session(self, **kwargs):
        return self.gateway.start_session(**kwargs)

    def session_data(self, run_id):
        return self.gateway.session_data(run_id)

    def update_session_context(self, run_id, **kwargs):
        return self.gateway.update_session_context(run_id, **kwargs)

    def record_finish(self, run_id, **fields):
        self.finishes.append(fields)
        return self.gateway.record_finish(run_id, **fields)

    def record_turn_summary(self, run_id, summary):
        return self.gateway.record_turn_summary(run_id, summary)

    def plugin_snapshot(self, platform, writable=False):
        return self.gateway.plugin_snapshot(platform, writable=writable)

    def plugin_health(self, plugin_id):
        return {"state": "ok"}

    def run_plugin_tool(self, run_id, **kwargs):
        return self.gateway.run_plugin_tool(run_id, **kwargs)

    def run_plugin_write_tool(self, run_id, **kwargs):
        return self.gateway.run_plugin_write_tool(run_id, **kwargs)

    def execute_plugin_operation(self, run_id, **kwargs):
        return self.gateway.execute_plugin_operation(run_id, **kwargs)

    def record_plugin_result(self, run_id, **kwargs):
        return self.gateway.record_plugin_result(run_id, **kwargs)

    def record_write_operation(self, run_id, entry):
        return self.gateway.record_write_operation(run_id, entry)


@pytest.fixture
def world(tmp_path, monkeypatch):
    paths = AppPaths.from_repo_root(tmp_path)
    paths.knowledge_root.mkdir(parents=True)
    gateway = ActionGateway(paths, DiagnosticRegistry({}, []),
                            pool_factory=lambda _platform: pytest.fail("插件写不得走 SSH"))
    service = PluginService(tmp_path, paths.state_dir)
    install_plugin(service, secret=SECRET)
    clock = Clock()
    mcp = FakeOpsMcp(SECRET, clock)
    monkeypatch.setattr(actions_module, "call_plugin_tool", mcp)
    return SimpleNamespace(
        tmp=tmp_path, paths=paths, service=service, mcp=mcp, clock=clock,
        store=TaskStore(tmp_path / "tasks"), actions=GatewayActions(tmp_path, gateway),
        sink=KnowledgeSink(paths, validate=lambda: None, rebuild=lambda: None),
    )


def runtime(world, replies=()):
    model = ScriptedModel(list(replies))
    agent = AgentRuntime(store=world.store, model=model, actions=world.actions, knowledge_sink=world.sink,
                         retry_delays=(), sleep=lambda _: None)
    agent.write_ops.clock = world.clock
    return agent, model


def tracker(world):
    return WriteOperations(world.store, None, world.actions, clock=world.clock)


def events(world, task_id, kind=None):
    return [event for event in world.store.events(task_id) if kind is None or event.type == kind]


def approval(world, task_id):
    return events(world, task_id, "approval_requested")[-1].payload


def tool_result(world, task_id, call_id):
    return next(event.payload["result"] for event in events(world, task_id, "tool_result")
                if event.payload["tool_call_id"] == call_id)


def model_view(world, task_id, call_id):
    """工具结果信封里的业务内容(模型投影由它派生)。"""
    return tool_result(world, task_id, call_id)["data"]


def notices(world, task_id):
    return [event.payload["message"] for event in events(world, task_id, "harness_notice")]


def session(world, task_id):
    return world.actions.session_data(world.store.get(task_id).run_id)


def propose(world, arguments=ARGS):
    task = new_task(world.store, message="重启 node11 的 Log Feeder 让配置生效")
    agent, model = runtime(world, [reply(call(PLAN, arguments, "w1"))])
    assert agent.run(task.task_id).status is TaskStatus.WAITING_APPROVAL
    assert PLAN in model.requests[0][1]
    return task, approval(world, task.task_id)


def decide(world, task, request, decision="approve", feedback="", replies=(), decided_by=HAZE):
    world.store.resolve_approval(task.task_id, request["approval_id"], decision=decision, feedback=feedback,
                                 decided_by=decided_by)
    world.store.claim_next("w1", lease_seconds=60)
    agent, model = runtime(world, replies)
    return agent.run(task.task_id), model


def resume(world, task, replies=()):
    world.store.claim_next("w1", lease_seconds=60)
    agent, model = runtime(world, replies)
    return agent.run(task.task_id), model


def track_to_end(world, task):
    for _ in range(10):
        if world.store.get(task.task_id).status is not TaskStatus.WAITING_OPERATION:
            return
        tracker(world).poll(task.task_id)
    pytest.fail("跟踪没有结束")


def finish_reply(match):
    return reply(call("finish", {"answer": "已重启并复核", "confidence": "high",
                                 "facts": [{"ref": "step:3", "match": match}]}, "f1"))


def all_text(root: Path) -> str:
    """数据根下除凭据库外的全部文件文本(任务、事件、records、知识库、缓存)。"""
    parts = []
    for path in root.rglob("*"):
        if path.is_file() and path.name != "auth-profiles.json":
            parts.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(parts)


# ---- 主链路 ----

def test_approved_restart_runs_end_to_end_with_records_report_and_oplog(world):
    task, request = propose(world)
    assert request["kind"] == "operation" and request["title"] == "重启 LOGSEARCH_LOGFEEDER@node11"
    assert request["plan"]["current"][0]["state"] == "STARTED" and request["plan_warnings"] == []
    assert request["expected_max_duration_s"] == 1200 and request["expires_at"] > request["created_at"]
    assert world.mcp.executed() == [], "提出计划不执行"

    done, _ = decide(world, task, request)
    assert done.status is TaskStatus.WAITING_OPERATION and done.operation_poll_at
    assert world.mcp.executed() == [{"plan_id": request["plan_id"]}]
    submitted = model_view(world, task.task_id, "w1")
    assert submitted["approved"] and submitted["request_id"] == 3301 and submitted["approver"] == "alice"

    tracker(world).poll(task.task_id)
    assert world.store.get(task.task_id).status is TaskStatus.WAITING_OPERATION
    tracker(world).poll(task.task_id)
    assert world.store.get(task.task_id).status is TaskStatus.QUEUED
    assert [event.payload["request_status"] for event in events(world, task.task_id, "operation_progress")] \
        == ["IN_PROGRESS", "COMPLETED"]

    done, model = resume(world, task, [finish_reply("COMPLETED")])
    assert done.status is TaskStatus.WAITING_INPUT
    assert "【写操作结果】重启 LOGSEARCH_LOGFEEDER@node11:成功" in json.dumps(model.requests[0][0], ensure_ascii=False)
    data = session(world, task.task_id)
    op = data["write_operations"][0]
    assert op["outcome"] == "succeeded" and op["approver"] == "alice" and op["request_id"] == 3301
    assert op["decided_at"].endswith("+00:00"), "批准时间按环境包时区(示例环境 UTC)展示,与报告其余时间一致"
    assert op["steps"] == ["step:1", "step:2", "step:3"]
    assert [step["approved"] for step in data["steps"]] == [False, True, False]
    assert data["oplogs"] == [op["oplog"]], "oplog 引用自动并入,finish 的 oplog 闸通过"
    oplog = world.paths.knowledge_root / op["oplog"].removeprefix("knowledge/")
    assert lint_doc(oplog, world.paths.knowledge_root) == []
    text = oplog.read_text(encoding="utf-8")
    assert "type: oplog" in text and "status: draft" in text and "alice" in text and "3301" in text
    report = (Path(world.paths.records_root) / data["run_id"] / "report.md").read_text(encoding="utf-8")
    assert "## 写操作" in report and "成功" in report and op["oplog"] in report
    assert [event.payload["outcome"] for event in events(world, task.task_id, "operation_finished")] == ["succeeded"]

    leaked = all_text(world.tmp)
    assert SECRET not in leaked and all(token not in leaked for token in world.mcp.tokens)


def test_noop_plan_and_plan_errors_go_straight_back_to_the_model(world):
    world.mcp.noop = True
    task = new_task(world.store)
    agent, _ = runtime(world, [reply(call(PLAN, ARGS, "w1")), reply(content="已是运行状态")])
    assert agent.run(task.task_id).status is TaskStatus.WAITING_INPUT
    assert model_view(world, task.task_id, "w1")["approval"] == "not_required"

    world.mcp.noop, world.mcp.plan_error = False, "SERVICE_OPERATION_CONFLICT"
    task = new_task(world.store)
    agent, _ = runtime(world, [reply(call(PLAN, ARGS, "w2")), reply(content="有冲突")])
    assert agent.run(task.task_id).status is TaskStatus.WAITING_INPUT
    assert "SERVICE_OPERATION_CONFLICT" in json.dumps(tool_result(world, task.task_id, "w2"))
    assert not events(world, task.task_id, "approval_requested") and world.mcp.executed() == []


@pytest.mark.parametrize("decision, feedback", [("reject", ""), ("revise", "先停 node12 那台")])
def test_rejected_or_revised_plans_are_never_executed(world, decision, feedback):
    task, request = propose(world)
    done, _ = decide(world, task, request, decision, feedback, replies=[reply(content="好的")])
    assert done.status is TaskStatus.WAITING_INPUT, world.store.get(task.task_id).error
    assert world.mcp.executed() == []
    result = model_view(world, task.task_id, "w1")
    assert result["approved"] is False and result["decision"] == decision and result.get("feedback") == feedback
    entry = session(world, task.task_id)["write_operations"][0]
    assert entry["outcome"] == "not_executed" and "oplog" not in entry


def test_auto_tasks_never_see_the_plan_tool(world):
    world.store.enqueue_auto("occ-1", {"alertname": "X", "platform": "alpha", "host": "node1"})
    task = world.store.claim_next("w1", lease_seconds=60)
    world.store.update(task.task_id, expected=TaskStatus.RUNNING, task_type="default", platform="alpha", node="node1")
    agent, model = runtime(world, [reply(call("finish", {"answer": "无", "confidence": "low"}, "f1"))])
    agent.run(task.task_id)
    assert PLAN not in model.requests[0][1]
    assert not any(item.get("role") == "write_plan" for item in world.store.get(task.task_id).plugin_tools["tools"])


def test_approving_a_write_requires_an_approver_identity(world):
    task, request = propose(world)
    service = TriageTaskService(world.store)
    with pytest.raises(TaskConflict, match="审批人"):
        service.decide_approval(task.task_id, request["approval_id"], decision="approve")
    service.decide_approval(task.task_id, request["approval_id"], decision="approve", decided_by=HAZE)
    decided = events(world, task.task_id, "approval_decided")[-1].payload
    assert decided["decided_by"] == HAZE


# ---- 过期与插件拒绝 ----

def test_plan_expired_at_approval_is_replanned_into_a_new_card(world):
    task, request = propose(world)
    world.clock.advance(world.mcp.plan_ttl + 1)
    done, _ = decide(world, task, request)
    assert done.status is TaskStatus.WAITING_APPROVAL and world.mcp.executed() == []
    fresh = approval(world, task.task_id)
    assert fresh["replanned_from"] == request["approval_id"] and fresh["plan_id"] != request["plan_id"]
    assert fresh["tool_call_id"] == "w1"
    assert events(world, task.task_id, "operation_finished")[-1].payload["replanned"] is True

    done, _ = decide(world, task, fresh)
    assert done.status is TaskStatus.WAITING_OPERATION
    assert world.mcp.executed() == [{"plan_id": fresh["plan_id"]}]


def test_plugin_restart_losing_the_plan_is_replanned(world):
    task, request = propose(world)
    world.mcp.plans.clear()  # MCP 进程重启:内存里的计划没了
    done, _ = decide(world, task, request)
    assert done.status is TaskStatus.WAITING_APPROVAL
    assert approval(world, task.task_id)["replanned_from"] == request["approval_id"]


@pytest.mark.parametrize("break_it, code", [
    (lambda world: setattr(world.mcp, "state_changed", True), "批准后目标状态已变化"),
    (lambda world: setattr(world.mcp, "execute_error", "SERVICE_OPERATION_CONFLICT"), "进行中的操作"),
    (lambda world: world.service.credentials.save_plugin_approval_secret(PLUGIN, "d4" * 32), "审批密钥不一致"),
    (lambda world: world.service.registry.set_tool(PLUGIN, "plan_operation", enabled=False), "插件页停用"),
])
def test_plugin_refusals_are_not_executed_and_explained(world, break_it, code):
    task, request = propose(world)
    break_it(world)
    done, _ = decide(world, task, request, replies=[reply(content="未执行")])
    assert done.status is TaskStatus.WAITING_INPUT and world.mcp.requests == {}
    assert code in json.dumps(tool_result(world, task.task_id, "w1"), ensure_ascii=False)
    entry = session(world, task.task_id)["write_operations"][0]
    assert entry["outcome"] == "not_executed" and "oplog" not in entry
    assert not any(step["approved"] for step in session(world, task.task_id)["steps"])
    if code != "插件页停用":  # 插件页停用时没调用 execute,也就没有执行步骤
        assert model_view(world, task.task_id, "w1")["refs"] == ["step:1", "step:2"], "拒绝原因可按 ref 引用"


# ---- 执行结果 ----

@pytest.mark.parametrize("configure, outcome, fragment", [
    (lambda mcp: setattr(mcp, "track_script", ["FAILED"]), "failed", "失败任务"),
    (lambda mcp: setattr(mcp, "verification", {"succeeded": False, "mismatched": [
        {"host": "node11", "component": "LOGSEARCH_LOGFEEDER", "state": "INSTALLED"}]}), "partial", "复核状态不符"),
    (lambda mcp: setattr(mcp, "lost_verification", True), "unverified", "未给出复核结果"),
])
def test_outcomes_are_classified_recorded_and_announced(world, configure, outcome, fragment):
    configure(world.mcp)
    task, request = propose(world)
    decide(world, task, request)
    track_to_end(world, task)
    done, model = resume(world, task, [reply(content="已知悉")])
    assert done.status is TaskStatus.WAITING_INPUT
    entry = session(world, task.task_id)["write_operations"][0]
    assert entry["outcome"] == outcome and entry["oplog"]
    notice = notices(world, task.task_id)[-1]
    assert fragment in notice and "hunter2" not in notice


def test_tracking_past_the_deadline_is_unknown_but_still_logged(world):
    world.mcp.track_script = ["IN_PROGRESS"]
    task, request = propose(world)
    decide(world, task, request)
    tracker(world).poll(task.task_id)
    world.clock.advance(1200 + 300 + 1)
    tracker(world).poll(task.task_id)
    assert world.store.get(task.task_id).status is TaskStatus.QUEUED
    resume(world, task, [reply(content="会去后台确认")])
    entry = session(world, task.task_id)["write_operations"][0]
    assert entry["outcome"] == "unknown" and entry["oplog"]
    assert "可能仍在执行" in notices(world, task.task_id)[-1]


def test_waiting_operation_cannot_be_cancelled(world):
    task, request = propose(world)
    decide(world, task, request)
    with pytest.raises(InvalidTransition):
        world.store.transition(task.task_id, TaskStatus.WAITING_OPERATION, TaskStatus.CANCELLED)
    with pytest.raises(TaskConflict, match="写操作执行中"):
        TriageTaskService(world.store).cancel(task.task_id)
    with pytest.raises(TaskConflict, match="写操作执行中"):
        TriageTaskService(world.store).update_runtime_controls(task.task_id, {"command_mode": "auto"})


def test_due_operations_honour_the_poll_time(world):
    task, request = propose(world)
    decide(world, task, request)
    assert world.store.due_operations() == [], f"{POLL_FAST_SECONDS}s 后才到期"
    world.store.update(task.task_id, expected=TaskStatus.WAITING_OPERATION,
                       operation_poll_at="2000-01-01T00:00:00+00:00")
    assert world.store.due_operations() == [task.task_id]


# ---- 中断恢复:不重复下发 ----

def test_timeout_after_submission_is_reconciled_without_resending(world):
    task, request = propose(world)
    world.mcp.execute_raises, world.mcp.submit_first = transport_timeout(), True
    done, _ = decide(world, task, request)
    assert done.status is TaskStatus.WAITING_OPERATION, "结果未知:等对账,不判失败"
    tracker(world).poll(task.task_id)  # 还没有请求号:交回运行时对账
    done, _ = resume(world, task)
    assert done.status is TaskStatus.WAITING_OPERATION
    assert len(world.mcp.executed()) == 1, "后台找到本计划的请求,不重发"
    assert events(world, task.task_id, "operation_submitted")[-1].payload["recovered"] is True
    assert model_view(world, task.task_id, "w1")["request_id"] == 3301


def test_timeout_before_submission_is_resent_once_while_plan_is_valid(world):
    task, request = propose(world)
    world.mcp.execute_raises, world.mcp.submit_first = transport_timeout(), False
    decide(world, task, request)
    tracker(world).poll(task.task_id)
    done, _ = resume(world, task)
    assert done.status is TaskStatus.WAITING_OPERATION
    assert len(world.mcp.executed()) == 2 and len(world.mcp.requests) == 1


def test_crash_between_write_ahead_log_and_execute_reconciles_first(world):
    task, request = propose(world)
    world.store.resolve_approval(task.task_id, request["approval_id"], decision="approve", decided_by=HAZE)
    world.store.claim_next("w1", lease_seconds=60)
    world.store.append_event(task.task_id, "operation_executing", {
        "approval_id": request["approval_id"], "attempt": 1, "plan_id": request["plan_id"], "approver": "alice"})
    # worker 在写前日志之后、调用之前死掉;接管时挂起的调用被补成「中断」
    world.store.update(task.task_id, expected=TaskStatus.RUNNING, lease_expires_at="2000-01-01T00:00:00+00:00")
    world.store.claim_next("w2", lease_seconds=60)
    agent, _ = runtime(world)
    assert agent.run(task.task_id).status is TaskStatus.WAITING_OPERATION
    assert len(world.mcp.executed()) == 1
    assert "已由 Harness 下发" in notices(world, task.task_id)[-1], "原调用已被中断补齐,改用通知告知"


def test_crash_during_tracking_resumes_tracking_only(world):
    task, request = propose(world)
    decide(world, task, request)
    world.store.transition(task.task_id, TaskStatus.WAITING_OPERATION, TaskStatus.QUEUED)
    done, _ = resume(world, task)
    assert done.status is TaskStatus.WAITING_OPERATION and len(world.mcp.executed()) == 1
    assert load_operation(world.store, task.task_id).finished is None


def test_impact_lists_downstream_services_for_service_restarts(world):
    task, request = propose(world, {"action": "restart", "scope": "service", "service": "LOGSEARCH",
                                    "reason": "配置生效"})
    assert request["impact"] == {"ok": True, "service": "LOGSEARCH", "downstream": ["YARN", "HBASE"],
                                 "upstream": ["ZOOKEEPER"]}
    assert ("get_topology", {"service": "LOGSEARCH"}) in world.mcp.calls


# ---- SSH 写命令(替身网关):批准执行后有写操作记录与 oplog ----

class FakeSink:
    def __init__(self):
        self.oplogs = []

    def write_oplog(self, run_id, **fields):
        self.oplogs.append(fields)
        return f"knowledge/oplog/{fields['platform']}/{fields['problem']}.md"


def test_approved_shell_write_is_logged_and_approved_reads_are_not_writes(env):
    from lib.triage_agent.command_policy import CommandPolicy

    policy_path = env.tmp / "command-policy.json"
    policy_path.write_text(json.dumps({"mode": "whitelist", "allow": [], "deny": []}), encoding="utf-8")
    sink = FakeSink()

    def run(replies):
        return AgentRuntime(store=env.store, model=ScriptedModel(replies), actions=env.actions, knowledge_sink=sink,
                            command_policy=CommandPolicy(policy_path), retry_delays=(), sleep=lambda _: None)

    for command, write in (("rm -f /tmp/x", True), ("foo-tool -x", False)):
        task = new_task(env.store)
        run([reply(call("shell", {"nodes": ["node1"], "command": command}, "c1"))]).run(task.task_id)
        request = [event.payload for event in env.store.events(task.task_id)
                   if event.type == "approval_requested"][-1]
        env.store.resolve_approval(task.task_id, request["approval_id"], decision="approve", scope="once",
                                   decided_by=HAZE)
        env.store.claim_next("w1", lease_seconds=60)
        run([reply(content="完成")]).run(task.task_id)
        data = env.actions.session_data(env.store.get(task.task_id).run_id)
        assert data["steps"][-1]["approved"] is write
        operations = data.get("write_operations") or []
        if write:
            assert operations[0]["kind"] == "shell" and operations[0]["approver"] == "alice"
            assert operations[0]["outcome"] == "succeeded" and operations[0]["oplog"].startswith("knowledge/oplog/")
            assert "rm -f /tmp/x" in sink.oplogs[-1]["sections"]["操作步骤"]
        else:
            assert operations == []


class FlakyValidate:
    """知识库校验前 n 次失败(模拟 lint/索引临时出错)。"""

    def __init__(self, failures):
        self.failures = failures

    def __call__(self):
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("kb lint 临时失败")


@pytest.mark.parametrize("failures, finished", [(1, True), (5, False)])
def test_oplog_failure_is_retried_before_finish_and_otherwise_blocks_it(world, failures, finished):
    world.sink = KnowledgeSink(world.paths, validate=FlakyValidate(failures), rebuild=lambda: None)
    task, request = propose(world)
    decide(world, task, request)
    track_to_end(world, task)
    replies = [finish_reply("COMPLETED")] if finished else [finish_reply("COMPLETED"), reply(content="oplog 写不进去")]
    done, _ = resume(world, task, replies)
    data = session(world, task.task_id)
    if finished:
        assert data["verdict"] is not None and data["write_operations"][0]["oplog"] in data["oplogs"]
        assert "oplog_error" not in data["write_operations"][0]
    else:
        assert done.status is TaskStatus.WAITING_INPUT and data["verdict"] is None, "缺 oplog 收不了口"
        assert "oplog" in json.dumps(tool_result(world, task.task_id, "f1"), ensure_ascii=False)


def test_plans_that_keep_expiring_stop_after_the_replan_cap(world):
    task, request = propose(world)
    for _ in range(3):
        world.clock.advance(world.mcp.plan_ttl + 1)
        done, _ = decide(world, task, request, replies=[reply(content="计划总是过期")])
        request = approval(world, task.task_id)
    assert done.status is TaskStatus.WAITING_INPUT and world.mcp.executed() == []
    assert "连续失效" in json.dumps(tool_result(world, task.task_id, "w1"), ensure_ascii=False)
