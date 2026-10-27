"""worklog 归属派生与周报分桶。"""
from __future__ import annotations

from datetime import date

from lib.work_items.links import assign_entries, build_index, weekly_buckets


def _item(item_id, links=(), **fields):
    return {"id": item_id, "status": "todo", "links": list(links), "history": [], **fields}


def _link(kind, ref):
    return {"kind": kind, "ref": ref}


def test_assignments_explicit_first_then_derived():
    items = [
        _item("wi-a", [_link("task", "tr-1")]),
        _item("wi-b", [_link("troubleshoot", "ts-9"), _link("worklog", "2026-10-22#2")]),
    ]
    index = build_index(items, lambda task_id: {"tr-1": "ts-1"}.get(task_id))
    days = [{"date": "2026-10-22", "entries": [
        {"entry_index": 0, "source": "auto:agent-ops:tr-1:evt-3"},
        {"entry_index": 1, "source": "auto:troubleshoot:ts-1"},
        {"entry_index": 2, "source": "auto:troubleshoot:ts-1"},  # 显式挂到 wi-b 优先
        {"entry_index": 3, "source": "auto:alert:ts-9"},
        {"entry_index": 4, "source": "auto:alert:NodeDown:node3"},
        {"entry_index": 5, "source": "manual:web"},
    ]}]
    assert assign_entries(index, days) == {
        "2026-10-22#0": ("wi-a", True),
        "2026-10-22#1": ("wi-a", True),
        "2026-10-22#2": ("wi-b", False),
        "2026-10-22#3": ("wi-b", True),
    }


def test_weekly_buckets():
    start, end = date(2026, 10, 22), date(2026, 10, 28)
    items = [
        _item("done-now", status="done", closed_at="2026-10-24T10:00:00+08:00"),
        _item("done-before", status="done", closed_at="2026-10-01T10:00:00+08:00"),
        _item("cancelled", status="cancelled", closed_at="2026-10-24T10:00:00+08:00"),
        _item("touched", status="in_progress",
              history=[{"action": "status", "ts": "2026-10-23T09:00:00+08:00"}]),
        _item("scheduled-now", start_date="2026-10-26", due_date="2026-10-30"),
        _item("next-week", due_date="2026-11-03"),
        _item("created-now", history=[{"action": "created", "ts": "2026-10-23T09:00:00+08:00"}]),
        _item("overdue", due_date="2026-10-20"),
        _item("worked", status="waiting"),
        _item("closed-later", status="done", closed_at="2026-11-02T10:00:00+08:00",
              start_date="2026-10-20", due_date="2026-10-30"),
    ]
    buckets = {entry["item"]["id"]: entry for entry in weekly_buckets(
        items, {"2026-10-25#0": ("worked", False)}, start, end)}
    assert {key: value["bucket"] for key, value in buckets.items()} == {
        "done-now": "completed",
        "touched": "in_progress",
        "scheduled-now": "in_progress",
        "next-week": "planned",
        "created-now": "open",
        "overdue": "open",
        "worked": "in_progress",
        "closed-later": "in_progress",
    }
    assert buckets["scheduled-now"]["in_next_period"] and not buckets["touched"]["in_next_period"]
    assert buckets["overdue"]["overdue"] and not buckets["next-week"]["overdue"]
    # 本期没过完:截止在今天之后的不算逾期
    midweek = {entry["item"]["id"]: entry for entry in weekly_buckets(
        [_item("due-friday", due_date="2026-10-27")], {}, start, end, today=date(2026, 10, 24))}
    assert not midweek["due-friday"]["overdue"]
    assert buckets["worked"]["worklog_refs"] == ["2026-10-25#0"]
