"""A3.4:每类任务的开场材料、提示片段与端到端收口。"""
from lib.agent_runtime import AgentRuntime
from lib.triage_agent.models import TaskStatus

from .conftest import ScriptedModel, call, new_task, reply


def _run(env, task_id, replies):
    model = ScriptedModel(replies)
    runtime = AgentRuntime(store=env.store, model=model, actions=env.actions, retry_delays=(), sleep=lambda _: None)
    return runtime.run(task_id), model


def _opening(model):
    return model.requests[0][0][-1]["content"]


def _facts(*pairs, answer="已确认"):
    return call("finish", {"answer": answer, "confidence": "high",
                           "facts": [{"ref": ref, "match": match} for ref, match in pairs]}, "f1")


def test_qa_opening_carries_snapshot_and_direct_answer_ends_round(env):
    snapshot = {"available": True, "collected_at": "2026-10-22T10:00:00+08:00", "nodes": [{"node": "node1", "load1": 3.2}]}
    task = new_task(env.store, message="node1 现在负载多少?", task_type="qa", polling_snapshot=snapshot)
    done, model = _run(env, task.task_id, [reply(content="node1 load1=3.2(本地快照,采样 10:00)。")])

    assert done.status is TaskStatus.WAITING_INPUT
    assert "【本地资源快照】采样时间 2026-10-22T10:00:00+08:00" in _opening(model)
    assert "任务类型:问答" in model.requests[0][0][0]["content"]


def test_investigation_opening_lists_context_refs_and_risk_seed(env):
    env.actions.context_pack = {
        "required_reads": [{"doc_id": "cluster/alpha/yarn/nm-memory.md", "title": "NM 内存", "category": "排查",
                            "reason": "服务匹配"}],
        "knowledge_context": {"diagnosis": [{"doc_id": "cluster/alpha/yarn/nm-memory.md"},
                                            {"doc_id": "cluster/alpha/system/swap.md", "title": "Swap"}]},
    }
    seed = {"risk_ref": "risk-1", "metric": "swap_used_pct", "observed": 60}
    task = new_task(env.store, risk_seed=seed)
    done, model = _run(env, task.task_id, [
        reply(call("shell", {"nodes": ["node1"], "command": "free -h"}, "c1")),
        reply(_facts(("step:1", "node1 ok free -h"))),
    ])

    opening = _opening(model)
    assert opening.count("cluster/alpha/yarn/nm-memory.md") == 1 and "cluster/alpha/system/swap.md" in opening
    assert "【资源风险】" in opening and "risk-1" in opening
    assert env.actions.context_requests[0].profile and env.store.get(task.task_id).context_pack_ref
    assert done.status is TaskStatus.WAITING_INPUT and env.actions.finishes[-1]["verdict"] == "conclusive"


def test_alert_prerun_runs_playbooks_in_order_once_and_stops_at_match(env):
    env.actions.route = {"doc": "cluster/alpha/hdfs/dn-dead.md", "playbooks": ["pb/first.yaml", "pb/second.yaml", "pb/third.yaml"]}
    env.actions.playbook_results = {
        "pb/first.yaml": {"verdict": "insufficient", "steps": [{"cmd": "ps -ef", "expect": "running", "pass": False, "actual": "no process"}]},
        "pb/second.yaml": {"verdict": "conclusive", "steps": [
            {"cmd": "grep heartbeat dn.log", "expect": "heartbeat", "pass": True,
             "actual": "2026-10-22 09:58:01 WARN heartbeat took 30012 ms"}]},
    }
    alert = {"alertname": "DataNodeDead", "platform": "alpha", "node": "node1"}
    task = new_task(env.store, task_type="alert", alert_context=alert)
    done, model = _run(env, task.task_id, [reply(_facts(("playbook:2.1", "heartbeat took 30012 ms")))])

    assert env.actions.playbooks_run == ["pb/first.yaml", "pb/second.yaml"]
    opening = _opening(model)
    assert "playbook:2.1 [✓]" in opening and "结论成立" in opening and "DataNodeDead" in opening
    assert env.actions.finishes[-1]["verdict"] == "conclusive"
    assert env.actions.finishes[-1]["evidence"][1] == "  [事实·playbook:2.1] 2026-10-22 09:58:01 WARN heartbeat took 30012 ms"
    assert len([e for e in env.store.events(task.task_id) if e.type == "agent_playbook_result"]) == 2


def test_alert_without_route_lets_model_investigate(env):
    task = new_task(env.store, task_type="alert", alert_context={"alertname": "X", "platform": "alpha", "node": "node1"})
    done, model = _run(env, task.task_id, [reply(content="未命中路由,暂无结论;需要补充告警时间吗?")])
    assert "未命中精确路由" in _opening(model) and done.status is TaskStatus.WAITING_INPUT


def test_artifact_writes_file_and_finishes_without_report_gate(env):
    task = new_task(env.store, task_type="artifact", message="写一个检查 NM 内存的巡检脚本")
    done, model = _run(env, task.task_id, [
        reply(call("create_task_artifact", {"filename": "check_nm.sh", "content": "free -h\n", "purpose": "巡检"}, "a1")),
        reply(call("finish", {"answer": "已生成 check_nm.sh", "confidence": "high"}, "f1")),
    ])
    assert done.status is TaskStatus.WAITING_INPUT
    assert env.store.get(task.task_id).artifact_refs[0].endswith("check_nm.sh")
    assert "任务类型:产物" in model.requests[0][0][0]["content"]


def test_default_type_has_no_materials(env):
    task = new_task(env.store, task_type="default")
    done, model = _run(env, task.task_id, [reply(content="你好,需要排查哪个集群?")])
    assert "【" not in _opening(model).split("\n", 1)[1]
    assert done.status is TaskStatus.WAITING_INPUT


def test_auto_alert_task_prerun_finish_completes_and_writes_worklog(env):
    env.actions.route = {"doc": "cluster/alpha/hdfs/dn-dead.md", "playbook": "pb/second.yaml"}
    env.actions.playbook_results = {"pb/second.yaml": {"verdict": "conclusive", "steps": [
        {"cmd": "grep heartbeat dn.log", "pass": True, "actual": "2026-10-22 09:58:01 WARN heartbeat took 30012 ms"}]}}
    task, _created = env.store.enqueue_auto("occ-1", {"alertname": "DataNodeDead", "platform": "alpha", "host": "node1"})
    env.store.claim_next("w1", lease_seconds=60)
    done, _model = _run(env, task.task_id, [
        reply(content="要确认哪个时间段?"),   # 自动任务不能等待 → 收口回合
        reply(_facts(("playbook:1.1", "heartbeat took 30012 ms"))),
    ])
    assert done.status is TaskStatus.COMPLETED
    assert env.actions.worklogs and env.actions.finishes[-1]["verdict"] == "conclusive"
