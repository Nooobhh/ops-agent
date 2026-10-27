"""周报按工作项归纳:工作项事实分桶、四阶段输入、来源 ID 校验;本期无工作项时输入与旧链路一致。"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib.datastore.local import LocalDataStore
from lib.weekly_report import draft_report_items
from lib.work_items.weekly import weekly_facts
from tests.envpack_helpers import as_env_pack

FIXED_INSPECTION = "每日完成例行巡检，核心运行指标均处于正常范围。"

START, END = date(2026, 10, 22), date(2026, 10, 28)
SUMMARY = [
    "完成 MySQL 8.0 复制断链修复，两端数据一致并恢复同步。",
    "完成 MySQL 备份从库搭建，每日备份按时成功并推送通知。",
    "完成 MySQL 巡检复核，主从状态与备份结果均正常。",
    "推进依赖解耦首批链路评审，明确试点剥离范围。",
    "梳理 MySQL 运维要点，沉淀复制与备份处置依据。",
]


def _repo(tmp_path: Path, *, work_item_rules: bool = True) -> Path:
    repo = as_env_pack(tmp_path / "repo")
    (repo / "config").mkdir(exist_ok=True)
    (repo / "config" / "weekly-report.yaml").write_text(
        f"report:\n  fixed_first_item: {FIXED_INSPECTION}\n", encoding="utf-8")
    skill = repo / "skills" / "weekly-report"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("weekly skill rules", encoding="utf-8")
    (skill / "references" / "STYLE_GUIDE.md").write_text("style rules", encoding="utf-8")
    if work_item_rules:
        (skill / "references" / "WORK_ITEMS.md").write_text("work item rules", encoding="utf-8")
    return repo


def _days(*entries):
    return [{"date": "2026-10-22", "entries": list(entries)}] + [
        {"date": f"2026-10-{day}", "entries": []} for day in range(23, 29)
    ]


def _entry(index, title, source="manual:web", excluded=False):
    return {"status": "DONE", "category": "OPS", "title": title, "source": source, "output": "-",
            "related": "-", "process": [], "raw": title, "entry_index": index,
            "weekly_report_excluded": excluded}


def _registry():
    return SimpleNamespace(
        resolve=lambda _source: SimpleNamespace(model_name="fake", reasoning_effort="none", model_profile_id="f"),
        client_material_for_task=lambda _selection, **_kwargs: SimpleNamespace(
            base_url="http://fake/v1", api_key="k", timeout_seconds=30, max_tool_turns=10),
    )


def _fake(monkeypatch, responses, calls):
    class FakeClient:
        def __init__(self, _config):
            pass

        def complete(self, messages, tools):
            calls.append(messages)
            return SimpleNamespace(content=json.dumps(responses.pop(0), ensure_ascii=False))

    monkeypatch.setattr("lib.triage_agent.model_client.OpenAICompatibleClient", FakeClient)


def _responses(group_ids, final_map):
    return [
        {"groups": [{"topic": "MySQL", "business_line": "基础运维线", "source_ids": group_ids,
                     "relationship": "同一工作项", "facts": [], "outcomes": [], "todo_evidence": []}],
         "ungrouped_source_ids": [], "previous_todo_status": []},
        {"summary": ["初稿"], "todos": ["初稿待办"], "source_map": {"summary_1": group_ids[:1]}},
        {"passed": True, "issues": [], "rewrite_instructions": []},
        {"summary": SUMMARY, "todos": ["推进依赖解耦分批灰度切流并完成双跑验证。", "复核 MySQL 备份通知成功率。"],
         "source_map": final_map},
    ]


def test_weekly_facts_buckets_and_maps_worklogs(tmp_path):
    store = LocalDataStore(tmp_path / "data")
    mysql = store.work_items.create({"title": "MySQL 复制与备份治理", "status": "in_progress"})
    store.work_items.link(mysql["id"], "worklog", "2026-10-22#0")
    store.work_items.create({"title": "依赖解耦灰度切流", "start_date": "2026-10-29", "due_date": "2026-11-02"})
    store.work_items.create({"title": "11 月演练", "due_date": "2026-11-30"})  # open:不进周报
    days = _days(_entry(0, "修复复制断链"), _entry(1, "无关日志"))
    facts, entry_map = weekly_facts(store, None, START, END, days, today=date(2026, 10, 28))
    by_title = {fact["title"]: fact for fact in facts}
    assert {title: fact["bucket"] for title, fact in by_title.items()} == {
        "MySQL 复制与备份治理": "in_progress", "依赖解耦灰度切流": "planned"}
    assert by_title["MySQL 复制与备份治理"]["worklog_source_ids"] == ["2026-10-22#0"]
    assert entry_map == {"2026-10-22#0": f"wi:{mysql['id']}"}
    empty = LocalDataStore(tmp_path / "empty")
    assert weekly_facts(empty, None, START, END, days, today=date(2026, 10, 28)) == ([], {})


def test_work_items_enter_every_stage(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    days = _days(_entry(0, "修复复制断链"), _entry(1, "忽略的日志", excluded=True))
    facts = [{"source_id": "wi:wi-20261022-aaaaaa", "bucket": "in_progress", "title": "MySQL 治理",
              "worklog_source_ids": ["2026-10-22#0", "2026-10-22#1"]}]
    calls = []
    _fake(monkeypatch, _responses(
        ["2026-10-22#0", "wi:wi-20261022-aaaaaa"],
        {"summary_1": ["2026-10-22#0", "wi:wi-20261022-aaaaaa"], "todo_1": ["wi:wi-20261022-aaaaaa"]},
    ), calls)
    summary, todos = draft_report_items(repo, days, _registry(),
                                        work_items=(facts, {"2026-10-22#0": "wi:wi-20261022-aaaaaa"}))
    assert summary[0] == FIXED_INSPECTION and len(todos) == 2
    assert all("work item rules" in messages[0]["content"] for messages in calls)
    payloads = [json.loads(messages[1]["content"]) for messages in calls]
    assert all("work_item_evidence" in payload for payload in payloads)
    # 周报忽略的日志不因挂了工作项而回到模型输入
    assert payloads[0]["work_item_evidence"][0]["worklog_source_ids"] == ["2026-10-22#0"]
    assert payloads[0]["worklog_evidence"][0]["work_item_source_id"] == "wi:wi-20261022-aaaaaa"


def test_aggregation_must_cover_work_items(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    facts = [{"source_id": "wi:wi-20261022-aaaaaa", "bucket": "planned", "title": "x", "worklog_source_ids": []}]
    _fake(monkeypatch, _responses(["2026-10-22#0"], {}), [])
    with pytest.raises(ValueError, match="遗漏来源: wi:wi-20261022-aaaaaa"):
        draft_report_items(repo, _days(_entry(0, "a")), _registry(), work_items=(facts, {}))


def test_final_source_map_rejects_unknown_ids(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _fake(monkeypatch, _responses(["2026-10-22#0"], {"summary_1": ["2026-10-22#0"], "todo_1": ["wi:made-up"]}), [])
    with pytest.raises(ValueError, match="未知来源: wi:made-up"):
        draft_report_items(repo, _days(_entry(0, "a")), _registry())


def test_fixed_inspection_needs_no_source(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    responses = _responses(["2026-10-22#0"], {"summary_1": ["固定巡检"], "summary_2": ["2026-10-22#0"]})
    responses[-1]["summary"] = [FIXED_INSPECTION, *SUMMARY]
    _fake(monkeypatch, responses, [])
    summary, _todos = draft_report_items(repo, _days(_entry(0, "a")), _registry())
    assert summary[0] == FIXED_INSPECTION


def test_no_work_items_keeps_model_input_unchanged(tmp_path, monkeypatch):
    """本期没有工作项:提示词与各阶段输入与不传工作项时逐字节一致,不出现工作项字段。"""
    repo = _repo(tmp_path, work_item_rules=False)
    days = _days(_entry(0, "修复复制断链"))
    runs = []
    for work_items in (None, ([], {})):
        calls = []
        _fake(monkeypatch, _responses(["2026-10-22#0"], {"summary_1": ["2026-10-22#0"]}), calls)
        draft_report_items(repo, days, _registry(), work_items=work_items)
        runs.append(json.dumps(calls, ensure_ascii=False))
    assert runs[0] == runs[1]
    assert "work_item" not in runs[0]


def test_regenerating_old_period_sees_status_at_period_end(tmp_path):
    store = LocalDataStore(tmp_path / "data")
    item = store.work_items.create({"title": "MySQL 复制修复", "status": "in_progress"})
    store.work_items.link(item["id"], "worklog", "2026-10-22#0")
    store.work_items.update(item["id"], {"status": "done"})  # 关闭发生在「今天」,晚于下面的历史周期
    facts, _ = weekly_facts(store, None, date(2020, 1, 2), date(2020, 1, 8),
                            [{"date": "2020-01-02", "entries": []}], today=date(2020, 1, 8))
    assert facts == []  # 历史周期里没有活动也没有排期:不进
    facts, _ = weekly_facts(store, None, START, END, _days(_entry(0, "修复")), today=date(2026, 10, 28))
    assert facts[0]["bucket"] in {"completed", "in_progress"}


def test_stage_json_tolerates_only_stray_closing_brackets():
    from lib.weekly_report import _parse_stage_json

    assert _parse_stage_json('{"a": {"b": 1}}}\n', "初稿") == {"a": {"b": 1}}
    assert _parse_stage_json('```json\n{"a": 1}]\n```', "初稿") == {"a": 1}
    for bad in ('{"a": 1} 另附说明', '{"a": 1}{"b": 2}', "不是 JSON", '{"a": '):
        with pytest.raises(ValueError, match="未返回有效 JSON"):
            _parse_stage_json(bad, "初稿")
