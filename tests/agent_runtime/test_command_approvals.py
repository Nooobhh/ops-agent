"""命令放行与人工裁决(底座 CommandApprovals)经新运行时的端到端行为。

A3.7 删除旧编排器后,原 tests/triage_agent/test_command_gate.py 里与安全相关的用例在此改由
新运行时驱动:永久名单、三档、审核模型、自动任务无人值守、四种裁决范围、签名一致与崩溃不重放。
"""
import json

import pytest

from lib.agent_runtime import AgentRuntime
from lib.triage_agent.command_policy import CommandPolicy
from lib.triage_agent.command_review import ReviewVerdict
from lib.triage_agent.models import TaskStatus

from .conftest import ScriptedModel, call, new_task, reply

UNREGISTERED = "foo-tool -x"


class StubReviewer:
    def __init__(self, safe):
        self.safe = safe
        self.reviewed = []

    def review(self, *, command, signature, context=None):
        self.reviewed.append(command)
        return ReviewVerdict(safe=self.safe, reason="stub")


def _policy(env, mode="whitelist", allow=(), deny=()):
    path = env.tmp / "command-policy.json"
    path.write_text(json.dumps({"mode": mode, "allow": list(allow), "deny": list(deny)}), encoding="utf-8")
    return CommandPolicy(path)


def _runtime(env, replies, policy, reviewer=None):
    return AgentRuntime(store=env.store, model=ScriptedModel(replies), actions=env.actions, retry_delays=(),
                        sleep=lambda _: None, command_policy=policy, command_reviewer=reviewer)


def _shell(command, call_id="c1"):
    return reply(call("shell", {"nodes": ["node1"], "command": command}, call_id))


def _done():
    return reply(content="已看完。")


def _result(env, task_id, call_id):
    return next(event.payload["result"] for event in env.store.events(task_id)
                if event.type == "tool_result" and event.payload["tool_call_id"] == call_id)


def _approval(env, task_id):
    return [event.payload for event in env.store.events(task_id) if event.type == "approval_requested"][-1]


def _suspend(env, policy, command=UNREGISTERED, **task_fields):
    task = new_task(env.store, **task_fields)
    status = _runtime(env, [_shell(command)], policy).run(task.task_id).status
    assert status is TaskStatus.WAITING_APPROVAL
    return task, _approval(env, task.task_id)


def _decide_and_resume(env, policy, task, approval, *, decision="approve", scope="once", replies=None):
    env.store.resolve_approval(task.task_id, approval["approval_id"], decision=decision, scope=scope)
    env.store.claim_next("w1", lease_seconds=60)
    return _runtime(env, replies or [_done()], policy).run(task.task_id)


def test_permanent_allow_list_runs_without_asking(env):
    task = new_task(env.store)
    _runtime(env, [_shell(UNREGISTERED), _done()], _policy(env, allow=[UNREGISTERED])).run(task.task_id)
    assert [cmd for _node, cmd, _approved in env.actions.shell_calls] == [UNREGISTERED]


def test_permanent_deny_list_is_returned_to_model_without_suspending(env):
    task = new_task(env.store)
    done = _runtime(env, [_shell(UNREGISTERED), _done()], _policy(env, deny=[UNREGISTERED])).run(task.task_id)
    assert done.status is TaskStatus.WAITING_INPUT and not env.actions.shell_calls
    assert _result(env, task.task_id, "c1")["ok"] is False


def test_bypass_runs_unregistered_but_not_writes_or_credentials(env):
    policy = _policy(env, mode="bypass")
    task = new_task(env.store)
    _runtime(env, [_shell(UNREGISTERED), _done()], policy).run(task.task_id)
    assert [cmd for _node, cmd, _approved in env.actions.shell_calls] == [UNREGISTERED]
    for command in ("rm -f /tmp/x", "cat /etc/shadow"):
        _task, approval = _suspend(env, policy, command)
        assert approval["kind"] == "command" and approval["command"] == command


def test_task_mode_overrides_global_mode_without_touching_policy_file(env):
    policy = _policy(env)
    before = policy.path.read_text(encoding="utf-8")
    task = new_task(env.store, command_mode="bypass")
    _runtime(env, [_shell(UNREGISTERED), _done()], policy).run(task.task_id)
    assert env.actions.shell_calls and policy.path.read_text(encoding="utf-8") == before


def test_auto_mode_reviewer_releases_only_what_it_judges_safe(env):
    safe = StubReviewer(True)
    task = new_task(env.store)
    _runtime(env, [_shell(UNREGISTERED), _done()], _policy(env, mode="auto"), safe).run(task.task_id)
    assert safe.reviewed == [UNREGISTERED] and env.actions.shell_calls

    unsafe = StubReviewer(False)
    task = new_task(env.store)
    status = _runtime(env, [_shell(UNREGISTERED)], _policy(env, mode="auto"), unsafe).run(task.task_id).status
    assert status is TaskStatus.WAITING_APPROVAL


def test_reviewer_never_sees_writes_credentials_or_allow_listed_commands(env):
    reviewer = StubReviewer(True)
    policy = _policy(env, mode="auto", allow=[UNREGISTERED])
    for command in ("rm -f /tmp/x", "cat /etc/shadow"):
        task = new_task(env.store)
        status = _runtime(env, [_shell(command)], policy, reviewer).run(task.task_id).status
        assert status is TaskStatus.WAITING_APPROVAL
    task = new_task(env.store)
    _runtime(env, [_shell(UNREGISTERED), _done()], policy, reviewer).run(task.task_id)
    assert reviewer.reviewed == []


def test_auto_task_is_rejected_instead_of_waiting_for_a_human(env):
    env.store.enqueue_auto("occ-1", {"alertname": "X", "platform": "alpha", "host": "node1"})
    task = env.store.claim_next("w1", lease_seconds=60)
    env.store.update(task.task_id, expected=TaskStatus.RUNNING, task_type="default", platform="alpha", node="node1")
    runtime = _runtime(env, [_shell(UNREGISTERED), reply(call("finish", {"answer": "命令未放行", "confidence": "low"}, "f1"))],
                       _policy(env))
    runtime.run(task.task_id)
    assert not env.actions.shell_calls
    assert "自动任务" in json.dumps(_result(env, task.task_id, "c1"), ensure_ascii=False)


def test_once_approval_runs_original_call_with_flag_and_does_not_cover_the_next(env):
    policy = _policy(env)
    task, approval = _suspend(env, policy, "rm -f /tmp/x")
    done = _decide_and_resume(env, policy, task, approval, replies=[_shell("rm -f /tmp/x", "c2")])
    assert env.actions.shell_calls == [("node1", "rm -f /tmp/x", True)]
    assert done.status is TaskStatus.WAITING_APPROVAL  # 同样的命令再来一次仍要人批
    assert "always" not in policy.path.read_text(encoding="utf-8") and policy.load().allow == []


@pytest.mark.parametrize("decision,listed", [("approve", "allow"), ("reject", "deny")])
def test_always_scope_writes_the_permanent_list(env, decision, listed):
    policy = _policy(env)
    task, approval = _suspend(env, policy)
    _decide_and_resume(env, policy, task, approval, decision=decision, scope="always")
    assert getattr(policy.load(), listed) == [UNREGISTERED]


def test_rejection_is_returned_to_model_without_running(env):
    policy = _policy(env)
    task, approval = _suspend(env, policy)
    _decide_and_resume(env, policy, task, approval, decision="reject")
    assert not env.actions.shell_calls
    assert _result(env, task.task_id, "c1")["ok"] is False


def test_signature_mismatch_refuses_to_execute(env):
    policy = _policy(env)
    task, approval = _suspend(env, policy)
    env.store.resolve_approval(task.task_id, approval["approval_id"], decision="approve", scope="once")
    claimed = env.store.claim_next("w1", lease_seconds=60)
    _runtime(env, [], policy)._execute_approved_shell(claimed, "c1", "cat")
    assert not env.actions.shell_calls
    assert "签名不一致" in json.dumps(_result(env, task.task_id, "c1"), ensure_ascii=False)


class _WorkerCrash(BaseException):
    pass


def test_crash_during_approved_execution_is_not_replayed(env):
    """执行中途 worker 崩溃:接管时该调用补成中断结果,批准不会被二次兑现。"""
    policy = _policy(env)
    task, approval = _suspend(env, policy)
    env.store.resolve_approval(task.task_id, approval["approval_id"], decision="approve", scope="once")
    executed = env.actions.run_shell

    def crash_after_running(*args, **kwargs):
        executed(*args, **kwargs)
        raise _WorkerCrash()

    env.actions.run_shell = crash_after_running
    with pytest.raises(_WorkerCrash):
        _runtime(env, [], policy).run(env.store.claim_next("w1", lease_seconds=60).task_id)
    env.actions.run_shell = executed
    env.store.update(task.task_id, expected=TaskStatus.RUNNING, lease_expires_at="2000-01-01T00:00:00+00:00")

    _runtime(env, [_done()], policy).run(env.store.claim_next("w1", lease_seconds=60).task_id)

    assert len(env.actions.shell_calls) == 1
    results = [event.payload for event in env.store.events(task.task_id)
               if event.type == "tool_result" and event.payload["tool_call_id"] == "c1"]
    assert len(results) == 1 and "中断" in json.dumps(results[0], ensure_ascii=False)
