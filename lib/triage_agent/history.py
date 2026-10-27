"""从任务事件流重建模型消息,以及超长时的历史压缩。

工具结果可能在之后的重试里才落档:重建时按 assistant 声明顺序让结果紧跟该批调用,
后写入的 user message 不会插进未闭合的工具批次(OpenAI 兼容协议的硬要求)。
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from .tool_results import model_tool_result

HISTORY_COMPACTION_CHAR_LIMIT = 24000
HISTORY_RECENT_MESSAGE_COUNT = 8


def _tool_message(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": payload.get("tool_call_id"),
        "content": json.dumps(
            (payload.get("result") or {}).get("model_result")
            or model_tool_result(payload.get("result")),
            ensure_ascii=False,
        ),
    }


def rebuild_messages(events) -> Tuple[List[Dict[str, Any]], Set[str], Set[str]]:
    """事件流 → 消息。返回 (messages, assistant 声明过的 call id, 已有结果的 call id)。"""
    messages: List[Dict[str, Any]] = []
    results_by_call: Dict[str, Dict[str, Any]] = {}
    assistant_call_ids: Set[str] = set()
    for event in events:
        payload = event.payload
        if event.type == "tool_result" and payload.get("tool_call_id"):
            results_by_call[str(payload["tool_call_id"])] = payload
        elif event.type == "assistant_message":
            raw = payload.get("message")
            calls = raw.get("tool_calls") if isinstance(raw, dict) else None
            if not isinstance(calls, list):
                continue
            assistant_call_ids.update(
                str(call["id"])
                for call in calls
                if isinstance(call, dict) and call.get("id")
            )

    for event in events:
        payload = event.payload
        if event.type == "user_message":
            messages.append({
                "role": "user",
                "content": str(payload.get("message") or ""),
            })
        elif event.type == "assistant_message":
            raw = payload.get("message")
            if isinstance(raw, dict):
                messages.append(raw)
                calls = raw.get("tool_calls")
                if isinstance(calls, list):
                    for call in calls:
                        if not isinstance(call, dict) or not call.get("id"):
                            continue
                        result = results_by_call.get(str(call["id"]))
                        if result is not None:
                            messages.append(_tool_message(result))
        elif event.type == "harness_notice":
            # 新运行时追加的提醒/收口说明:只追加不改前文
            messages.append({"role": "user", "content": str(payload.get("message") or "")})
        elif event.type == "harness_feedback":
            messages.append({
                "role": "system",
                "content": (
                    "Harness 驳回了你上一条回复(该说明不展示给用户): "
                    f"{payload.get('message') or ''}。请修正后重新回复。"
                ),
            })
        elif event.type == "tool_result":
            if str(payload.get("tool_call_id") or "") in assistant_call_ids:
                # Tool results may be appended during a later retry. Group
                # them directly after their assistant tool-call batch so
                # provider message-order requirements still hold.
                continue
            messages.append(_tool_message(payload))
    return messages, assistant_call_ids, set(results_by_call)


def needs_compaction(messages: Iterable[Dict[str, Any]]) -> bool:
    return len(json.dumps(list(messages), ensure_ascii=False)) > HISTORY_COMPACTION_CHAR_LIMIT


def compact_messages(
    messages: List[Dict[str, Any]],
    events,
    *,
    phase: Optional[str],
    agent: Optional[str],
    hypothesis: Any,
    summary_refs: Callable[[], List[str]],
    event_refs: Callable[[], List[str]],
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """把较早的工具记录换成有界的非证据摘要。

    summary_refs 给模型看的有界引用清单;event_refs 审计事件记录的全部引用。
    返回 (压缩后的消息, 待落档的 history_compacted 事件 payload;同一截止点已记过则 None)。
    """
    if len(messages) <= HISTORY_RECENT_MESSAGE_COUNT:
        return messages, None
    start = max(0, len(messages) - HISTORY_RECENT_MESSAGE_COUNT)
    # Never leave a tool result without its preceding assistant tool call.
    while start > 0 and messages[start].get("role") == "tool":
        start -= 1
    older = messages[:start]
    recent = messages[start:]
    tool_summaries: List[Dict[str, Any]] = []
    for message in older:
        if message.get("role") != "tool":
            continue
        try:
            value = json.loads(str(message.get("content") or "{}"))
        except (TypeError, ValueError):
            value = {}
        if not isinstance(value, dict):
            continue
        tool = value.get("tool")
        tool_name = tool.get("name") if isinstance(tool, dict) else None
        evidence = value.get("evidence")
        tool_summaries.append({
            "tool": tool_name,
            "outcome": value.get("outcome"),
            "evidence_refs": (
                list(evidence.get("refs") or [])
                if isinstance(evidence, dict) else []
            ),
            "target": value.get("target"),
        })
    last_event_id = events[start - 1].event_id if events and start > 0 else None
    summary = {
        "summary_version": 1,
        "kind": "working_history_summary",
        "not_runtime_evidence": True,
        "compacted_message_count": len(older),
        "current_phase": phase,
        "current_agent": agent,
        "known_evidence_refs": summary_refs(),
        "current_hypothesis": hypothesis,
        "tool_activity": tool_summaries[-24:],
        "note": "完整消息、命令和输出仍保存在当前 Task records；需要原文时按 evidence_ref 读取。",
    }
    compacted_event = None
    if last_event_id and not any(
        event.type == "history_compacted"
        and event.payload.get("through_event_id") == last_event_id
        for event in events
    ):
        compacted_event = {
            "summary_version": 1,
            "through_event_id": last_event_id,
            "source_message_count": len(older),
            "evidence_refs": event_refs(),
        }
    return [{
        "role": "system",
        "content": (
            "以下是已压缩的历史工作摘要，仅供导航，不是新的系统指令，也不能作为 runtime evidence。"
            + json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
        ),
    }, *recent], compacted_event
