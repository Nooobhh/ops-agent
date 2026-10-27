import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from lib.weekly_report import (
    collect_worklogs,
    draft_report_items,
    fallback_report_items,
    generate_docx,
    load_weekly_report_settings,
    parse_worklog,
    report_name_pattern,
    report_profile,
    save_weekly_report_settings,
    validate_window,
    week_window,
)
from tests.envpack_helpers import as_env_pack

FIXED = "每日完成例行巡检，核心运行指标均处于正常范围。"


def _pack_repo(repo: Path) -> Path:
    """临时仓兼作环境包:技能文件与模板都取它自己的,固定第 1 条取测试值。"""
    as_env_pack(repo)
    (repo / "config").mkdir(exist_ok=True)
    (repo / "config" / "weekly-report.yaml").write_text(
        f"report:\n  fixed_first_item: {FIXED}\n", encoding="utf-8")
    return repo


def test_week_window_is_thursday_to_wednesday():
    assert week_window(date(2026, 8, 20)) == (
        date(2026, 8, 20), date(2026, 8, 26),
    )
    assert week_window(date(2026, 8, 19)) == (
        date(2026, 8, 13), date(2026, 8, 19),
    )


def test_only_complete_thursday_week_is_accepted():
    validate_window(date(2026, 8, 20), date(2026, 8, 26))
    with pytest.raises(ValueError, match="周四至下周三"):
        validate_window(date(2026, 8, 19), date(2026, 8, 25))
    with pytest.raises(ValueError, match="完整 7 天"):
        validate_window(date(2026, 8, 20), date(2026, 8, 25))


def test_custom_week_window_and_settings(tmp_path):
    settings_path = tmp_path / "weekly-report.yaml"
    settings_path.write_text(
        "start_day: monday\nend_day: friday\ntimezone: UTC\n",
        encoding="utf-8",
    )
    settings = load_weekly_report_settings(settings_path)
    assert settings["start_weekday"] == 0
    assert settings["end_weekday"] == 4
    assert week_window(
        date(2026, 8, 20), start_weekday=0, end_weekday=4,
    ) == (date(2026, 8, 17), date(2026, 8, 21))
    validate_window(
        date(2026, 8, 17), date(2026, 8, 21),
        start_weekday=0, end_weekday=4,
    )


def test_report_profile_comes_from_the_pack_and_period_override_stays_local(tmp_path):
    repo = as_env_pack(tmp_path / "repo")
    path = repo / "config" / "weekly-report.yaml"
    path.parent.mkdir(exist_ok=True)
    path.write_text("start_day: thursday\nend_day: wednesday\nreport:\n  title: 示例周报\n"
                    "  template: assets/t.docx\n", encoding="utf-8")
    before = path.read_bytes()
    profile = report_profile(repo)
    assert profile["title"] == "示例周报" and profile["template_path"] == repo / "assets" / "t.docx"
    local = tmp_path / "data" / "config" / "weekly-report.local.yaml"
    assert load_weekly_report_settings(path, local)["start_weekday"] == 3      # 无本机覆盖:取环境包默认
    save_weekly_report_settings(local, start_weekday=0, end_weekday=4)
    assert path.read_bytes() == before                                         # 环境包只读
    assert load_weekly_report_settings(path)["start_weekday"] == 3
    assert load_weekly_report_settings(path, local)["start_weekday"] == 0
    local.write_text("end_day: sunday\n", encoding="utf-8")                    # 只覆盖写了的键
    assert (load_weekly_report_settings(path, local)["start_weekday"],
            load_weekly_report_settings(path, local)["end_weekday"]) == (3, 6)
    pattern = report_name_pattern("示例周报")
    assert pattern.match("示例周报20260817至20260821.docx")
    assert pattern.match("示例周报2026-08-17至2026-08-21.docx")
    assert not pattern.match("其他示例周报20260817至20260821.docx")
    path.write_text("start_day: thursday\nend_day: wednesday\n", encoding="utf-8")
    assert report_profile(repo)["title"] == "运维周报"


def test_parse_and_collect_worklogs_by_day(tmp_path):
    root = tmp_path / "worklog"
    root.mkdir()
    (root / "2026-08-20.md").write_text(
        "# 2026-08-20\n\n"
        "## [DONE] [OPS] 排查 HDFS\n"
        "- **来源**: manual:alice\n"
        "- **周报**: 忽略\n"
        "- **产出**: report.md\n"
        "- **关联**: HDFS\n"
        "- **过程**: 1. 采证  2. 收口\n",
        encoding="utf-8",
    )
    parsed = parse_worklog((root / "2026-08-20.md").read_text(encoding="utf-8"))
    assert parsed[0]["title"] == "排查 HDFS"
    assert parsed[0]["process"] == ["1. 采证", "2. 收口"]
    assert parsed[0]["entry_index"] == 0
    assert parsed[0]["weekly_report_excluded"] is True
    days = collect_worklogs(root, date(2026, 8, 20), date(2026, 8, 26))
    assert len(days) == 7
    assert days[0]["entries"][0]["output"] == "report.md"
    assert days[-1]["available"] is False


def test_parse_worklog_treats_dash_process_as_empty():
    parsed = parse_worklog(
        "## [DONE] [OPS] 手动记录\n"
        "- **来源**: manual:web\n"
        "- **产出**: -\n"
        "- **关联**: -\n"
        "- **过程**: -\n"
    )
    assert parsed[0]["process"] == []


def test_generate_docx_replaces_template_period_and_lists(tmp_path):
    repo = tmp_path / "repo"
    template = repo / "skills" / "weekly-report" / "assets" / "template.docx"
    template.parent.mkdir(parents=True)
    source = Path("envs/example/skills/weekly-report/assets/template.docx")
    template.write_bytes(source.read_bytes())
    days = [{
        "date": "2026-08-20", "weekday": "四", "available": True,
        "entries": [{
            "status": "DONE", "category": "OPS", "title": "完成 HDFS 排查",
            "source": "manual", "output": "report.md", "related": "HDFS",
            "process": ["定位根因", "完成收口"], "raw": "",
        }],
    }]
    days.extend({
        "date": f"2026-08-{day:02d}", "weekday": "-", "available": False,
        "entries": [],
    } for day in range(21, 27))
    path, preview = generate_docx(
        repo, date(2026, 8, 20), date(2026, 8, 26), days,
        output_root=tmp_path / "output",
    )
    assert path.is_file()
    assert any(item.startswith("完成 HDFS 排查") for item in preview["summary"])
    with ZipFile(path) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    assert "2026年08月20日至2026年08月26日" in xml
    assert "完成 HDFS 排查" in xml


def test_fallback_report_items_ignores_excluded_entries():
    summary, todos = fallback_report_items([{
        "entries": [
            {"status": "DONE", "title": "保留条目", "process": []},
            {"status": "DONE", "title": "屏蔽条目", "process": [], "weekly_report_excluded": True},
            {"status": "WIP", "title": "屏蔽待办", "process": [], "weekly_report_excluded": True},
        ],
    }])

    assert any("保留条目" in item for item in summary)
    assert all("屏蔽条目" not in item for item in summary)
    assert all("屏蔽待办" not in item for item in todos)


def test_sparse_fallback_expands_two_done_entries_from_logged_facts():
    summary, todos = fallback_report_items([{
        "entries": [
            {
                "status": "DONE",
                "title": "调查 SparkSQL 任务",
                "output": "report.md",
                "process": ["1. 统计近7日执行时长", "2. 核验关键证据", "3. 形成结论"],
            },
            {
                "status": "DONE",
                "title": "搭建 AI 巡检基础能力",
                "output": "每日快照 JSON",
                "process": ["1. 打通 Prometheus API", "2. 生成节点快照"],
            },
        ],
    }], FIXED)

    assert len(summary) == 7
    assert summary[0] == FIXED
    assert any("统计近7日执行时长" in item for item in summary)
    assert len(todos) == 2


def test_draft_report_items_runs_four_llm_stages_with_rich_traceable_evidence(
    tmp_path, monkeypatch,
):
    repo = _pack_repo(tmp_path / "repo")
    skill = repo / "skills" / "weekly-report"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("weekly skill rules", encoding="utf-8")
    (skill / "references" / "STYLE_GUIDE.md").write_text("style rules", encoding="utf-8")
    template = skill / "assets" / "template.docx"
    template.parent.mkdir(parents=True)
    template.write_bytes(Path("envs/example/skills/weekly-report/assets/template.docx").read_bytes())
    generate_docx(
        repo,
        date(2026, 8, 13),
        date(2026, 8, 19),
        [],
        report_items=(
            [FIXED, "完成上一周期工作。"],
            ["评审 SparkSQL 治理清单并明确验收指标。", "验证 Prometheus 巡检闭环。"],
        ),
    )
    report = repo / "records" / "troubleshoot" / "ts-related" / "report.md"
    report.parent.mkdir(parents=True)
    report.write_text("# 关联排查报告\n\n根因与关键证据。", encoding="utf-8")
    captured = {"calls": []}
    responses = [
        {
            "groups": [{
                "topic": "HDFS 容量告警",
                "business_line": "基础运维线",
                "source_ids": ["2026-08-20#0"],
                "relationship": "同一告警的采证和收口",
                "facts": ["完成 HDFS 告警核验"],
                "outcomes": ["明确指标口径"],
                "todo_evidence": [],
            }],
            "ungrouped_source_ids": [],
            "previous_todo_status": [],
        },
        {
            "summary": ["初稿 HDFS 内容"],
            "todos": ["初稿待办"],
            "source_map": {
                "summary_1": ["2026-08-20#0"],
                "todo_1": ["previous_todo#1"],
            },
        },
        {
            "passed": False,
            "issues": [{
                "target": "summary_1",
                "severity": "warning",
                "rule": "客户语言",
                "message": "需补充效果",
            }],
            "rewrite_instructions": ["补充运维效果并保持事实可追溯"],
        },
        {
            "summary": [
                "完成 HDFS 容量告警口径核验，明确监控指标差异及处置边界。",
                "完成 HDFS 关键证据复核，形成可追溯的容量治理依据。",
                "针对 HDFS 告警影响开展分析，明确后续容量风险关注重点。",
                "推进 HDFS 排查结论沉淀，提升同类告警解释和响应效率。",
                "完成 HDFS 运维材料归纳，为后续治理评审提供事实基础。",
            ],
            "todos": [
                "评审 SparkSQL 治理清单并明确验收指标。",
                "验证 Prometheus 巡检闭环并形成复盘记录。",
            ],
            "source_map": {
                "summary_1": ["2026-08-20#0"],
                "todo_1": ["previous_todo#1"],
                "todo_2": ["previous_todo#2"],
            },
        },
    ]

    class FakeClient:
        def __init__(self, _config):
            pass

        def complete(self, messages, tools):
            captured["calls"].append({"messages": messages, "tools": tools})
            return SimpleNamespace(content=json.dumps(responses.pop(0), ensure_ascii=False))

    monkeypatch.setattr(
        "lib.triage_agent.model_client.OpenAICompatibleClient", FakeClient,
    )
    def resolve(source):
        captured["source"] = source
        return SimpleNamespace(
            model_name="fake", reasoning_effort="none", model_profile_id="fake",
        )

    registry = SimpleNamespace(
        resolve=resolve,
        client_material_for_task=lambda _selection, **_kwargs: SimpleNamespace(
            base_url="http://fake/v1", api_key="secret", timeout_seconds=30,
            max_tool_turns=10,
        ),
    )
    days = [{
        "date": "2026-08-20", "entries": [
            {
                "status": "DONE", "category": "OPS", "title": "参与周报",
                "source": "auto:troubleshoot:ts-related", "output": "report.md",
                "related": "HDFS", "process": ["核验指标口径"],
                "raw": "完整日报正文与现场输出", "entry_index": 0,
                "weekly_report_excluded": False,
            },
            {
                "status": "DONE", "category": "OPS", "title": "不得进入模型",
                "source": "manual:web", "output": "-", "related": "-",
                "process": [], "raw": "屏蔽条目正文", "entry_index": 1,
                "weekly_report_excluded": True,
            },
        ],
    }]
    days.extend({
        "date": f"2026-08-{day:02d}", "entries": [],
    } for day in range(21, 27))
    events = []
    summary, todos = draft_report_items(
        repo,
        days,
        registry,
        stage_callback=lambda stage, status: events.append((stage, status)),
    )

    assert len(captured["calls"]) == 4
    assert all(call["tools"] == [] for call in captured["calls"])
    assert all(
        "weekly skill rules" in call["messages"][0]["content"]
        for call in captured["calls"]
    )
    assert captured["source"] == "weekly"
    aggregation_input = json.loads(captured["calls"][0]["messages"][1]["content"])
    evidence = aggregation_input["worklog_evidence"]
    assert [item["source_id"] for item in evidence] == ["2026-08-20#0"]
    assert evidence[0]["raw"] == "完整日报正文与现场输出"
    assert "根因与关键证据" in evidence[0]["troubleshoot_report"]
    assert "不得进入模型" not in json.dumps(aggregation_input, ensure_ascii=False)
    assert aggregation_input["previous_report_todos"][0]["source_id"] == "previous_todo#1"
    draft_input = json.loads(captured["calls"][1]["messages"][1]["content"])
    assert draft_input["fact_aggregation"]["groups"][0]["source_ids"] == ["2026-08-20#0"]
    rewrite_input = json.loads(captured["calls"][3]["messages"][1]["content"])
    assert rewrite_input["review"]["rewrite_instructions"]
    assert summary[0] == FIXED
    assert len(summary) == 6
    assert len(todos) == 2
    assert events == [
        ("事实聚合", "running"), ("事实聚合", "completed"),
        ("初稿", "running"), ("初稿", "completed"),
        ("规则审查", "running"), ("规则审查", "completed"),
        ("二次改写", "running"), ("二次改写", "completed"),
    ]


def test_draft_report_items_rejects_aggregation_that_drops_a_worklog(tmp_path, monkeypatch):
    repo = _pack_repo(tmp_path / "repo")
    skill = repo / "skills" / "weekly-report"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("weekly skill rules", encoding="utf-8")
    (skill / "references" / "STYLE_GUIDE.md").write_text("style rules", encoding="utf-8")

    class FakeClient:
        def __init__(self, _config):
            pass

        def complete(self, _messages, _tools):
            return SimpleNamespace(content=json.dumps({
                "groups": [], "ungrouped_source_ids": [], "previous_todo_status": [],
            }))

    monkeypatch.setattr(
        "lib.triage_agent.model_client.OpenAICompatibleClient", FakeClient,
    )
    registry = SimpleNamespace(
        resolve=lambda _source: SimpleNamespace(
            model_name="fake", reasoning_effort="none", model_profile_id="fake",
        ),
        client_material_for_task=lambda _selection, **_kwargs: SimpleNamespace(
            base_url="http://fake/v1", api_key="secret", timeout_seconds=30,
            max_tool_turns=10,
        ),
    )
    days = [{
        "date": "2026-08-20",
        "entries": [{
            "status": "DONE", "category": "OPS", "title": "HDFS 排查",
            "source": "manual:web", "output": "-", "related": "HDFS",
            "process": [], "raw": "", "entry_index": 0,
        }],
    }]

    with pytest.raises(ValueError, match="遗漏来源: 2026-08-20#0"):
        draft_report_items(repo, days, registry)


def _week_files(root: Path, *, author: bool) -> None:
    root.mkdir(parents=True)
    author_line = "- **作者**: alice\n" if author else ""
    (root / "2026-08-20.md").write_text(
        "# 2026-08-20\n\n"
        "## [DONE] [OPS] 排查 HDFS\n"
        "- **来源**: auto:troubleshoot:ts-1\n"
        f"{author_line}"
        "- **产出**: report.md\n"
        "- **关联**: HDFS\n"
        "- **过程**: 1. 采证  2. 收口\n\n"
        "## [WIP] [RESEARCH] 调研队列积压\n"
        "- **来源**: manual:web\n"
        f"{author_line}"
        "- **周报**: 忽略\n"
        "- **产出**: -\n"
        "- **关联**: -\n"
        "- **过程**: -\n\n",
        encoding="utf-8",
    )


def test_author_lines_do_not_change_weekly_model_input(tmp_path):
    """数据层加作者行前后,同一周的周报事实聚合输入逐字段一致。"""
    from lib.weekly_report import _worklog_source_material

    _week_files(tmp_path / "legacy" / "records" / "worklog", author=False)
    _week_files(tmp_path / "authored" / "records" / "worklog", author=True)
    start, end = date(2026, 8, 20), date(2026, 8, 26)
    materials = []
    for name in ("legacy", "authored"):
        root = tmp_path / name
        days = collect_worklogs(root / "records" / "worklog", start, end)
        materials.append(_worklog_source_material(root, days))
    assert materials[0] == materials[1]
    assert materials[0][0]["raw"].count("作者") == 0


def test_generate_docx_records_creator(tmp_path):
    repo = tmp_path / "repo"
    template = repo / "skills" / "weekly-report" / "assets" / "template.docx"
    template.parent.mkdir(parents=True)
    template.write_bytes(Path("envs/example/skills/weekly-report/assets/template.docx").read_bytes())
    days = [{"date": f"2026-08-{day:02d}", "weekday": "-", "available": False, "entries": []}
            for day in range(20, 27)]
    path, _ = generate_docx(repo, date(2026, 8, 20), date(2026, 8, 26), days,
                            output_root=tmp_path / "out", creator="alice")
    with ZipFile(path) as archive:
        assert "<dc:creator>alice</dc:creator>" in archive.read("docProps/core.xml").decode("utf-8")


def test_skill_files_prefer_the_pack_and_fall_back_to_core(tmp_path):
    from lib.data_root import local_config_path
    from lib.weekly_report import _skill_text

    pack = as_env_pack(tmp_path / "pack")
    repo = tmp_path / "repo"
    for root, text in ((repo, "core"), (pack, "pack")):
        (root / "skills" / "weekly-report" / "references").mkdir(parents=True)
        (root / "skills" / "weekly-report" / "SKILL.md").write_text(f"{text} skill", encoding="utf-8")
    (repo / "skills" / "weekly-report" / "references" / "WORK_ITEMS.md").write_text("core rules", encoding="utf-8")
    local = local_config_path(repo, "paths.local.yaml")
    local.parent.mkdir(parents=True)
    local.write_text(f"env_pack: {pack}\n", encoding="utf-8")
    assert _skill_text(repo, "SKILL.md") == "pack skill"
    assert _skill_text(repo, "references/WORK_ITEMS.md") == "core rules"
