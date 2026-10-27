"""调度任务的数据依赖查询:读写了哪些表、同表的上游写入者与下游读取者、流程级依赖。

只读检索快照里环境包 knowledge.assembly 声明的图谱(定义静态推导,不是运行证据)。典型用途:SPARKSQL 间歇
Job aborted 时找「写同表的上游任务」,再用调度库的运行记录比对两边运行时间是否重叠。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from .task_route import assembly, graph_key, group_platform, topology_freshness

MAX_TABLES = 20


def _label() -> str:
    decl = assembly()
    return decl.label if decl is not None else "调度"

MAX_PEERS = 10


def _node(connection: sqlite3.Connection, node_id: str) -> Optional[tuple[str, dict[str, Any]]]:
    row = connection.execute(
        "SELECT name, properties_json FROM topology_nodes WHERE graph_id = ? AND node_id = ?",
        (graph_key(), node_id),
    ).fetchone()
    return (str(row[0]), json.loads(row[1] or "{}")) if row else None


def _targets(connection: sqlite3.Connection, source: str, relation: str) -> list[str]:
    return [str(row[0]) for row in connection.execute(
        "SELECT target FROM topology_edges WHERE graph_id = ? AND source = ? AND relation = ? ORDER BY target",
        (graph_key(), source, relation),
    )]


def _sources(connection: sqlite3.Connection, target: str, relation: str) -> list[str]:
    return [str(row[0]) for row in connection.execute(
        "SELECT source FROM topology_edges WHERE graph_id = ? AND target = ? AND relation = ? ORDER BY source",
        (graph_key(), target, relation),
    )]


def _task_info(connection: sqlite3.Connection, node_id: str) -> dict[str, Any]:
    _kind, _platform, pid, tid = node_id.split(":", 3)
    name, props = _node(connection, node_id) or ("", {})
    process_name, process = _node(connection, f"process:{_platform}:{pid}") or ("", {})
    return {
        "process_id": int(pid), "task_id": int(tid), "task_name": name,
        "task_type": props.get("task_type"), "disabled": str(props.get("disabled")).lower() == "true",
        "process_name": process_name, "process_state": process.get("state"),
        "freq_type": process.get("freq_type"), "schedule_time": process.get("schedule_time"),
    }


def _peers(connection: sqlite3.Connection, table: str, relation: str, self_node: str) -> dict[str, Any]:
    """表另一侧的任务:启用的列出(同流程内的标 same_process,流程内串行一般不会并发),停用的只计数。"""
    self_process = self_node.split(":")[2]
    enabled, disabled = [], 0
    for node_id in _sources(connection, table, relation):
        if not node_id.startswith("task:") or node_id == self_node:
            continue
        info = _task_info(connection, node_id)
        if info["disabled"]:
            disabled += 1
            continue
        info["same_process"] = node_id.split(":")[2] == self_process
        enabled.append(info)
    enabled.sort(key=lambda item: (item["same_process"], item["process_id"], item["task_id"]))
    return {"tasks": enabled[:MAX_PEERS], "total": len(enabled), "disabled": disabled,
            "truncated": len(enabled) > MAX_PEERS}


def task_dependencies(connection: sqlite3.Connection, *, process_id: int,
                      task_id: Optional[int] = None, platform: Optional[str] = None) -> dict[str, Any]:
    platform = platform or group_platform(connection)
    process_node = f"process:{platform}:{process_id}"
    generated_at, freshness_gap = topology_freshness(connection)
    result: dict[str, Any] = {
        "process_id": process_id, "task_id": task_id, "topology_generated_at": generated_at,
        "reference_only": True, "gaps": [freshness_gap] if freshness_gap else [], "tasks": [],
    }
    if _node(connection, process_node) is None:
        result["gaps"].append(f"{_label()} 拓扑未登记流程 {process_id}")
        return result
    if task_id is not None:
        task_nodes = [f"task:{platform}:{process_id}:{task_id}"]
        if _node(connection, task_nodes[0]) is None:
            result["gaps"].append(f"{_label()} 拓扑未登记任务 {process_id}/{task_id}")
            return result
    else:
        task_nodes = [node for node in _targets(connection, process_node, "contains") if node.startswith("task:")]
    process_tasks = set(_targets(connection, process_node, "contains"))
    for node_id in task_nodes:
        reads = _targets(connection, node_id, "reads_from")
        writes = _targets(connection, node_id, "writes_to")
        if task_id is None and not reads and not writes:
            continue
        entry = _task_info(connection, node_id)
        entry["reads"] = [
            {"table": (_node(connection, table) or ("", {}))[0],
             "writers": _peers(connection, table, "writes_to", node_id)}
            for table in reads[:MAX_TABLES]
        ]
        # 定义 SQL 里 WITH 子句的别名(t1、t2)也被解析成表:无库名且两侧都没有任务的去掉
        entry["reads"] = [item for item in entry["reads"] if "." in item["table"] or item["writers"]["total"]]
        entry["writes"] = [
            {"table": (_node(connection, table) or ("", {}))[0],
             "readers": _peers(connection, table, "reads_from", node_id)}
            for table in writes[:MAX_TABLES]
        ]
        entry["tables_truncated"] = len(reads) > MAX_TABLES or len(writes) > MAX_TABLES
        result["tasks"].append(entry)
    upstream = sorted({
        target for task in process_tasks for target in _targets(connection, task, "depends_on")
        if target.startswith("process:")
    })
    downstream = sorted({
        source.split(":")[2] for source in _sources(connection, process_node, "depends_on")
        if source.startswith("task:")
    }, key=int)
    result["depends_on_processes"] = [
        {"process_id": int(item.split(":")[2]), "process_name": (_node(connection, item) or ("", {}))[0]}
        for item in upstream
    ]
    result["depended_by_processes"] = [
        {"process_id": int(pid), "process_name": (_node(connection, f"process:{platform}:{pid}") or ("", {}))[0]}
        for pid in downstream
    ]
    return result
