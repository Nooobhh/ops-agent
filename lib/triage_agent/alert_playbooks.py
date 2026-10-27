"""告警的登记 playbook:按精确路由选路径、逐个执行一次并核对结论。新旧运行时共用。

playbook 只来自 `(platform, alertname)` 精确路由命中的那条文档;task_id 只用于在路由内
选诊断路径,不参与路由。每个 playbook 以固定 invocation_id 执行,重启后不重放。
"""
from __future__ import annotations

import inspect
from typing import Any, Dict, List, Optional, Tuple

from .models import TriageTask


def execution_kwargs(function, execution_id: Optional[str]) -> Dict[str, Any]:
    """Pass execution_id to new gateways without breaking legacy test doubles."""
    if not execution_id:
        return {}
    try:
        parameters = inspect.signature(function).parameters.values()
    except (TypeError, ValueError):
        return {"execution_id": execution_id}
    if any(
        parameter.name == "execution_id"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    ):
        return {"execution_id": execution_id}
    return {}


def typed_path_playbooks(hit: Dict[str, Any], task_type: Optional[str]) -> Optional[List[str]]:
    """精确路由文档按任务类型分诊断路径(path 带 task_types)时,在路由内选路径。

    识别出类型:先跑声明该类型的路径,再跑不限类型的通用路径;识别不出:全部路径按 order。
    playbook 仍只来自本条精确路由,task_id 只用于在路由内选择,不参与路由。
    """
    paths = [item for item in hit.get("diagnostic_paths") or [] if isinstance(item, dict)]
    if not any(item.get("task_types") for item in paths):
        return None
    ordered = sorted(enumerate(paths, start=1), key=lambda pair: (int(pair[1].get("order") or pair[0]), pair[0]))
    if task_type:
        matched = [item for _index, item in ordered if task_type in (item.get("task_types") or [])]
        generic = [item for _index, item in ordered if not item.get("task_types")]
        selected = matched + generic
    else:
        selected = [item for _index, item in ordered]
    result: List[str] = []
    for item in selected:
        refs = sorted(
            enumerate(item.get("playbooks") or [], start=1),
            key=lambda pair: int((pair[1].get("order") if isinstance(pair[1], dict) else None) or pair[0]),
        )
        for _index, ref in refs:
            path = ref.get("path") if isinstance(ref, dict) else ref
            if path and str(path) not in result:
                result.append(str(path))
    return result


def alert_task_type(actions, task: TriageTask, alert: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从告警字段里的 processId/taskId 经任务拓扑识别任务类型;识别不了返回 None(不猜)。"""
    from lib.kb.retrieval.task_route import extract_task_ids

    fields = {**(alert.get("labels") or {}), **(alert.get("annotations") or {})}
    text = "\n".join(
        [f"{key}: {value}" for key, value in fields.items()]
        + [str(alert.get("detail") or ""), str(task.initial_message or "")]
    )
    process_ids, task_ids = extract_task_ids(text)
    resolver = getattr(actions, "task_route", None)
    if not task_ids or not callable(resolver):
        return None
    try:
        route = resolver(
            process_id=int(process_ids[0]) if process_ids else None,
            task_id=int(task_ids[0]),
        )
    except Exception:  # noqa: BLE001 - 识别失败退回按顺序全跑,不阻断告警处理
        return None
    return route if isinstance(route, dict) and route.get("task_type") else None


def ordered_playbooks(hit: Dict[str, Any]) -> List[str]:
    values: List[Tuple[int, str]] = []
    refs = hit.get("playbooks") or []
    if refs:
        for index, ref in enumerate(refs, start=1):
            if isinstance(ref, dict):
                path = ref.get("path")
                order = ref.get("order", index)
            else:
                path, order = ref, index
            if path:
                values.append((int(order or index), str(path)))
    else:
        for path_index, path_item in enumerate(hit.get("diagnostic_paths") or [], start=1):
            if not isinstance(path_item, dict):
                continue
            path_order = int(path_item.get("order") or path_index)
            for ref_index, ref in enumerate(path_item.get("playbooks") or [], start=1):
                if isinstance(ref, dict):
                    path, order = ref.get("path"), ref.get("order", ref_index)
                else:
                    path, order = ref, ref_index
                if path:
                    values.append((path_order * 10000 + int(order or ref_index), str(path)))
    if values:
        return [path for _, path in sorted(values, key=lambda item: (item[0], item[1]))]
    return [str(hit["playbook"])] if hit.get("playbook") else []


def select_playbooks(
    actions, task: TriageTask, alert: Dict[str, Any], hit: Dict[str, Any],
) -> Tuple[List[str], Optional[Dict[str, Any]]]:
    """路由内的 playbook 顺序;带类型路径时附 path_selection(记入 agent_playbook_result)。"""
    playbooks = ordered_playbooks(hit)
    if not any(isinstance(item, dict) and item.get("task_types") for item in hit.get("diagnostic_paths") or []):
        return playbooks, None
    route = alert_task_type(actions, task, alert)
    task_type = route.get("task_type") if route else None
    playbooks = typed_path_playbooks(hit, task_type) or playbooks
    return playbooks, {
        "task_type": task_type,
        "process_id": route.get("process_id") if route else None,
        "task_id": route.get("task_id") if route else None,
        "playbooks": playbooks,
    }


def verify_playbook(playbook: str, result: Dict[str, Any], invocation_id: str) -> Dict[str, Any]:
    steps = result.get("steps") or []
    expected = [str(step.get("expect") or "") for step in steps if isinstance(step, dict) and step.get("expect")]
    actual = [str(step.get("actual") or "") for step in steps if isinstance(step, dict) and step.get("actual")]
    verdict = str(result.get("verdict") or "insufficient")
    matched = verdict == "conclusive" and bool(steps) and all(
        step.get("pass") is not False for step in steps if isinstance(step, dict)
    )
    return {
        "playbook": playbook,
        "expected": expected,
        "actual": actual,
        "matched": matched,
        "verdict": verdict,
        "evidence_refs": [
            f"{invocation_id}-step-{index}"
            for index, step in enumerate(steps, start=1)
            if isinstance(step, dict)
        ],
    }


def execute_playbook(
    store,
    actions,
    invocations,
    task: TriageTask,
    *,
    invocation_id: str,
    playbook: str,
    path_selection: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """执行一个登记 playbook(已完成的不重放),落 agent_playbook_result;返回 (result, verification)。"""
    if store.invocation_completed(task.task_id, invocation_id):
        result = store.invocation_result(task.task_id, invocation_id) or {}
    else:
        store.record_invocation(task.task_id, invocation_id, "pending")
        try:
            result = actions.run_registered_playbook(
                run_id=task.run_id,
                platform=task.platform,
                node=task.node,
                playbook=playbook,
                **execution_kwargs(
                    actions.run_registered_playbook,
                    getattr(task, "execution_id", None),
                ),
            )
        except Exception as exc:
            invocations.record_failure(task.task_id, invocation_id, "run_registered_playbook", exc)
            raise
        store.record_invocation(task.task_id, invocation_id, "done", result)
    verification = verify_playbook(playbook, result, invocation_id)
    store.append_event(
        task.task_id,
        "agent_playbook_result",
        {
            "invocation_id": invocation_id,
            "playbook": playbook,
            "result": result,
            "verification": verification,
            **({"path_selection": path_selection} if path_selection else {}),
        },
    )
    return result, verification
