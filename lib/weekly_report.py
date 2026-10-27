"""Worklog aggregation and deterministic weekly-report generation.

The weekly-report skill defines the business rules; this module provides the
server-side adapter so the web console can use the same rules without shelling
out to an interactive agent.
"""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as xml_escape

import yaml

from lib.atomic_file import atomic_write_text
from lib.data_root import resolve_records_root
from lib.datastore import DataStoreError, WorklogStore, open_datastore
from lib.envpack import env_timezone, load_env_pack
from lib.worklog import parse_worklog  # noqa: F401 (路由与测试经此导入)


WEEKDAYS = (
    ("monday", "周一"),
    ("tuesday", "周二"),
    ("wednesday", "周三"),
    ("thursday", "周四"),
    ("friday", "周五"),
    ("saturday", "周六"),
    ("sunday", "周日"),
)
DEFAULT_START_WEEKDAY = 3
DEFAULT_END_WEEKDAY = 2
SKILL_DIR = Path("skills") / "weekly-report"
_TROUBLESHOOT_SOURCE_RE = re.compile(
    r"^auto:troubleshoot:(?P<run_id>[A-Za-z0-9][A-Za-z0-9_.-]{0,127})$"
)
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NS = {"w": _W_NS}
ET.register_namespace("w", _W_NS)


def parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("日期必须是 YYYY-MM-DD") from exc


def week_window(
    anchor: date | None = None,
    *,
    start_weekday: int = DEFAULT_START_WEEKDAY,
    end_weekday: int = DEFAULT_END_WEEKDAY,
) -> tuple[date, date]:
    """Return the configured weekly window containing the Beijing date."""
    if anchor is None:
        anchor = datetime.now(env_timezone()).date()
    _validate_weekday(start_weekday)
    _validate_weekday(end_weekday)
    start = anchor - timedelta(days=(anchor.weekday() - start_weekday) % 7)
    return start, start + timedelta(days=(end_weekday - start_weekday) % 7)


def validate_window(
    start: date,
    end: date,
    *,
    start_weekday: int = DEFAULT_START_WEEKDAY,
    end_weekday: int = DEFAULT_END_WEEKDAY,
) -> None:
    _validate_weekday(start_weekday)
    _validate_weekday(end_weekday)
    expected_days = (end_weekday - start_weekday) % 7
    if (
        end < start
        or (end - start).days != expected_days
        or start.weekday() != start_weekday
        or end.weekday() != end_weekday
    ):
        end_label = WEEKDAYS[end_weekday][1]
        if end_weekday < start_weekday:
            end_label = f"下{end_label}"
        raise ValueError(
            f"周报范围必须是{WEEKDAYS[start_weekday][1]}至"
            f"{end_label}的完整 {expected_days + 1} 天周期"
        )


def _validate_weekday(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 6:
        raise ValueError("星期必须是 0（周一）至 6（周日）")


REPORT_DEFAULTS = {"title": "运维周报", "template": "skills/weekly-report/assets/template.docx", "fixed_first_item": ""}


def report_profile(repo_root: Path) -> dict[str, Any]:
    """周报的环境部分:环境包 weekly-report.yaml 的 report 段。

    title 为文件名标题;template 为 DOCX 模板(相对环境包);fixed_first_item 为总结固定第 1 条(一字不改,
    不要求来源);business_lines 为事实聚合可选的业务线。
    """
    pack = load_env_pack(Path(repo_root))
    path = pack.config_file("weekly-report.yaml")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    report = (raw or {}).get("report") or {}
    if not isinstance(report, dict):
        raise ValueError("周报配置的 report 必须是 YAML 对象")
    profile = {key: str(report.get(key) or default) for key, default in REPORT_DEFAULTS.items()}
    if "/" in profile["title"] or "\\" in profile["title"]:
        raise ValueError("周报标题不得含路径分隔符")
    lines = report.get("business_lines") or []
    if not isinstance(lines, list) or not all(isinstance(item, str) and item for item in lines):
        raise ValueError("周报配置的 report.business_lines 必须是非空字符串列表")
    profile["business_lines"] = list(lines)
    profile["template_path"] = pack.resolve(profile["template"])
    return profile


def _skill_text(repo_root: Path, relative: str) -> str:
    """周报技能文件:环境包 skills/weekly-report/ 下的同名文件(环境风格与示例)优先,否则用核心通用版。"""
    override = load_env_pack(Path(repo_root)).resolve(str(SKILL_DIR / relative))
    path = override if override.is_file() else Path(repo_root) / SKILL_DIR / relative
    return path.read_text(encoding="utf-8")


def report_filename(title: str, start: date, end: date) -> str:
    return f"{title}{start:%Y%m%d}至{end:%Y%m%d}.docx"


def report_name_pattern(title: str) -> re.Pattern[str]:
    """周报文件名:<标题><开始>至<结束>.docx,日期兼容带横线的旧写法。"""
    return re.compile(
        rf"^{re.escape(title)}(?P<start>\d{{4}}-?\d{{2}}-?\d{{2}})至(?P<end>\d{{4}}-?\d{{2}}-?\d{{2}})\.docx$"
    )


def load_weekly_report_settings(path: Path, override: Path | None = None) -> dict[str, Any]:
    """周期:环境包 weekly-report.yaml 给默认值,本机 weekly-report.local.yaml(设置页保存)覆盖。"""
    raw: dict[str, Any] = {}
    for source in (path, override):
        if source is None or not Path(source).is_file():
            continue
        loaded = yaml.safe_load(Path(source).read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError("周报配置必须是 YAML 对象")
        raw.update({key: loaded[key] for key in ("start_day", "end_day") if key in loaded})
    names = [name for name, _label in WEEKDAYS]
    try:
        start_weekday = names.index(str(raw.get("start_day", "thursday")).lower())
        end_weekday = names.index(str(raw.get("end_day", "wednesday")).lower())
    except ValueError as exc:
        raise ValueError("周报配置的 start_day/end_day 必须是英文星期名") from exc
    return {
        "start_weekday": start_weekday,
        "end_weekday": end_weekday,
        "start_label": WEEKDAYS[start_weekday][1],
        "end_label": WEEKDAYS[end_weekday][1],
        "timezone": env_timezone().key,
    }


def save_weekly_report_settings(
    path: Path,
    *,
    start_weekday: int,
    end_weekday: int,
) -> dict[str, Any]:
    """只写本机覆盖文件(<数据根>/config/weekly-report.local.yaml),不碰环境包。"""
    _validate_weekday(start_weekday)
    _validate_weekday(end_weekday)
    payload = {"start_day": WEEKDAYS[start_weekday][0], "end_day": WEEKDAYS[end_weekday][0]}
    atomic_write_text(Path(path), yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))
    return load_weekly_report_settings(path)


def _strip_process_prefix(value: str) -> str:
    """Make a worklog process fragment readable in a customer-facing sentence."""
    value = re.sub(r"^\s*\d+(?:\.\d+|[\.、])?\s*", "", value).strip()
    return value.rstrip("。；; ")


def _display_title(value: str) -> str:
    """Remove source prefixes accidentally repeated in manually entered titles."""
    return re.sub(r"^(?:manual|auto):\s*", "", str(value or "").strip(), flags=re.IGNORECASE)


def _action_title(value: str) -> str:
    title = _display_title(value)
    if re.match(r"^(?:完成|推进|开展|排查|调查|开发|优化|修复|搭建|梳理)", title):
        return title
    return f"完成{title}"


def _sparse_expansion_candidates(entries: list[dict[str, Any]]) -> list[str]:
    """Derive additional, factual report items when a week has few DONE entries.

    Sparse periods still need the skill's minimum summary length.  Candidates
    are generated only from fields already present in the worklog; no new
    outcome, metric, or component is introduced.
    """
    candidates: list[str] = []
    for entry in entries:
        title = _display_title(entry.get("title") or "")
        if not title:
            continue
        process = [
            _strip_process_prefix(str(item))
            for item in entry.get("process", [])
            if str(item).strip()
        ]
        process = [item for item in process if item]
        if process:
            candidates.append(
                f"围绕{title}开展专项分析，重点完成{process[0]}，明确问题边界与处置结论，为后续运维治理提供依据。"
            )
        if len(process) > 1:
            candidates.append(
                f"针对{title}完成关键证据核验，结合{process[1]}交叉验证分析结果，提升问题判断的准确性。"
            )
        if len(process) > 2:
            candidates.append(
                f"梳理{title}的后续处置方向，依据{process[-1]}沉淀可复用结论，支撑同类任务复盘与优化。"
            )
        output = str(entry.get("output") or "").strip()
        if output and output != "-":
            candidates.append(
                f"将{title}的分析结果沉淀为{output}，形成可追溯的运维依据，便于后续复盘与协同处理。"
            )
    return candidates


def _ensure_report_shape(
    summary: list[str],
    todos: list[str],
    days: list[dict[str, Any]],
    fixed_item: str = "",
) -> tuple[list[str], list[str]]:
    """Enforce the skill's minimum shape without inventing sparse-week work."""
    entries = [
        entry
        for day in days
        for entry in day.get("entries", [])
        if not entry.get("weekly_report_excluded", False)
    ]
    done = [entry for entry in entries if entry.get("status") == "DONE"]
    normalized = [item.strip() for item in summary if isinstance(item, str) and item.strip()]
    normalized = _with_fixed_first(normalized, fixed_item)
    target_count = 7 if len(done) >= 2 else 6
    if done and len(normalized) < target_count:
        for candidate in _sparse_expansion_candidates(done):
            if candidate not in normalized:
                normalized.append(candidate)
            if len(normalized) >= target_count:
                break
    normalized = normalized[:10]

    normalized_todos = [item.strip() for item in todos if isinstance(item, str) and item.strip()]
    if len(done) > 1 and len(normalized_todos) < 2:
        for entry in done:
            title = _display_title(entry.get("title") or "")
            if not title:
                continue
            candidate = f"复核{title}的后续效果并形成可验收记录"
            if candidate not in normalized_todos:
                normalized_todos.append(candidate)
            if len(normalized_todos) >= 2:
                break
    return normalized, normalized_todos[:3]


def collect_worklogs(
    worklogs: "WorklogStore | Path",
    start: date,
    end: date,
    *,
    start_weekday: int = DEFAULT_START_WEEKDAY,
    end_weekday: int = DEFAULT_END_WEEKDAY,
) -> list[dict[str, Any]]:
    """按周报周期取 worklog;传目录时按 ambient 用户目录包成 WorklogStore(兼容旧调用)。"""
    validate_window(
        start, end, start_weekday=start_weekday, end_weekday=end_weekday,
    )
    if isinstance(worklogs, (str, Path)):
        worklogs = open_datastore(worklog_root=Path(worklogs)).worklogs
    return worklogs.days(start, end)


def _with_fixed_first(summary: list[str], fixed_item: str) -> list[str]:
    if not fixed_item:
        return list(summary)
    return [fixed_item, *[item for item in summary if item != fixed_item]]


def fallback_report_items(days: list[dict[str, Any]], fixed_item: str = "") -> tuple[list[str], list[str]]:
    entries = [
        entry for day in days for entry in day["entries"]
        if not entry.get("weekly_report_excluded", False)
    ]
    done = [entry for entry in entries if entry["status"] == "DONE"]
    wip = [entry for entry in entries if entry["status"] != "DONE"]
    summary = [fixed_item] if fixed_item else []
    for entry in done:
        if len(summary) >= 10:
            break
        detail = _action_title(entry["title"])
        process = [item for item in entry.get("process", []) if item]
        if process:
            detail += "，围绕" + "；".join(_strip_process_prefix(item) for item in process[:2]) + "形成阶段性分析结论"
        summary.append(detail)
    todos = [entry["title"].strip() for entry in wip[:3] if entry["title"].strip()]
    if len(todos) < 2:
        for entry in done:
            if len(todos) >= 2:
                break
            candidate = f"复核{_display_title(entry['title'])}的后续效果并形成可验收记录"
            if candidate not in todos:
                todos.append(candidate)
    return _ensure_report_shape(summary, todos[:3], days, fixed_item)


def _clip_evidence(value: str, limit: int) -> tuple[str, bool]:
    value = str(value or "").strip()
    if len(value) <= limit:
        return value, False
    return value[:limit].rstrip() + "\n[证据因上下文预算截断]", True


def _linked_troubleshoot_report(repo_root: Path, source: str) -> tuple[str | None, str]:
    match = _TROUBLESHOOT_SOURCE_RE.match(str(source or ""))
    if match is None:
        return None, ""
    store = open_datastore(Path(repo_root)).troubleshoot
    try:
        ref = store.report_ref(match.group("run_id"))
    except DataStoreError:
        return None, ""
    return ref, store.report(match.group("run_id")) or ""


def _worklog_source_material(
    repo_root: Path,
    days: list[dict[str, Any]],
    work_item_map: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Build rich, traceable LLM evidence without interpreting or rewriting it.

    work_item_map(worklog 来源 ID → wi:<id>)只给挂了工作项的条目加 work_item_source_id;
    没有工作项时条目结构与旧链路逐字节一致。
    """
    material: list[dict[str, Any]] = []
    for day in days:
        day_date = str(day.get("date") or "")
        for fallback_index, entry in enumerate(day.get("entries", [])):
            if entry.get("weekly_report_excluded", False):
                continue
            entry_index = entry.get("entry_index", fallback_index)
            source_id = f"{day_date}#{entry_index}"
            raw, raw_truncated = _clip_evidence(str(entry.get("raw") or ""), 8_000)
            report_path, report = _linked_troubleshoot_report(
                repo_root, str(entry.get("source") or ""),
            )
            report, report_truncated = _clip_evidence(report, 12_000)
            material.append({
                "source_id": source_id,
                "date": day_date,
                "status": entry.get("status"),
                "category": entry.get("category"),
                "title": entry.get("title"),
                "source": entry.get("source"),
                "output": entry.get("output"),
                "related": entry.get("related"),
                "process": entry.get("process") or [],
                "raw": raw,
                "raw_truncated": raw_truncated,
                "troubleshoot_report_path": report_path,
                "troubleshoot_report": report,
                "troubleshoot_report_truncated": report_truncated,
            })
            if work_item_map and source_id in work_item_map:
                material[-1]["work_item_source_id"] = work_item_map[source_id]
    return material


def _paragraph_text(paragraph: ET.Element) -> str:
    return "".join(
        node.text or "" for node in paragraph.findall(".//w:t", _NS)
    ).strip()


def _report_todos(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as source:
        document = ET.fromstring(source.read("word/document.xml"))
    table = document.find(".//w:body/w:tbl", _NS)
    if table is None:
        return []
    rows = table.findall("./w:tr", _NS)
    for index, row in enumerate(rows[:-1]):
        heading = "".join(_paragraph_text(p) for p in row.findall(".//w:p", _NS))
        if "三、下周待办" not in heading and "三、待完成事宜" not in heading:
            continue
        cells = rows[index + 1].findall("./w:tc", _NS)
        if not cells:
            return []
        return [
            text for paragraph in cells[0].findall("./w:p", _NS)
            if (text := _paragraph_text(paragraph))
        ]
    return []


def _previous_report_todos(repo_root: Path, days: list[dict[str, Any]]) -> list[dict[str, str]]:
    if not days:
        return []
    start = parse_day(str(days[0]["date"])) - timedelta(days=7)
    end = parse_day(str(days[-1]["date"])) - timedelta(days=7)
    report_root = resolve_records_root(Path(repo_root)) / "weekly-reports"
    title = report_profile(repo_root)["title"]
    candidates = (
        report_root / report_filename(title, start, end),
        report_root / f"{title}{start.isoformat()}至{end.isoformat()}.docx",
    )
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None:
        return []
    return [
        {"source_id": f"previous_todo#{index}", "text": text}
        for index, text in enumerate(_report_todos(path), start=1)
    ]


def _parse_stage_json(raw: str, stage: str) -> dict[str, Any]:
    value = str(raw or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.IGNORECASE)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        # 轻量模型偶尔在完整对象后多吐收尾括号:只容忍尾部纯括号/空白,其余照旧失败
        try:
            parsed, end = json.JSONDecoder().raw_decode(value)
        except json.JSONDecodeError:
            raise ValueError(f"周报{stage}阶段未返回有效 JSON") from exc
        if value[end:].strip(" \t\r\n}]"):
            raise ValueError(f"周报{stage}阶段未返回有效 JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"周报{stage}阶段必须返回 JSON 对象")
    return parsed


def _complete_stage(
    client,
    *,
    system: str,
    payload: dict[str, Any],
    stage: str,
    stage_callback: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    context = getattr(client, "context", None)
    if isinstance(context, dict):
        context["client_role"] = "weekly-report"
        context["weekly_stage"] = stage
    if stage_callback is not None:
        stage_callback(stage, "running")
    try:
        reply = client.complete([{
            "role": "system",
            "content": system,
        }, {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False),
        }], [])
    except Exception as exc:  # transport/protocol details are normalized for the web API
        if stage_callback is not None:
            stage_callback(stage, "failed")
        raise ValueError(f"周报{stage}阶段模型调用失败: {exc}") from exc
    try:
        parsed = _parse_stage_json(reply.content or "", stage)
    except Exception:
        if stage_callback is not None:
            stage_callback(stage, "failed")
        raise
    if stage_callback is not None:
        stage_callback(stage, "completed")
    return parsed


def _validate_aggregation(
    aggregation: dict[str, Any],
    source_material: list[dict[str, Any]],
) -> None:
    groups = aggregation.get("groups")
    ungrouped = aggregation.get("ungrouped_source_ids", [])
    if not isinstance(groups, list) or not isinstance(ungrouped, list):
        raise ValueError("周报事实聚合阶段缺少 groups/ungrouped_source_ids")
    known = {str(item["source_id"]) for item in source_material}
    covered: set[str] = set()
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("source_ids"), list):
            raise ValueError("周报事实聚合阶段的 group.source_ids 结构无效")
        group_ids = {str(item) for item in group["source_ids"]}
        duplicated = covered & group_ids
        if duplicated:
            raise ValueError(
                f"周报事实聚合阶段重复归组来源: {', '.join(sorted(duplicated))}"
            )
        covered.update(group_ids)
    ungrouped_ids = {str(item) for item in ungrouped}
    duplicated = covered & ungrouped_ids
    if duplicated:
        raise ValueError(
            f"周报事实聚合阶段同时归组和未归组: {', '.join(sorted(duplicated))}"
        )
    covered.update(ungrouped_ids)
    unknown = covered - known
    missing = known - covered
    if unknown:
        raise ValueError(f"周报事实聚合阶段引用未知来源: {', '.join(sorted(unknown))}")
    if missing:
        raise ValueError(f"周报事实聚合阶段遗漏来源: {', '.join(sorted(missing))}")


def _string_list(value: Any, field: str, stage: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"周报{stage}阶段的 {field} 结构无效")
    return [item.strip() for item in value if item.strip()]


def _validate_report_payload(payload: dict[str, Any], stage: str) -> tuple[list[str], list[str]]:
    summary = _string_list(payload.get("summary"), "summary", stage)
    todos = _string_list(payload.get("todos"), "todos", stage)
    source_map = payload.get("source_map")
    if not isinstance(source_map, dict):
        raise ValueError(f"周报{stage}阶段缺少 source_map")
    if not all(
        isinstance(key, str)
        and isinstance(value, list)
        and all(isinstance(item, str) for item in value)
        for key, value in source_map.items()
    ):
        raise ValueError(f"周报{stage}阶段的 source_map 结构无效")
    return summary, todos


def _validate_source_map(
    payload: dict[str, Any],
    summary: list[str],
    known: set[str],
    stage: str,
    fixed_item: str = "",
) -> None:
    """终稿 source_map 只能引用已知来源(worklog、wi:、previous_todo#N);固定巡检条不要求来源。"""
    fixed_keys = {
        f"summary_{index}" for index, item in enumerate(summary, start=1)
        if fixed_item and item == fixed_item
    }
    unknown = sorted({
        str(source)
        for key, sources in payload["source_map"].items() if key not in fixed_keys
        for source in sources
    } - known)
    if unknown:
        raise ValueError(f"周报{stage}阶段的 source_map 引用未知来源: {', '.join(unknown)}")


def _validate_final_report_items(
    summary: list[str],
    todos: list[str],
    source_material: list[dict[str, Any]],
    fixed_item: str = "",
) -> tuple[list[str], list[str]]:
    summary = _with_fixed_first(summary, fixed_item)
    if len(set(summary)) != len(summary) or len(set(todos)) != len(todos):
        raise ValueError("周报二次改写阶段仍存在重复条目")
    if source_material:
        if not 6 <= len(summary) <= 10:
            raise ValueError("周报二次改写阶段的本周工作总结必须为 6-10 条")
        if not 2 <= len(todos) <= 3:
            raise ValueError("周报二次改写阶段的下周待办必须为 2-3 条")
    elif len(summary) > (1 if fixed_item else 0) or todos:
        raise ValueError("本周期无有效日志时不得生成无来源的总结或待办")
    return summary, todos


def draft_report_items(
    repo_root: Path,
    days: list[dict[str, Any]],
    model_registry,
    *,
    stage_callback: Callable[[str, str], None] | None = None,
    work_items: tuple[list[dict[str, Any]], dict[str, str]] | None = None,
) -> tuple[list[str], list[str]]:
    """Run the skill as a four-stage LLM workflow with traceable evidence.

    work_items = lib.work_items.weekly.weekly_facts 的 (工作项证据, worklog→工作项映射)。
    本期没有工作项时提示词与各阶段输入与旧链路逐字节一致。
    """
    from lib.triage_agent.model_client import ModelConfig, OpenAICompatibleClient

    profile = report_profile(repo_root)
    skill = _skill_text(repo_root, "SKILL.md")
    style = _skill_text(repo_root, "references/STYLE_GUIDE.md")
    selection = model_registry.resolve("weekly")
    material = model_registry.client_material_for_task(selection, source="weekly")
    client = OpenAICompatibleClient(ModelConfig(
        base_url=material.base_url,
        api_key=material.api_key,
        model=selection.model_name,
        reasoning_effort=selection.reasoning_effort,
        timeout_seconds=material.timeout_seconds,
        max_tool_turns=material.max_tool_turns,
    ))
    work_item_facts, work_item_map = work_items or ([], {})
    evidence = _worklog_source_material(repo_root, days, work_item_map)
    previous_todos = _previous_report_todos(repo_root, days)
    work_item_evidence = _work_item_evidence(work_item_facts, evidence)
    extra = {"work_item_evidence": work_item_evidence} if work_item_evidence else {}
    skill_context = (
        "你必须完整遵守以下 weekly-report skill 与风格指南。"
        "所有判断只能来自输入证据；带有 source_id 的事实必须保持可追溯。"
        "只返回本阶段要求的 JSON 对象，不要 Markdown。\n\n"
        + skill + "\n\n" + style
    )
    if work_item_evidence:
        skill_context += "\n\n" + _skill_text(repo_root, "references/WORK_ITEMS.md")

    aggregation = _complete_stage(
        client,
        stage="事实聚合",
        system=(
            skill_context
            + "\n\n当前阶段：事实聚合。先理解所有日志，不写周报成稿。"
            "发现跨日期、同一组件、同一问题、前后承接或同一成果链路的关联；"
            "按业务线将真正相关的条目聚合，不得仅按日期逐条复述。"
            "每个输入 source_id 必须且只能出现在 groups.source_ids 或 "
            "ungrouped_source_ids 中。评估上一期待办是否已完成、部分完成或未闭环。"
        ),
        payload={
            "period": {
                "start_date": days[0]["date"] if days else None,
                "end_date": days[-1]["date"] if days else None,
            },
            "worklog_evidence": evidence,
            "previous_report_todos": previous_todos,
            **extra,
            "output_schema": {
                "groups": [{
                    "topic": "聚合主题",
                    "business_line": "|".join(profile["business_lines"]) or "业务线名称",
                    "source_ids": ["来源ID"],
                    "relationship": "条目之间为何相关",
                    "facts": ["可直接回溯的事实"],
                    "outcomes": ["已记录的成果或意义"],
                    "todo_evidence": ["明确待办、未实施、未坐实或治理边界"],
                }],
                "ungrouped_source_ids": ["无法与其他条目合并的来源ID"],
                "previous_todo_status": [{
                    "source_id": "previous_todo#N",
                    "status": "completed|partial|unresolved|unknown",
                    "evidence_source_ids": ["本周来源ID"],
                    "reason": "判断依据",
                }],
            },
        },
        stage_callback=stage_callback,
    )
    _validate_aggregation(aggregation, [*evidence, *work_item_evidence])

    draft = _complete_stage(
        client,
        stage="初稿",
        system=(
            skill_context
            + "\n\n当前阶段：初稿。基于事实聚合结果生成面向甲方的周报初稿。"
            "相同主题只写 1-2 条，优先体现跨日志形成的完整工作链路；"
            "不得退回按 worklog 一条对一条改写。为每条总结和待办列出来源ID。"
        ),
        payload={
            "fact_aggregation": aggregation,
            "previous_report_todos": previous_todos,
            **extra,
            "output_schema": {
                "summary": ["本周工作总结条目"],
                "todos": ["下周待办条目"],
                "source_map": {
                    "summary_1": ["来源ID"],
                    "todo_1": ["来源ID或previous_todo#N"],
                },
            },
        },
        stage_callback=stage_callback,
    )
    _validate_report_payload(draft, "初稿")

    review = _complete_stage(
        client,
        stage="规则审查",
        system=(
            skill_context
            + "\n\n当前阶段：规则审查。你是严格审稿人，不重写正文。"
            "逐条核对事实来源、跨条目聚合、业务线顺序、重复表达、客户语言、"
            "禁止细节、条数、长度和待办验收点。即使格式合格，也要指出内容质量问题。"
        ),
        payload={
            "worklog_evidence": evidence,
            "previous_report_todos": previous_todos,
            **extra,
            "fact_aggregation": aggregation,
            "draft": draft,
            "output_schema": {
                "passed": False,
                "issues": [{
                    "target": "summary_N|todo_N|global",
                    "severity": "error|warning",
                    "rule": "违反的技能规则",
                    "message": "具体问题与事实依据",
                }],
                "rewrite_instructions": ["二次改写必须执行的具体动作"],
            },
        },
        stage_callback=stage_callback,
    )
    if not isinstance(review.get("issues"), list) or not isinstance(
        review.get("rewrite_instructions"), list
    ):
        raise ValueError("周报规则审查阶段的 issues/rewrite_instructions 结构无效")

    revised = _complete_stage(
        client,
        stage="二次改写",
        system=(
            skill_context
            + "\n\n当前阶段：二次改写。根据审查意见输出最终周报。"
            "必须修复所有 error 和可执行 warning；保留有证据的具体组件与关键参数，"
            "删除虚构、重复、空泛和内部实现细节。最终正文必须由你完成，不得要求代码补句。"
        ),
        payload={
            "worklog_evidence": evidence,
            "previous_report_todos": previous_todos,
            **extra,
            "fact_aggregation": aggregation,
            "draft": draft,
            "review": review,
            "output_schema": {
                "summary": ["最终本周工作总结条目"],
                "todos": ["最终下周待办条目"],
                "source_map": {
                    "summary_1": ["来源ID"],
                    "todo_1": ["来源ID或previous_todo#N"],
                },
            },
        },
        stage_callback=stage_callback,
    )
    summary, todos = _validate_report_payload(revised, "二次改写")
    _validate_source_map(revised, summary, {
        *(str(item["source_id"]) for item in [*evidence, *work_item_evidence, *previous_todos]),
    }, "二次改写", profile["fixed_first_item"])
    return _validate_final_report_items(summary, todos, evidence, profile["fixed_first_item"])


def _work_item_evidence(
    facts: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """工作项证据只引用本期有效 worklog(周报忽略的条目不因挂了工作项而复活)。"""
    known = {str(item["source_id"]) for item in evidence}
    return [
        {**fact, "worklog_source_ids": [ref for ref in fact["worklog_source_ids"] if ref in known]}
        for fact in facts
    ]


def _set_paragraph_text(paragraph: ET.Element, text: str, template_run: ET.Element | None = None) -> None:
    for child in list(paragraph):
        if child.tag != f"{{{_W_NS}}}pPr":
            paragraph.remove(child)
    run = copy.deepcopy(template_run) if template_run is not None else ET.Element(f"{{{_W_NS}}}r")
    for child in list(run):
        if child.tag == f"{{{_W_NS}}}t":
            run.remove(child)
    text_node = ET.SubElement(run, f"{{{_W_NS}}}t")
    text_node.text = text
    if text[:1].isspace() or text[-1:].isspace():
        text_node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    paragraph.append(run)


def _find_paragraphs(root: ET.Element) -> list[ET.Element]:
    return root.findall(".//w:tbl/w:tr/w:tc/w:p", _NS)


def _with_creator(core_xml: bytes, creator: str) -> bytes:
    """docx 文档属性的作者写生成人用户 ID;模板无该元素时原样保留。"""
    return re.sub(
        rb"<dc:creator>[^<]*</dc:creator>",
        b"<dc:creator>" + xml_escape(creator).encode("utf-8") + b"</dc:creator>",
        core_xml, count=1,
    )


def generate_docx(
    repo_root: Path,
    start: date,
    end: date,
    days: list[dict[str, Any]],
    *,
    output_root: Path | None = None,
    report_items: tuple[list[str], list[str]] | None = None,
    start_weekday: int = DEFAULT_START_WEEKDAY,
    end_weekday: int = DEFAULT_END_WEEKDAY,
    creator: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    validate_window(
        start, end, start_weekday=start_weekday, end_weekday=end_weekday,
    )
    profile = report_profile(repo_root)
    summary, todos = report_items or fallback_report_items(days, profile["fixed_first_item"])
    template = profile["template_path"]
    destination_root = Path(output_root or (resolve_records_root(Path(repo_root)) / "weekly-reports"))
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = destination_root / report_filename(profile["title"], start, end)
    with zipfile.ZipFile(template) as source:
        document = ET.fromstring(source.read("word/document.xml"))
        paragraphs = _find_paragraphs(document)
        # The template stores all content in one table. Indexes are stable in
        # the checked-in template: date paragraph, summary list cell, todo cell.
        date_paragraph = document.findall("./w:body/w:p", _NS)[1]
        date_run = date_paragraph.find("./w:r", _NS)
        _set_paragraph_text(
            date_paragraph,
            f"（时间：{start:%Y年%m月%d日}至{end:%Y年%m月%d日}）",
            date_run,
        )
        table = document.find(".//w:body/w:tbl", _NS)
        if table is None:
            raise ValueError("周报模板缺少内容表格")
        rows = table.findall("./w:tr", _NS)
        summary_cell = rows[4].find("./w:tc", _NS)
        todo_cell = rows[6].find("./w:tc", _NS)
        if summary_cell is None or todo_cell is None:
            raise ValueError("周报模板缺少总结或待办区域")
        summary_template = summary_cell.find("./w:p", _NS)
        todo_template = todo_cell.find("./w:p", _NS)
        if summary_template is None or todo_template is None:
            raise ValueError("周报模板缺少列表段落")
        summary_run = summary_template.find("./w:r", _NS)
        todo_run = todo_template.find("./w:r", _NS)
        for child in list(summary_cell):
            if child.tag == f"{{{_W_NS}}}p":
                summary_cell.remove(child)
        for child in list(todo_cell):
            if child.tag == f"{{{_W_NS}}}p":
                todo_cell.remove(child)
        for item in summary:
            paragraph = copy.deepcopy(summary_template)
            _set_paragraph_text(paragraph, item, summary_run)
            summary_cell.append(paragraph)
        for item in todos:
            paragraph = copy.deepcopy(todo_template)
            _set_paragraph_text(paragraph, item, todo_run)
            todo_cell.append(paragraph)
        xml = ET.tostring(document, encoding="utf-8", xml_declaration=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination_root,
        )
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as target:
                for info in source.infolist():
                    if info.filename == "word/document.xml":
                        data = xml
                    elif info.filename == "docProps/core.xml" and creator:
                        data = _with_creator(source.read(info.filename), creator)
                    else:
                        data = source.read(info.filename)
                    target.writestr(info, data)
            os.replace(temporary, destination)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    preview = {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "filename": destination.name,
        "summary": summary,
        "todos": todos,
        "days": days,
    }
    return destination, preview
