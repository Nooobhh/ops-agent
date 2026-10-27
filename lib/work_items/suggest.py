"""worklog → 工作项的匹配建议:模型只给建议与理由,人勾选确认后才经挂载接口写入。"""
from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Dict, List, Mapping

from lib.datastore.work_items import CLOSED_STATUSES

MAX_ENTRIES = 60
MAX_CANDIDATES = 80
REASON_MAX = 200
SYSTEM = (
    "你是运维工作记录归类助手。工作项是要做的事,工作日志是发生了什么。"
    "把每条工作日志匹配到最可能对应的一个工作项;没有明显对应的不要匹配,宁缺毋滥。"
    "只依据输入里的标题、过程、产出与工作项标题、描述、计划节点、排期判断。"
    "只返回 JSON 对象 {\"matches\": [{\"worklog_ref\": \"日期#序号\", \"work_item_id\": \"wi-…\", "
    "\"reason\": \"一句话依据\"}]},不要 Markdown。"
)


def candidates(items: List[Mapping[str, Any]], start: date, end: date) -> List[Mapping[str, Any]]:
    """未关闭项与本期内关闭的完成项;取消的不参与。"""
    result = []
    for item in items:
        status = item.get("status")
        if status == "cancelled":
            continue
        closed_on = str(item.get("closed_at") or "")[:10]
        if status in CLOSED_STATUSES and not (closed_on and start.isoformat() <= closed_on <= end.isoformat()):
            continue
        result.append(item)
    return result[:MAX_CANDIDATES]


def unassigned_entries(days: List[Mapping[str, Any]], assigned: Mapping[str, Any]) -> List[Dict[str, Any]]:
    entries = []
    for day in days:
        for position, entry in enumerate(day.get("entries") or []):
            ref = f"{day['date']}#{entry.get('entry_index', position)}"
            if ref in assigned or entry.get("weekly_report_excluded"):
                continue
            entries.append({
                "worklog_ref": ref, "title": entry.get("title"), "status": entry.get("status"),
                "category": entry.get("category"), "output": entry.get("output"),
                "related": entry.get("related"), "process": entry.get("process") or [],
            })
    return entries[:MAX_ENTRIES]


def suggest(client, entries: List[Mapping[str, Any]], items: List[Mapping[str, Any]],
            plans: Mapping[str, str]) -> List[Dict[str, Any]]:
    if not entries or not items:
        return []
    payload = {
        "worklogs": entries,
        "work_items": [{
            "work_item_id": item["id"], "title": item.get("title"),
            "description": str(item.get("description") or "")[:300] or None,
            "plan": plans.get(item.get("plan_id")), "plan_node": item.get("plan_node"),
            "start_date": item.get("start_date"), "due_date": item.get("due_date"),
            "status": item.get("status"),
        } for item in items],
    }
    reply = client.complete([
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ], [])
    text = str(reply.content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("匹配建议未返回有效 JSON") from exc
    matches = parsed.get("matches") if isinstance(parsed, dict) else None
    if not isinstance(matches, list):
        raise ValueError("匹配建议缺少 matches")
    known_refs = {entry["worklog_ref"] for entry in entries}
    known_items = {item["id"]: item for item in items}
    result, seen = [], set()
    # 模型编出来的引用、重复给同一条日志的建议一律丢弃
    for match in matches:
        if not isinstance(match, dict):
            continue
        ref, item_id = str(match.get("worklog_ref") or ""), str(match.get("work_item_id") or "")
        if ref not in known_refs or item_id not in known_items or ref in seen:
            continue
        seen.add(ref)
        result.append({
            "worklog_ref": ref,
            "work_item_id": item_id,
            "work_item_title": known_items[item_id].get("title"),
            "reason": str(match.get("reason") or "").strip()[:REASON_MAX],
        })
    return result
