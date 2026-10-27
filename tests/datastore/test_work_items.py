"""工作项与计划的本地存储:字段校验、状态与 history、挂载唯一、告警去重、计划导入。"""
from __future__ import annotations

import json

import pytest

from lib.datastore import DataStoreError, WorkItemConflict, WorkItemNotFound
from lib.datastore.local import LocalDataStore


@pytest.fixture
def items(tmp_path):
    return LocalDataStore(tmp_path / "data").work_items


def test_create_defaults_and_files(items, tmp_path):
    item = items.create({"title": "MySQL 3307 复制修复", "due_date": "2026-10-30"}, author="alice")
    assert item["id"].startswith("wi-")
    assert (item["source"], item["status"], item["priority"], item["kind"]) == ("manual", "todo", "normal", "task")
    assert item["assignee"] == "alice" and item["created_by"] == "alice"
    assert item["history"][0]["action"] == "created"
    path = tmp_path / "data" / "records" / "work-items" / f"{item['id']}.json"
    assert json.loads(path.read_text(encoding="utf-8"))["title"] == "MySQL 3307 复制修复"
    assert items.get(item["id"]) == item


@pytest.mark.parametrize("fields, message", [
    ({"title": ""}, "标题"),
    ({"title": "a\nb"}, "换行"),
    ({"title": "x", "status": "doing"}, "状态"),
    ({"title": "x", "priority": "urgent"}, "优先级"),
    ({"title": "x", "due_date": "2026/10/30"}, "截止日期"),
    ({"title": "x", "start_date": "2026-11-01", "due_date": "2026-10-30"}, "开始日期"),
    ({"title": "x", "plan_node": "调度空转调优"}, "计划"),
    ({"title": "x", "source": "alert"}, "来源引用"),
    ({"title": "x", "assignee": "Bad Id"}, "用户 ID"),
    ({"title": "x", "links": []}, "未知字段"),
])
def test_create_validation(items, fields, message):
    with pytest.raises(DataStoreError, match=message):
        items.create(fields)


def test_status_change_sets_closed_at_and_history(items):
    item = items.create({"title": "x"}, author="alice")
    done = items.update(item["id"], {"status": "done"}, author="alice", note="已验收")
    assert done["closed_at"] and done["history"][-1] == {
        **done["history"][-1], "action": "status", "changes": {"status": ["todo", "done"]}, "note": "已验收",
    }
    reopened = items.update(item["id"], {"status": "in_progress"})
    assert reopened["closed_at"] is None
    assert items.update(item["id"], {"status": "in_progress"}) == reopened  # 无变化不写 history
    with pytest.raises(DataStoreError, match="不可修改"):
        items.update(item["id"], {"source": "plan"})


def test_list_filters_and_order(items):
    late = items.create({"title": "晚", "due_date": "2026-12-01"})
    early = items.create({"title": "早", "due_date": "2026-11-01"})
    loose = items.create({"title": "无截止", "priority": "high"})
    closed = items.create({"title": "已完成", "status": "done"})
    assert [item["id"] for item in items.list()] == [early["id"], late["id"], loose["id"], closed["id"]]
    assert closed["id"] not in [item["id"] for item in items.list(include_closed=False)]
    assert [item["id"] for item in items.list(q="截止")] == [loose["id"]]
    assert [item["id"] for item in items.list(status=["done"])] == [closed["id"]]


def test_links_unique_across_items(items):
    first, second = items.create({"title": "a"}), items.create({"title": "b"})
    items.link(first["id"], "task", "tr-20261023-010101-abcdef", author="alice")
    assert items.link(first["id"], "task", "tr-20261023-010101-abcdef")["links"][0]["linked_by"] == "alice"
    with pytest.raises(WorkItemConflict) as conflict:
        items.link(second["id"], "task", "tr-20261023-010101-abcdef")
    assert conflict.value.owner_id == first["id"]
    assert items.find_link("task", "tr-20261023-010101-abcdef")["id"] == first["id"]
    items.unlink(first["id"], "task", "tr-20261023-010101-abcdef")
    assert items.find_link("task", "tr-20261023-010101-abcdef") is None
    with pytest.raises(WorkItemNotFound):
        items.unlink(first["id"], "task", "tr-20261023-010101-abcdef")
    for kind, ref in (("worklog", "2026-10-22"), ("task", "../x"), ("report", "x")):
        with pytest.raises(DataStoreError):
            items.link(first["id"], kind, ref)


def test_alert_source_dedup_until_closed(items):
    item = items.create({"title": "NodeDown", "source": "alert", "source_ref": "prometheus:alpha:NodeDown:node3"})
    with pytest.raises(WorkItemConflict):
        items.create({"title": "again", "source": "alert", "source_ref": "prometheus:alpha:NodeDown:node3"})
    items.update(item["id"], {"status": "cancelled"})
    assert items.create({"title": "again", "source": "alert", "source_ref": "prometheus:alpha:NodeDown:node3"})


def test_unknown_ids_are_not_found(items):
    for call in (lambda: items.get("wi-1"), lambda: items.get("../etc"), lambda: items.get_plan("plan-x")):
        with pytest.raises(WorkItemNotFound):
            call()


PLAN_ROWS = [
    {"node": "重复计算治理", "kind": "task", "title": "审计日志通道打通",
     "start_date": "2026-08-17", "due_date": "2026-08-21", "done": True},
    {"node": "重复计算治理", "kind": "acceptance", "title": "执行机时平均最长 300s 内",
     "start_date": None, "due_date": None, "done": False},
    {"node": "调度空转调优", "kind": "task", "title": "建立任务就绪时延基线",
     "start_date": "2026-09-07", "due_date": "2026-09-11", "done": False},
]


def test_import_new_plan_and_reimport(items):
    preview = items.import_rows(PLAN_ROWS, plan_title="2026 下半年治理", dry_run=True)
    assert (preview["create"], preview["update"]) == (3, 0) and items.list_plans() == []
    result = items.import_rows(PLAN_ROWS, plan_title="2026 下半年治理", author="alice")
    plan = result["plan"]
    assert plan["nodes"] == ["重复计算治理", "调度空转调优"]
    members = items.list(plan_id=plan["id"])
    assert {item["source"] for item in members} == {"plan"}
    done = next(item for item in members if item["title"] == "审计日志通道打通")
    # 导入时已完成按截止日收口,不算进导入那周
    assert done["status"] == "done" and done["closed_at"].startswith("2026-08-21T23:59:59")
    acceptance = next(item for item in members if item["kind"] == "acceptance")
    assert acceptance["plan_node"] == "重复计算治理"

    items.update(done["id"], {"status": "in_progress"})
    rows = [dict(PLAN_ROWS[0], due_date="2026-08-22"), *PLAN_ROWS[1:],
            {"node": "依赖关系解耦", "kind": "task", "title": "血缘元数据接入",
             "start_date": "2026-10-11", "due_date": "2026-10-14", "done": False}]
    again = items.import_rows(rows, plan_id=plan["id"], author="alice")
    assert (again["create"], again["update"], again["unchanged"]) == (1, 1, 2)
    updated = items.get(done["id"])
    assert updated["due_date"] == "2026-08-22" and updated["status"] == "in_progress"  # 状态不被覆盖
    assert items.get_plan(plan["id"])["nodes"][-1] == "依赖关系解耦"


def test_import_standalone_rows(items):
    rows = [{"node": None, "kind": "task", "title": "本周临时:证书续期", "start_date": None,
             "due_date": "2026-10-28", "done": False}]
    result = items.import_rows(rows)
    assert result["plan"] is None and result["create"] == 1
    (item,) = items.list()
    assert (item["source"], item["plan_id"], item["due_date"]) == ("manual", None, "2026-10-28")
    with pytest.raises(DataStoreError, match="项目节点"):
        items.import_rows(rows, plan_title="x")
    with pytest.raises(DataStoreError, match="计划"):
        items.import_rows(PLAN_ROWS)


def test_item_can_join_plan_node(items):
    plan = items.import_rows(PLAN_ROWS, plan_title="p")["plan"]
    loose = items.create({"title": "临时排查"})
    moved = items.update(loose["id"], {"plan_id": plan["id"], "plan_node": "调度空转调优"})
    assert moved["plan_node"] == "调度空转调优"
    with pytest.raises(DataStoreError, match="没有项目节点"):
        items.update(loose["id"], {"plan_node": "不存在"})
    renamed = items.update_plan(plan["id"], {"title": "改名"})
    assert renamed["title"] == "改名" and renamed["history"][-1]["action"] == "updated"
