from lib.agent_runtime import AgentRuntime
from lib.agent_runtime.tiers import HARD_TURNS
from lib.triage_agent.models import TaskStatus

from .conftest import ScriptedModel, call, new_task, reply


def _runtime(env, replies, **kwargs):
    model = ScriptedModel(replies)
    return AgentRuntime(store=env.store, model=model, actions=env.actions,
                        retry_delays=(), sleep=lambda _: None, **kwargs), model


def _events(env, task_id, kind):
    return [event.payload for event in env.store.events(task_id) if event.type == kind]


def _finish(facts, call_id=None, **extra):
    """问答式收口:facts 为 [(ref, match)]。"""
    return call("finish", {"answer": "node1 正常", "confidence": "high",
                           "facts": [{"ref": ref, "match": match} for ref, match in facts], **extra}, call_id)


def test_supported_hypothesis_writes_conclusive_report_with_extracted_lines(env):
    task = new_task(env.store)
    env.actions.outputs["grep -i heartbeat dn.log"] = "2026-10-21 13:19:02 WARN heartbeat took 30012 ms\n"
    env.actions.outputs["grep heartbeat hdfs-site.xml"] = "<name>dfs.heartbeat.timeout</name><value>5000</value>\n"
    hypothesis = {"statement": "DN 心跳超时", "status": "supported", "evidence": [
        {"role": "observation", "ref": "step:1", "match": "heartbeat took 30012", "value": "30012"},
        {"role": "criterion", "ref": "step:2", "match": "dfs.heartbeat.timeout", "value": "5000"},
    ]}
    runtime, model = _runtime(env, [
        reply(call("shell", {"nodes": ["node1"], "command": "grep -i heartbeat dn.log"}, "c1"),
              usage={"prompt_tokens": 900, "prompt_tokens_details": {"cached_tokens": 800}}),
        reply(call("shell", {"nodes": ["node1"], "command": "grep heartbeat hdfs-site.xml"}, "c2")),
        reply(call("finish", {"answer": "心跳超时导致", "confidence": "high", "hypotheses": [hypothesis],
                              "incident_window": {"start": "2026-10-21 13:18", "end": "2026-10-21 13:22"}}, "f1")),
    ])
    result = runtime.run(task.task_id)

    assert result.status is TaskStatus.WAITING_INPUT
    report = env.actions.finishes[-1]
    assert report["verdict"] == "conclusive" and report["root_cause"] == "DN 心跳超时"
    assert report["evidence"][0] == "[成立] DN 心跳超时"
    assert report["evidence"][1] == "  [异常观测·step:1] 2026-10-21 13:19:02 WARN heartbeat took 30012 ms"
    assert _events(env, task.task_id, "model_usage")[0]["cached_tokens"] == 800
    shell_result = next(p for p in _events(env, task.task_id, "tool_result") if p["name"] == "shell")
    assert shell_result["result"]["model_result"]["results"][0]["ref"] == "step:1"


def test_messages_are_append_only_with_stable_prefix(env):
    task = new_task(env.store)
    runtime, model = _runtime(env, [
        reply(call("shell", {"nodes": ["node1"], "command": "uptime"}, "c1")),
        reply(call("shell", {"nodes": ["node2"], "command": "uptime"}, "c2")),
        reply(_finish([("step:2", "node2 ok uptime")])),
    ])
    runtime.run(task.task_id)

    first, second, third = (messages for messages, _tools in model.requests)
    assert second[:len(first)] == first and third[:len(second)] == second
    assert first[0]["role"] == "system" and "【任务上下文】" in first[-1]["content"]


def test_zero_verified_evidence_goes_to_report_gate_then_approval_writes_report(env):
    task = new_task(env.store)
    runtime, _ = _runtime(env, [reply(_finish([("step:9", "不存在的输出片段")], call_id="f1"))])
    waiting = runtime.run(task.task_id)
    assert waiting.status is TaskStatus.WAITING_APPROVAL
    assert not env.actions.finishes

    approval = _events(env, task.task_id, "approval_requested")[-1]
    env.store.resolve_approval(task.task_id, approval["approval_id"], decision="approve")
    env.store.claim_next("w1", lease_seconds=60)
    runtime, _ = _runtime(env, [])
    done = runtime.run(task.task_id)

    assert done.status is TaskStatus.WAITING_INPUT
    assert env.actions.finishes[-1]["verdict"] == "insufficient"


def test_command_guard_denial_is_returned_to_model(env):
    task = new_task(env.store)
    runtime, model = _runtime(env, [
        reply(call("shell", {"nodes": ["node1"], "command": "sudo ls /root"}, "c1")),
        reply(call("shell", {"nodes": ["node1"], "command": "ls /tmp"}, "c2")),
        reply(_finish([("step:1", "node1 ok ls /tmp")])),
    ])
    assert runtime.run(task.task_id).status is TaskStatus.WAITING_INPUT
    assert [cmd for _node, cmd, _approved in env.actions.shell_calls] == ["ls /tmp"]
    denied = next(p for p in _events(env, task.task_id, "tool_result") if p["tool_call_id"] == "c1")
    assert "sudo" in str(denied["result"]["error"])


def test_unregistered_command_waits_for_approval_then_runs_original_call(env):
    task = new_task(env.store)
    runtime, _ = _runtime(env, [reply(call("shell", {"nodes": ["node1"], "command": "foo-tool -x"}, "c1"))])
    assert runtime.run(task.task_id).status is TaskStatus.WAITING_APPROVAL
    approval = _events(env, task.task_id, "approval_requested")[-1]
    assert approval["kind"] == "command" and approval["signature"] == "foo-tool -x"

    env.store.resolve_approval(task.task_id, approval["approval_id"], decision="approve", scope="task")
    env.store.claim_next("w1", lease_seconds=60)
    runtime, _ = _runtime(env, [reply(_finish([("step:1", "node1 ok uptime")]))])
    runtime.run(task.task_id)

    assert env.actions.shell_calls == [("node1", "foo-tool -x", True)]
    assert env.store.get(task.task_id).command_defaults == ["foo-tool -x"]


def test_heavy_full_file_read_is_refused_with_hint(env):
    task = new_task(env.store)
    env.actions.sizes["/var/log/nginx/access.log"] = 3 * 1024 ** 3
    runtime, _ = _runtime(env, [
        reply(call("shell", {"nodes": ["node1"], "command": "grep -c x /var/log/nginx/access.log"}, "c1")),
        reply(call("finish", {"answer": "未能统计", "confidence": "low"}, "f1")),
    ])
    runtime.run(task.task_id)
    assert not env.actions.shell_calls
    denied = next(p for p in _events(env, task.task_id, "tool_result") if p["tool_call_id"] == "c1")
    assert "tail -c" in str(denied["result"]["error"])


def test_soft_budget_asks_renewal_and_rejection_enters_closing_round(env):
    task = new_task(env.store)
    env.store.update(task.task_id, expected=TaskStatus.RUNNING, tool_turns=60)
    runtime, _ = _runtime(env, [])
    gated = runtime.run(task.task_id)
    assert gated.status is TaskStatus.WAITING_APPROVAL

    approval = _events(env, task.task_id, "approval_requested")[-1]
    env.store.resolve_approval(task.task_id, approval["approval_id"], decision="reject")
    env.store.claim_next("w1", lease_seconds=60)
    runtime, model = _runtime(env, [reply(call("finish", {"answer": "证据不足", "confidence": "low"}, "f1"))])
    done = runtime.run(task.task_id)

    assert model.requests[0][1] == ["finish"]
    assert done.status is TaskStatus.WAITING_APPROVAL  # 零核验证据仍走报告闸


def test_budget_renewal_is_applied_once_across_later_approvals(env):
    task = new_task(env.store)
    env.store.update(task.task_id, expected=TaskStatus.RUNNING, tool_turns=60)
    runtime, _ = _runtime(env, [])
    runtime.run(task.task_id)
    approval = _events(env, task.task_id, "approval_requested")[-1]
    env.store.resolve_approval(task.task_id, approval["approval_id"], decision="approve")
    env.store.claim_next("w1", lease_seconds=60)
    runtime, _ = _runtime(env, [reply(call("finish", {"answer": "证据不足", "confidence": "low"}, "f1"))])
    gated = runtime.run(task.task_id)
    assert gated.status is TaskStatus.WAITING_APPROVAL  # 报告闸:任务还会再次重入

    gate = _events(env, task.task_id, "approval_requested")[-1]
    env.store.resolve_approval(task.task_id, gate["approval_id"], decision="approve")
    env.store.claim_next("w1", lease_seconds=60)
    runtime, _ = _runtime(env, [])
    runtime.run(task.task_id)

    assert env.store.get(task.task_id).budget_grants == 1
    assert len(_events(env, task.task_id, "budget_decision_applied")) == 1


def test_hard_cap_closing_round_without_finish_writes_unanswered_report(env):
    task = new_task(env.store)
    # 续期已放宽到硬上限之上:先采一步证据,下一轮触硬上限进收口回合
    env.store.update(task.task_id, expected=TaskStatus.RUNNING, tool_turns=HARD_TURNS - 1, budget_grants=10)
    runtime, model = _runtime(env, [
        reply(call("shell", {"nodes": ["node1"], "command": "uptime"}, "c1")),
        reply(content="我再看看"),
    ])
    done = runtime.run(task.task_id)

    assert model.requests[1][1] == ["finish"]
    assert done.status is TaskStatus.WAITING_INPUT
    assert done.failure_kind == "closing_round_unanswered"
    assert env.actions.finishes[-1]["verdict"] == "insufficient"
    assert env.actions.finishes[-1]["evidence"] == ["[已采·step:1] [node1] uptime"]


def test_invalid_arguments_three_turns_enter_closing_round(env):
    task = new_task(env.store)
    bad = [reply(call("shell", {}, f"c{index}", error="参数不是合法 JSON")) for index in range(3)]
    runtime, model = _runtime(env, [*bad, reply(call("finish", {"answer": "无法排查", "confidence": "low"}, "f1"))])
    runtime.run(task.task_id)
    assert model.requests[3][1] == ["finish"]
    assert _events(env, task.task_id, "closing_round")[0]["reason"].startswith("连续 3 轮")


def test_text_question_returns_turn_and_waits_for_input(env):
    task = new_task(env.store)
    runtime, _ = _runtime(env, [reply(content="是哪个集群的 node1?")])
    done = runtime.run(task.task_id)
    assert done.status is TaskStatus.WAITING_INPUT and done.tool_turns == 0
    assert _events(env, task.task_id, "agent_decision")[0]["action"] == "needs_input"


def test_new_tasks_run_autonomous_and_evaluation_guided_snapshots_still_load(env):
    task = new_task(env.store)
    assert task.harness_tier == "autonomous"
    # A3.6 评估期的 guided 任务:快照里多出的 guidance 字段忽略,按自主档继续
    env.store.update(task.task_id, expected=TaskStatus.RUNNING, harness_tier="guided", tool_turns=25, harness_switches={
        "tier": "guided", "projection_chars": 32768, "soft_turns": 50, "soft_seconds": 1800,
        "soft_node_commands": 120, "guidance": True})
    runtime, model = _runtime(env, [
        reply(call("shell", {"nodes": ["node1"], "command": "uptime"}, "c1")),
        reply(_finish([("step:1", "node1 ok uptime")])),
    ])
    assert runtime.run(task.task_id).status is TaskStatus.WAITING_INPUT
    assert not _events(env, task.task_id, "budget_reminder")
    assert "排查要点" not in model.requests[0][0][0]["content"]


def test_kb_trigger_turn_only_offers_kb_draft(env):
    task = new_task(env.store, message="请基于当前执行报告生成知识库草稿，等待我确认后再落档。")
    runtime, model = _runtime(env, [reply(content="当前没有执行报告,无法生成草稿")])
    runtime.run(task.task_id)
    assert model.requests[0][1] == ["create_kb_draft"]
