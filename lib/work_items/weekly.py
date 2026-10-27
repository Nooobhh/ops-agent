"""周报的工作项事实:代码只按状态与日期分桶、挂上 worklog 来源 ID,不写任何周报句子。

只取 completed / in_progress / planned 三桶;其余未关闭项(open)不进周报输入,免得把久未动的事项写成进展。
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .detail import index_for
from .links import assign_entries, weekly_buckets

DESCRIPTION_LIMIT = 2000
REPORT_BUCKETS = ("completed", "in_progress", "planned")


def _clip(value: Optional[str]) -> Optional[str]:
    text = str(value or "").strip()
    if len(text) <= DESCRIPTION_LIMIT:
        return text or None
    return text[:DESCRIPTION_LIMIT].rstrip() + "\n[描述因上下文预算截断]"


def _notes_in(item: Mapping[str, Any], start: date, end: date) -> List[str]:
    return [
        str(event["note"]) for event in item.get("history") or []
        if event.get("note") and start <= date.fromisoformat(str(event.get("ts"))[:10]) <= end
    ]


def weekly_facts(datastore, task_store, start: date, end: date, days: List[Mapping[str, Any]], *,
                 today: date) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """返回 (工作项证据, worklog 来源 ID → 工作项来源 ID)。没有进周报的工作项时两者皆空。"""
    store = datastore.work_items
    items = store.list()
    if not items:
        return [], {}
    assignments = assign_entries(index_for(items, task_store), days)
    plans = {plan["id"]: plan["title"] for plan in store.list_plans()}
    evidence: List[Dict[str, Any]] = []
    for entry in weekly_buckets(items, assignments, start, end, today=today):
        if entry["bucket"] not in REPORT_BUCKETS:
            continue
        item = entry["item"]
        evidence.append({
            "source_id": f"wi:{item['id']}",
            "bucket": entry["bucket"],
            "title": item["title"],
            "description": _clip(item.get("description")),
            "status": entry["status"],
            "kind": item.get("kind") or "task",
            "priority": item.get("priority"),
            "plan": plans.get(item.get("plan_id")),
            "plan_node": item.get("plan_node"),
            "start_date": item.get("start_date"),
            "due_date": item.get("due_date"),
            "closed_at": item.get("closed_at") if entry["bucket"] == "completed" else None,
            "in_next_period": entry["in_next_period"],
            "overdue": entry["overdue"],
            "progress_notes": _notes_in(item, start, end),
            "worklog_source_ids": entry["worklog_refs"],
        })
    included = {fact["source_id"] for fact in evidence}
    entry_map = {
        ref: f"wi:{item_id}" for ref, (item_id, _derived) in assignments.items()
        if f"wi:{item_id}" in included
    }
    return evidence, entry_map
