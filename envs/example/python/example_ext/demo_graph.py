"""演示图谱(graphs 扩展点):一张固定的小作业调度图,两个流程、三个任务与读写的表。

真实环境在这里接自己的调度系统:读流程/任务定义,写 graph-latest.json(带 view 供知识页展示)并返回图。
"""
from __future__ import annotations

from datetime import datetime, timezone

from lib.atomic_file import atomic_write_json

VIEW = {
    "core_kinds": ["process", "task", "table"],
    "default_visible_kinds": ["process", "task"],
    "optional_kinds": ["table"],
    "count_kind": "process",
    "summary_kinds": ["process", "task", "table"],
}


def _node(node_id, kind, name, **properties):
    return {"id": node_id, "kind": kind, "name": name, "platform": "alpha", "properties": properties}


def _edge(source, target, relation):
    return {"id": f"{source}|{relation}|{target}", "source": source, "target": target, "relation": relation}


def collect(context) -> dict:
    nodes = [
        _node("process:alpha:101", "process", "日报汇总"),
        _node("process:alpha:102", "process", "明细导入", freq_type="D"),
        _node("task:alpha:101:1", "task", "汇总计算", task_type="SPARKSQL"),
        _node("task:alpha:102:1", "task", "源库导入", task_type="TRANSFER"),
        _node("task:alpha:102:2", "task", "清洗", task_type="SQL"),
        _node("table:alpha:ods.orders", "table", "ods.orders"),
        _node("table:alpha:dws.daily", "table", "dws.daily"),
    ]
    edges = [
        _edge("process:alpha:101", "task:alpha:101:1", "contains"),
        _edge("process:alpha:102", "task:alpha:102:1", "contains"),
        _edge("process:alpha:102", "task:alpha:102:2", "contains"),
        _edge("task:alpha:102:1", "table:alpha:ods.orders", "writes_to"),
        _edge("task:alpha:101:1", "table:alpha:ods.orders", "reads_from"),
        _edge("task:alpha:101:1", "table:alpha:dws.daily", "writes_to"),
        _edge("process:alpha:101", "process:alpha:102", "depends_on"),
    ]
    now = datetime.now(timezone.utc).isoformat()
    graph = {
        "schema_version": 1, "topology_kind": "jobs", "update_mode": context.mode,
        "generated_at": now, "completed_at": now, "source_fingerprint": "demo-v1",
        "stats": {"processes": 2, "tasks": 3}, "change_summary": {}, "warnings": [],
        "view": {**VIEW, "count": 2}, "nodes": nodes, "edges": edges,
    }
    context.graph_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(context.graph_dir / "graph-latest.json", graph)
    return graph
