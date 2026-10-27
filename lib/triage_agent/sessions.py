"""任务与排查记录(records session)的绑定:首个节点动作前建 session,报告路径随之暴露。"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from lib.datastore import open_datastore

from .errors import OrchestrationError
from .models import TaskSource, TaskStatus, TriageTask


def _survey_fields(actions, task: TriageTask, node: str) -> Dict[str, Any]:
    # 集群级调查落 survey session:record 契约要求 node == nodes[0]
    if task.scope != "cluster":
        return {}
    known = actions.known_nodes(task.platform)
    return {
        "session_type": "survey",
        "nodes": [node, *[item for item in known if item != node]],
    }


def ensure_session(
    store,
    actions,
    task: TriageTask,
    *,
    service: str = "",
    node: Optional[str] = None,
) -> TriageTask:
    """已有 run_id 时补齐 workspace 的平台/节点;否则新建排查 session 并写回任务。

    node 缺省取任务节点;集群级任务的入口节点可由调用方给出(首个动作的节点)。
    """
    node = node or task.node
    if task.run_id:
        data = actions.session_data(task.run_id)
        if data.get("platform") == "local" and task.platform and node:
            actions.update_session_context(
                task.run_id,
                platform=task.platform,
                node=node,
                service=str((task.alert_event or {}).get("service") or service),
                **_survey_fields(actions, task, node),
            )
        return task
    if not task.platform or not node:
        raise OrchestrationError("节点动作前必须确定 platform 和 node")
    trigger = (
        str((task.alert_event or {}).get("alertname") or task.initial_message)
        if task.source is TaskSource.AUTO
        else f"manual:{task.initial_message}"
    )
    session = actions.start_session(
        platform=task.platform,
        node=node,
        trigger=trigger,
        service=service or str((task.alert_event or {}).get("service") or ""),
        author=task.created_by,
        **_survey_fields(actions, task, node),
    )
    return store.update(task.task_id, expected=TaskStatus.RUNNING, run_id=session.run_id)


def sync_live_report(
    store,
    actions,
    task: TriageTask,
    display_path: Callable[[Any], str],
) -> TriageTask:
    """Expose the report as soon as the first server action is recorded."""
    if not task.run_id:
        return task
    paths = getattr(actions, "paths", None)
    if paths is None:
        return task
    records_root = getattr(paths, "records_root", None) or (
        open_datastore(paths.repo_root).troubleshoot.root
    )
    report = records_root / task.run_id / "report.md"
    if not report.is_file():
        return task
    return store.update(
        task.task_id,
        expected=TaskStatus.RUNNING,
        report_path=display_path(report),
    )
