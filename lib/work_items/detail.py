"""工作项挂载详情:任务 → 报告/产物/审批/写操作,排查记录 → 报告/产物/写操作,worklog 显式 + 派生。

任务存储按鸭子类型传入(get/events),本模块不依赖 Agent 运行时。
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Mapping, Optional

from lib.datastore import DataStoreError

from .links import LinkIndex, assign_entries, build_index

_APPROVAL_EVENTS = {"approval_requested", "approval_decided"}


def task_run_id_reader(task_store) -> Any:
    def read(task_id: str) -> Optional[str]:
        try:
            return task_store.get(task_id).run_id
        except Exception:  # noqa: BLE001 — 任务目录缺失或损坏时只是无法派生
            return None
    return read


def index_for(items: List[Mapping[str, Any]], task_store) -> LinkIndex:
    return build_index(items, task_run_id_reader(task_store))


def all_assignments(index: LinkIndex, worklogs) -> Dict[str, Any]:
    days = worklogs.available_days()
    if not days:
        return {}
    return assign_entries(index, worklogs.days(days[0], days[-1]))


def resolve(item: Mapping[str, Any], *, task_store, troubleshoot, worklogs,
            index: LinkIndex) -> Dict[str, Any]:
    tasks, runs = [], []
    for link in item.get("links") or []:
        if link["kind"] == "task":
            tasks.append(_task(link, task_store, troubleshoot))
        elif link["kind"] == "troubleshoot":
            runs.append({"link": link, **_run(link["ref"], troubleshoot)})
    entries = []
    for ref, (owner, derived) in all_assignments(index, worklogs).items():
        if owner != item["id"]:
            continue
        day, position = ref.split("#", 1)
        entries.append({"ref": ref, "derived": derived, **_worklog(worklogs, day, int(position))})
    entries.sort(key=lambda entry: entry["ref"])
    return {"tasks": tasks, "troubleshoot": runs, "worklogs": entries}


def _task(link: Mapping[str, Any], task_store, troubleshoot) -> Dict[str, Any]:
    task_id = link["ref"]
    try:
        task = task_store.get(task_id)
    except Exception:  # noqa: BLE001
        return {"link": link, "task_id": task_id, "missing": True}
    approvals: Dict[str, Dict[str, Any]] = {}
    try:
        events = task_store.events(task_id)
    except Exception:  # noqa: BLE001
        events = []
    for event in events:
        if event.type not in _APPROVAL_EVENTS:
            continue
        payload = event.payload
        approval_id = str(payload.get("approval_id") or "")
        if not approval_id:
            continue
        entry = approvals.setdefault(approval_id, {"approval_id": approval_id})
        if event.type == "approval_requested":
            entry.update(kind=payload.get("kind"), requested_at=event.ts)
        else:
            entry.update(decision=payload.get("decision") or ("approve" if payload.get("approved") else "reject"),
                         decided_at=event.ts,
                         decided_by=(payload.get("decided_by") or {}).get("name"))
    result = {
        "link": link,
        "task_id": task_id,
        "title": task.display_title or task.initial_message[:80],
        "status": task.status.value,
        "task_type": task.task_type,
        "created_at": task.created_at,
        "created_by": task.created_by,
        "run_id": task.run_id,
        "approvals": list(approvals.values()),
    }
    if task.run_id:
        result.update(_run(task.run_id, troubleshoot))
    return result


def _run(run_id: str, troubleshoot) -> Dict[str, Any]:
    try:
        session = troubleshoot.session(run_id)
        run_dir = troubleshoot.run_dir(run_id)
    except DataStoreError:
        return {"run_id": run_id, "missing": True}
    if session is None:
        return {"run_id": run_id, "missing": True}
    artifacts = run_dir / "artifacts"
    return {
        "run_id": run_id,
        "trigger": session.get("trigger"),
        "verdict": session.get("verdict"),
        "report_ref": troubleshoot.report_ref(run_id) if (run_dir / "report.md").is_file() else None,
        "artifacts": sorted(path.name for path in artifacts.iterdir() if path.is_file())
        if artifacts.is_dir() else [],
        "write_operations": [
            {key: operation.get(key) for key in
             ("id", "kind", "title", "outcome", "approver", "decided_at", "oplog")}
            for operation in session.get("write_operations") or []
        ],
    }


def _worklog(worklogs, day: str, position: int) -> Dict[str, Any]:
    try:
        entry = worklogs.get(date.fromisoformat(day), position)
    except (IndexError, OSError, ValueError):
        return {"missing": True}
    return {key: entry.get(key) for key in ("status", "category", "title", "source", "author")}
