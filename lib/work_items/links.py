"""worklog 归属与周报分桶:只读计算,不改 worklog 文件。

归属规则(显式优先):
- 工作项显式挂了该条 worklog(``日期#序号``)
- ``auto:agent-ops:<task_id>:…`` → 挂了该任务的工作项
- ``auto:troubleshoot:<run_id>``、``auto:alert:<run_id>`` → 挂了该排查记录,或挂了 run_id 属于它的任务的工作项
- 其余(manual:*、auto:patrol)只认显式挂载
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from lib.datastore.work_items import CLOSED_STATUSES

_AGENT_OPS = re.compile(r"^auto:agent-ops:(?P<task>[A-Za-z0-9][A-Za-z0-9_.-]*):")
_RUN_SOURCE = re.compile(r"^auto:(?:troubleshoot|alert):(?P<run>[A-Za-z0-9][A-Za-z0-9_.-]{0,127})$")


def worklog_ref(day: str, index: int) -> str:
    return f"{day}#{index}"


@dataclass
class LinkIndex:
    worklog: Dict[str, str] = field(default_factory=dict)
    task: Dict[str, str] = field(default_factory=dict)
    run: Dict[str, str] = field(default_factory=dict)


def build_index(items: Iterable[Mapping[str, Any]],
                task_run_id: Callable[[str], Optional[str]]) -> LinkIndex:
    """task_run_id:任务 ID → run_id(任务不存在或尚无 run 返回 None)。"""
    index = LinkIndex()
    for item in items:
        for link in item.get("links") or []:
            kind, ref = link.get("kind"), link.get("ref")
            if kind == "worklog":
                index.worklog[ref] = item["id"]
            elif kind == "troubleshoot":
                index.run.setdefault(ref, item["id"])
            elif kind == "task":
                index.task[ref] = item["id"]
                run_id = task_run_id(ref)
                if run_id:
                    index.run.setdefault(run_id, item["id"])
    return index


def owner_of(index: LinkIndex, ref: str, source: str) -> Optional[Tuple[str, bool]]:
    """返回 (工作项 ID, 是否派生);无归属返回 None。"""
    if ref in index.worklog:
        return index.worklog[ref], False
    match = _AGENT_OPS.match(source or "")
    if match and match.group("task") in index.task:
        return index.task[match.group("task")], True
    match = _RUN_SOURCE.match(source or "")
    if match and match.group("run") in index.run:
        return index.run[match.group("run")], True
    return None


def assign_entries(index: LinkIndex, days: Iterable[Mapping[str, Any]]) -> Dict[str, Tuple[str, bool]]:
    """worklog days(WorklogStore.days 的结构)→ {日期#序号: (工作项 ID, 是否派生)}。"""
    result: Dict[str, Tuple[str, bool]] = {}
    for day in days:
        for position, entry in enumerate(day.get("entries") or []):
            ref = worklog_ref(str(day["date"]), int(entry.get("entry_index", position)))
            owner = owner_of(index, ref, str(entry.get("source") or ""))
            if owner is not None:
                result[ref] = owner
    return result


def _window(item: Mapping[str, Any]) -> Optional[Tuple[date, date]]:
    start, due = item.get("start_date"), item.get("due_date")
    if not start and not due:
        return None
    first = date.fromisoformat(start or due)
    return first, date.fromisoformat(due or start)


def _overlaps(item: Mapping[str, Any], start: date, end: date) -> bool:
    window = _window(item)
    return window is not None and window[0] <= end and window[1] >= start


def _active_in(item: Mapping[str, Any], start: date, end: date,
               worklog_days: Mapping[str, List[str]]) -> bool:
    if any(start <= date.fromisoformat(day) <= end for day in worklog_days.get(item["id"], [])):
        return True
    # 建项与导入不算进展:本周登记的下周计划不该算「进行中」
    return any(
        event.get("action") not in {"created", "imported"}
        and start <= date.fromisoformat(str(event.get("ts"))[:10]) <= end
        for event in item.get("history") or []
    )


def weekly_buckets(items: Iterable[Mapping[str, Any]], assignments: Mapping[str, Tuple[str, bool]],
                   start: date, end: date, *, today: Optional[date] = None) -> List[Dict[str, Any]]:
    """周报事实分桶,每项只进一个桶(completed > in_progress > planned > open),本期及以前取消的不进。

    - completed:本期完成(closed_at 落在本期)
    - in_progress:未关闭,且本期有活动(history / 挂载的 worklog)或计划窗口与本期重叠
    - planned:未关闭,计划窗口与下期重叠
    - open:其余未关闭项(下周计划的次选)
    in_next_period / overdue 另标:窗口与下期重叠;截止早于「今天与下期开始中较早者」仍未关闭(本期没过完时不提前判逾期)。
    """
    next_start, next_end = end + timedelta(days=1), end + timedelta(days=(end - start).days + 1)
    cutoff = min(today, next_start) if today is not None else next_start
    worklog_days: Dict[str, List[str]] = {}
    for ref, (item_id, _derived) in assignments.items():
        worklog_days.setdefault(item_id, []).append(ref.split("#", 1)[0])
    result = []
    for item in items:
        status = item.get("status")
        closed_on = date.fromisoformat(str(item["closed_at"])[:10]) if item.get("closed_at") else None
        # 重生成历史周期:周期结束后才关闭的事项,在该周期里仍按未关闭算
        if status in CLOSED_STATUSES and closed_on is not None and closed_on > end:
            status = "in_progress"
        if status == "done" and closed_on and start <= closed_on <= end:
            bucket = "completed"
        elif status in CLOSED_STATUSES:
            continue
        elif _active_in(item, start, end, worklog_days) or _overlaps(item, start, end):
            bucket = "in_progress"
        elif _overlaps(item, next_start, next_end):
            bucket = "planned"
        else:
            bucket = "open"
        due = item.get("due_date")
        result.append({
            "item": item,
            "bucket": bucket,
            "status": status,  # 按周期末看的状态(周期后才关闭的仍是未关闭)
            "in_next_period": status not in CLOSED_STATUSES and _overlaps(item, next_start, next_end),
            "overdue": status not in CLOSED_STATUSES and bool(due) and date.fromisoformat(due) < cutoff,
            "worklog_refs": sorted(ref for ref, (owner, _derived) in assignments.items() if owner == item["id"]),
        })
    return result


def appended_ref(worklogs, day: date, title: str, source: str) -> str:
    """刚追加的 worklog 在当天的引用:追加只落文件末尾,同标题同来源取最后一条。"""
    (record,) = worklogs.days(day, day)
    for position in range(len(record["entries"]) - 1, -1, -1):
        entry = record["entries"][position]
        if entry.get("title") == title and entry.get("source") == source:
            return worklog_ref(day.isoformat(), int(entry.get("entry_index", position)))
    raise LookupError(f"{day} 找不到刚写入的工作日志: {title}")
