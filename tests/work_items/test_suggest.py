"""匹配建议:只认输入里出现过的日志与工作项,模型编造或重复的建议丢弃。"""
from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest

from lib.work_items.suggest import candidates, suggest, unassigned_entries


class FakeClient:
    def __init__(self, reply):
        self.reply = reply
        self.messages = None

    def complete(self, messages, tools):
        self.messages = messages
        return SimpleNamespace(content=self.reply)


ITEMS = [{"id": "wi-1", "title": "MySQL 复制修复", "status": "in_progress", "plan_id": None}]
ENTRIES = [{"worklog_ref": "2026-10-20#0", "title": "修复 111→112 复制断链"},
           {"worklog_ref": "2026-10-20#1", "title": "开会"}]


def test_suggest_keeps_only_known_refs():
    reply = json.dumps({"matches": [
        {"worklog_ref": "2026-10-20#0", "work_item_id": "wi-1", "reason": "同一 MySQL 复制问题"},
        {"worklog_ref": "2026-10-20#0", "work_item_id": "wi-1", "reason": "重复"},
        {"worklog_ref": "2026-10-20#9", "work_item_id": "wi-1", "reason": "编造的日志"},
        {"worklog_ref": "2026-10-20#1", "work_item_id": "wi-404", "reason": "编造的工作项"},
    ]}, ensure_ascii=False)
    client = FakeClient(f"```json\n{reply}\n```")
    assert suggest(client, ENTRIES, ITEMS, {}) == [{
        "worklog_ref": "2026-10-20#0", "work_item_id": "wi-1",
        "work_item_title": "MySQL 复制修复", "reason": "同一 MySQL 复制问题",
    }]
    assert client.messages[1]["role"] == "user" and "MySQL 复制修复" in client.messages[1]["content"]
    with pytest.raises(ValueError, match="有效 JSON"):
        suggest(FakeClient("不是 JSON"), ENTRIES, ITEMS, {})
    assert suggest(FakeClient("{}"), [], ITEMS, {}) == []


def test_candidates_and_unassigned_entries():
    items = [
        {"id": "a", "status": "todo"},
        {"id": "b", "status": "done", "closed_at": "2026-10-21T10:00:00+08:00"},
        {"id": "c", "status": "done", "closed_at": "2026-09-01T10:00:00+08:00"},
        {"id": "d", "status": "cancelled"},
    ]
    assert [item["id"] for item in candidates(items, date(2026, 10, 15), date(2026, 10, 21))] == ["a", "b"]
    days = [{"date": "2026-10-20", "entries": [
        {"entry_index": 0, "title": "x"}, {"entry_index": 1, "title": "y"},
        {"entry_index": 2, "title": "z", "weekly_report_excluded": True},
    ]}]
    assert [entry["worklog_ref"] for entry in unassigned_entries(days, {"2026-10-20#0": ("a", False)})] == ["2026-10-20#1"]
