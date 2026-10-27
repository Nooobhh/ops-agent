"""精确告警路由内按任务类型选诊断路径:playbook 只来自本条路由,task_id 只用于选择。"""
from __future__ import annotations

from types import SimpleNamespace

from lib.triage_agent.alert_playbooks import alert_task_type, typed_path_playbooks

PB = "playbooks/alpha/jobs/"
HIT = {
    "playbooks": [{"path": PB + "check_task_failure.sh", "order": 1}],
    "diagnostic_paths": [
        {"id": "default", "order": 1, "playbooks": [{"path": PB + "check_task_failure.sh", "order": 1}]},
        {"id": "sparksql", "order": 2, "task_types": ["SPARKSQL"],
         "playbooks": [{"path": PB + "check_sparksql_failure.sh", "order": 1}]},
        {"id": "jdbc", "order": 3, "task_types": ["MYSQL", "SQLSERVER"],
         "playbooks": [{"path": PB + "check_jdbc_task_failure.sh", "order": 1}]},
    ],
}


def _names(paths):
    return [path.rsplit("/", 1)[-1] for path in paths]


def test_typed_paths_run_matching_type_then_generic():
    select = typed_path_playbooks
    assert _names(select(HIT, "SPARKSQL")) == ["check_sparksql_failure.sh", "check_task_failure.sh"]
    assert _names(select(HIT, "SQLSERVER")) == ["check_jdbc_task_failure.sh", "check_task_failure.sh"]
    assert _names(select(HIT, "DEP")) == ["check_task_failure.sh"]
    assert _names(select(HIT, None)) == [
        "check_task_failure.sh", "check_sparksql_failure.sh", "check_jdbc_task_failure.sh",
    ]
    untyped = {"playbooks": HIT["playbooks"], "diagnostic_paths": HIT["diagnostic_paths"][:1]}
    assert select(untyped, "SPARKSQL") is None            # 未声明 task_types 的路由保持原行为


def test_alert_task_type_reads_ids_from_alert_fields_and_never_guesses():
    calls = []

    def task_route(*, process_id, task_id):
        calls.append((process_id, task_id))
        return {"task_type": "SPARKSQL" if task_id == 13373 else None, "process_id": process_id, "task_id": task_id}

    actions = SimpleNamespace(task_route=task_route)
    task = SimpleNamespace(initial_message="")
    resolve = alert_task_type
    alert = {"annotations": {"processId": "1016", "taskId": "13373"}}
    assert resolve(actions, task, alert)["task_type"] == "SPARKSQL"
    assert calls[-1] == (1016, 13373)
    assert resolve(actions, task, {"annotations": {"taskId": "1"}}) is None          # 拓扑识别不出
    assert resolve(actions, task, {"annotations": {"processId": "1016"}}) is None    # 没有任务号不识别

    def broken(**_kwargs):
        raise RuntimeError("index missing")

    assert resolve(SimpleNamespace(task_route=broken), task, alert) is None
