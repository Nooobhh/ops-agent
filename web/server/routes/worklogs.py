"""Worklog browsing and weekly-report generation endpoints."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import zipfile
from datetime import date, datetime, time
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree as ET

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import Field, field_validator

from lib.data_root import local_config_path
from lib.datastore import DataStoreError, WorkItemNotFound, open_datastore
from lib.envpack import env_timezone, load_env_pack
from lib.triage_agent.models import StrictModel
from lib.work_items.links import appended_ref
from lib.work_items.weekly import weekly_facts
from lib.worklog import WorklogEntry
from lib.weekly_report import (
    collect_worklogs,
    draft_report_items,
    generate_docx,
    load_weekly_report_settings,
    parse_day,
    report_filename,
    report_name_pattern,
    report_profile,
    save_weekly_report_settings,
    validate_window,
    week_window,
)
from lib.weekly_report_jobs import (
    WeeklyReportJobConflict,
    WeeklyReportJobManager,
    WeeklyReportJobNotFound,
)
from web.server.context import RequestContext, request_context


_TROUBLESHOOT_SOURCE = re.compile(
    r"^auto:troubleshoot:(?P<run_id>[A-Za-z0-9][A-Za-z0-9_.-]{0,127})$"
)
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


class WeeklyReportRequest(StrictModel):
    start_date: str = Field(min_length=10, max_length=10)
    end_date: str = Field(min_length=10, max_length=10)


class WeeklyReportSettingsRequest(StrictModel):
    start_weekday: int = Field(ge=0, le=6)
    end_weekday: int = Field(ge=0, le=6)


class WorklogCreateRequest(StrictModel):
    date: str = Field(min_length=10, max_length=10)
    status: Literal["DONE", "WIP", "BLOCKED"]
    category: Literal["BUGFIX", "FEATURE", "RESEARCH", "OPS", "DOC", "MEETING"]
    title: str = Field(min_length=1, max_length=300)
    output: str = Field(default="-", max_length=2000)
    related: str = Field(default="-", max_length=2000)
    process: list[str] = Field(default_factory=list, max_length=8)
    work_item_id: str | None = Field(default=None, max_length=40)

    @field_validator("title", "output", "related")
    @classmethod
    def clean_single_line(cls, value: str, info):
        cleaned = value.strip()
        if "\n" in cleaned or "\r" in cleaned:
            raise ValueError(f"{info.field_name} 不能包含换行")
        if info.field_name == "title" and not cleaned:
            raise ValueError("标题不能为空")
        return cleaned or "-"

    @field_validator("process")
    @classmethod
    def clean_process(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item for item in cleaned):
            raise ValueError("过程步骤不能为空")
        if any("\n" in item or "\r" in item for item in cleaned):
            raise ValueError("过程步骤不能包含换行")
        if any(len(item) > 200 for item in cleaned):
            raise ValueError("单条过程步骤不能超过 200 字")
        return cleaned


class WorklogWeeklyReportExclusionRequest(StrictModel):
    excluded: bool


def _window(
    start_date: str | None,
    end_date: str | None,
    settings: dict,
) -> tuple[date, date]:
    if start_date is None and end_date is None:
        return week_window(
            start_weekday=settings["start_weekday"],
            end_weekday=settings["end_weekday"],
        )
    if not start_date or not end_date:
        raise HTTPException(status_code=400, detail="start_date 和 end_date 必须同时提供")
    try:
        start, end = parse_day(start_date), parse_day(end_date)
        validate_window(
            start,
            end,
            start_weekday=settings["start_weekday"],
            end_weekday=settings["end_weekday"],
        )
        current_start, _current_end = week_window(
            start_weekday=settings["start_weekday"],
            end_weekday=settings["end_weekday"],
        )
        if start > current_start:
            raise ValueError("不能查看或生成当前周期之后的周报")
        return start, end
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _report_item(path: Path, name_pattern: re.Pattern) -> dict:
    match = name_pattern.match(path.name)
    if not match:
        raise ValueError("周报文件名不符合约定")
    start = match.group("start").replace("-", "")
    end = match.group("end").replace("-", "")
    return {
        "filename": path.name,
        "start_date": f"{start[:4]}-{start[4:6]}-{start[6:]}",
        "end_date": f"{end[:4]}-{end[4:6]}-{end[6:]}",
        "updated_at": datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(),
    }


def _safe_report(report_root: Path, filename: str, name_pattern: re.Pattern) -> Path:
    if Path(filename).name != filename or not name_pattern.match(filename):
        raise HTTPException(status_code=404, detail="周报不存在")
    path = (report_root / filename).resolve()
    try:
        path.relative_to(report_root.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="周报不存在") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="周报不存在")
    return path


def _docx_paragraphs(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as source:
        root = ET.fromstring(source.read("word/document.xml"))
    paragraphs = []
    for paragraph in root.findall(f".//{{{_W_NS}}}p"):
        text = "".join(node.text or "" for node in paragraph.findall(f".//{{{_W_NS}}}t"))
        if text.strip():
            paragraphs.append(text)
    return paragraphs


def _paragraph_text(paragraph: ET.Element) -> str:
    return "".join(
        node.text or "" for node in paragraph.findall(f".//{{{_W_NS}}}t")
    ).strip()


def _docx_table_preview(path: Path) -> dict:
    with zipfile.ZipFile(path) as source:
        root = ET.fromstring(source.read("word/document.xml"))
    table = root.find(f".//{{{_W_NS}}}tbl")
    if table is None:
        raise ValueError("周报文档缺少内容表格")
    rows = table.findall(f"./{{{_W_NS}}}tr")

    def cell_texts(row_index: int) -> list[str]:
        if row_index >= len(rows):
            return []
        return [
            "".join(node.text or "" for node in cell.findall(f".//{{{_W_NS}}}t")).strip()
            for cell in rows[row_index].findall(f"./{{{_W_NS}}}tc")
        ]

    def list_items(row_index: int) -> list[str]:
        if row_index >= len(rows):
            return []
        cells = rows[row_index].findall(f"./{{{_W_NS}}}tc")
        if not cells:
            return []
        return [
            text for paragraph in cells[0].findall(f"./{{{_W_NS}}}p")
            if (text := _paragraph_text(paragraph))
        ]

    user_row = cell_texts(1)
    project_row = cell_texts(2)
    return {
        "customer": user_row[1] if len(user_row) > 1 else "",
        "contract_number": user_row[3] if len(user_row) > 3 else "",
        "project_name": project_row[1] if len(project_row) > 1 else "",
        "project_manager": project_row[3] if len(project_row) > 3 else "",
        "summary": list_items(4),
        "todos": list_items(6),
    }


def router(*, repo_root: Path, model_registry=None, job_manager=None, task_store=None) -> APIRouter:
    routes = APIRouter()
    repo_root = Path(repo_root).resolve()
    weekly_jobs = job_manager or WeeklyReportJobManager()
    datastore = open_datastore(repo_root)
    worklogs = datastore.worklogs
    report_root = datastore.records_root / "weekly-reports"
    # 周期默认值在环境包,设置页保存的覆盖值在数据根本机配置;界面不写环境目录
    settings_path = load_env_pack(repo_root).config_file("weekly-report.yaml")
    local_settings_path = local_config_path(repo_root, "weekly-report.local.yaml")

    def profile() -> dict:
        try:
            return report_profile(repo_root)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            raise HTTPException(status_code=500, detail=f"周报配置无效: {exc}") from exc

    def name_pattern() -> re.Pattern:
        return report_name_pattern(profile()["title"])

    def report_item(path: Path) -> dict:
        return _report_item(path, name_pattern())

    def safe_report(filename: str) -> Path:
        return _safe_report(report_root, filename, name_pattern())

    def current_settings() -> dict:
        try:
            settings = load_weekly_report_settings(settings_path, local_settings_path)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=f"周报配置无效: {exc}") from exc
        current_start, current_end = week_window(
            start_weekday=settings["start_weekday"],
            end_weekday=settings["end_weekday"],
        )
        return {
            **settings,
            "current_start_date": current_start.isoformat(),
            "current_end_date": current_end.isoformat(),
        }

    def worklog_entry(work_date: str, entry_index: int) -> tuple[date, dict]:
        try:
            parsed_date = parse_day(work_date)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            return parsed_date, worklogs.get(parsed_date, entry_index)
        except IndexError as exc:
            raise HTTPException(status_code=404, detail="工作日志条目不存在") from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"工作日志读取失败: {exc}") from exc

    @routes.get("/api/weekly-report-settings")
    def get_weekly_report_settings(
        _context: RequestContext = Depends(request_context),
    ):
        return {"schema_version": 1, **current_settings()}

    @routes.put("/api/weekly-report-settings")
    def update_weekly_report_settings(
        body: WeeklyReportSettingsRequest,
        _context: RequestContext = Depends(request_context),
    ):
        try:
            save_weekly_report_settings(
                local_settings_path,
                start_weekday=body.start_weekday,
                end_weekday=body.end_weekday,
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"schema_version": 1, **current_settings()}

    @routes.get("/api/worklogs")
    def get_worklogs(
        start_date: str | None = Query(default=None),
        end_date: str | None = Query(default=None),
        _context: RequestContext = Depends(request_context),
    ):
        settings = current_settings()
        start, end = _window(start_date, end_date, settings)
        return {
            "schema_version": 1,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "days": collect_worklogs(
                worklogs,
                start,
                end,
                start_weekday=settings["start_weekday"],
                end_weekday=settings["end_weekday"],
            ),
        }

    @routes.post("/api/worklogs", status_code=201)
    def create_worklog(
        body: WorklogCreateRequest,
        _context: RequestContext = Depends(request_context),
    ):
        try:
            work_date = parse_day(body.date)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if work_date > datetime.now(env_timezone()).date():
            raise HTTPException(status_code=422, detail="工作日志日期不能晚于北京时间今天")
        entry = WorklogEntry(
            status=body.status,
            category=body.category,
            title=body.title,
            source="manual:web",
            output=body.output,
            related=body.related,
            process=body.process,
            ts=datetime.combine(work_date, time(hour=12), tzinfo=env_timezone()),
        )
        author = _context.user.id if _context.user else None
        if body.work_item_id:
            try:
                datastore.work_items.get(body.work_item_id)
            except WorkItemNotFound as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        try:
            path = worklogs.append(entry, author=author)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"工作日志写入失败: {exc}") from exc
        worklog_ref = None
        if body.work_item_id:
            try:
                worklog_ref = appended_ref(worklogs, work_date, entry.title, entry.source)
                datastore.work_items.link(
                    body.work_item_id, "worklog", worklog_ref,
                    author=author or datastore.users.owner_id(),
                )
            except (LookupError, DataStoreError) as exc:
                raise HTTPException(status_code=409, detail=f"日志已写入,挂到工作项失败: {exc}") from exc
        return {
            "schema_version": 1,
            "date": work_date.isoformat(),
            "path": "records/" + path.relative_to(datastore.records_root).as_posix(),
            "work_item_id": body.work_item_id,
            "worklog_ref": worklog_ref,
            "entry": {
                "status": entry.status,
                "category": entry.category,
                "title": entry.title,
                "source": entry.source,
                "author": author or datastore.users.owner_id(),
                "output": entry.output,
                "related": entry.related,
                "process": entry.process,
                "raw": "",
            },
        }

    @routes.put("/api/worklogs/{work_date}/{entry_index}/weekly-report-exclusion")
    def update_worklog_weekly_report_exclusion(
        work_date: str,
        entry_index: int,
        body: WorklogWeeklyReportExclusionRequest,
        _context: RequestContext = Depends(request_context),
    ):
        try:
            parsed_date = parse_day(work_date)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            excluded = worklogs.set_weekly_excluded(parsed_date, entry_index, body.excluded)
        except (FileNotFoundError, IndexError) as exc:
            raise HTTPException(status_code=404, detail="工作日志条目不存在") from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"工作日志更新失败: {exc}") from exc
        return {
            "schema_version": 1,
            "date": parsed_date.isoformat(),
            "entry_index": entry_index,
            "weekly_report_excluded": excluded,
        }

    @routes.get("/api/worklogs/{work_date}/{entry_index}")
    def get_worklog_detail(
        work_date: str,
        entry_index: int,
        _context: RequestContext = Depends(request_context),
    ):
        parsed_date, entry = worklog_entry(work_date, entry_index)
        report_path = None
        report_markdown = None
        match = _TROUBLESHOOT_SOURCE.match(str(entry.get("source") or ""))
        if match:
            try:
                report_markdown = datastore.troubleshoot.report(match.group("run_id"))
            except DataStoreError:
                report_markdown = None
            if report_markdown is not None:
                report_path = datastore.troubleshoot.report_ref(match.group("run_id"))
        return {
            "schema_version": 1,
            "date": parsed_date.isoformat(),
            "entry": entry,
            "report_path": report_path,
            "report_markdown": report_markdown,
        }

    @routes.get("/api/weekly-reports")
    def list_reports(
        limit: int | None = Query(default=None, ge=1, le=1000),
        _context: RequestContext = Depends(request_context),
    ):
        report_root.mkdir(parents=True, exist_ok=True)
        items = []
        pattern = name_pattern()
        for path in sorted(report_root.glob("*.docx"), key=lambda item: item.name, reverse=True):
            if pattern.match(path.name):
                items.append(_report_item(path, pattern))
        return {
            "schema_version": 1,
            "items": items if limit is None else items[:limit],
        }

    def submit_report_job(
        *,
        start: date,
        end: date,
        days: list[dict],
        operation: str,
        creator: str | None,
    ) -> dict:
        def run(stage_callback):
            # 工作项事实在生成时现取:分桶、挂上 worklog 来源 ID,不写句子
            work_items = weekly_facts(
                datastore, task_store, start, end, days,
                today=datetime.now(env_timezone()).date(),
            )
            report_items = draft_report_items(
                repo_root,
                days,
                model_registry,
                stage_callback=stage_callback,
                work_items=work_items,
            )
            stage_callback("生成DOCX", "running")
            path, preview = generate_docx(
                repo_root,
                start,
                end,
                days,
                report_items=report_items,
                start_weekday=start.weekday(),
                end_weekday=end.weekday(),
                creator=creator,
            )
            stage_callback("生成DOCX", "completed")
            return {"report": report_item(path), "preview": preview}

        try:
            return weekly_jobs.submit(
                start=start,
                end=end,
                operation=operation,
                runner=run,
            )
        except WeeklyReportJobConflict as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": str(exc),
                    "job": exc.existing,
                },
            ) from exc

    @routes.get("/api/weekly-report-jobs")
    def list_weekly_report_jobs(
        start_date: str | None = Query(default=None),
        end_date: str | None = Query(default=None),
        _context: RequestContext = Depends(request_context),
    ):
        return {
            "schema_version": 1,
            "items": weekly_jobs.list(start=start_date, end=end_date),
        }

    @routes.get("/api/weekly-report-jobs/{job_id}")
    def get_weekly_report_job(
        job_id: str,
        _context: RequestContext = Depends(request_context),
    ):
        try:
            return weekly_jobs.get(job_id)
        except WeeklyReportJobNotFound as exc:
            raise HTTPException(status_code=404, detail="周报生成任务不存在") from exc

    @routes.post("/api/weekly-reports", status_code=202)
    def create_report(
        body: WeeklyReportRequest,
        _context: RequestContext = Depends(request_context),
    ):
        settings = current_settings()
        start, end = _window(body.start_date, body.end_date, settings)
        existing = report_root / report_filename(profile()["title"], start, end)
        if existing.is_file():
            raise HTTPException(status_code=409, detail="该周期周报已生成，请直接查看")
        days = collect_worklogs(
            worklogs,
            start,
            end,
            start_weekday=settings["start_weekday"],
            end_weekday=settings["end_weekday"],
        )
        if model_registry is None:
            raise HTTPException(status_code=503, detail="周报生成模型不可用")
        return submit_report_job(
            start=start,
            end=end,
            days=days,
            operation="create",
            creator=_context.user.id if _context.user else datastore.users.owner_id(),
        )

    @routes.put("/api/weekly-reports/{filename}", status_code=202)
    def regenerate_report(
        filename: str,
        _context: RequestContext = Depends(request_context),
    ):
        existing = safe_report(filename)
        item = report_item(existing)
        start = parse_day(item["start_date"])
        end = parse_day(item["end_date"])
        days = collect_worklogs(
            worklogs,
            start,
            end,
            start_weekday=start.weekday(),
            end_weekday=end.weekday(),
        )
        if model_registry is None:
            raise HTTPException(status_code=503, detail="周报生成模型不可用")
        return submit_report_job(
            start=start,
            end=end,
            days=days,
            operation="regenerate",
            creator=_context.user.id if _context.user else datastore.users.owner_id(),
        )

    @routes.get("/api/weekly-reports/{filename}/preview")
    def preview_report(
        filename: str,
        _context: RequestContext = Depends(request_context),
    ):
        path = safe_report(filename)
        try:
            table = _docx_table_preview(path)
        except (ValueError, zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
            raise HTTPException(status_code=422, detail=f"周报预览失败: {exc}") from exc
        return {
            "schema_version": 1,
            "report": report_item(path),
            "paragraphs": _docx_paragraphs(path),
            **table,
        }

    @routes.get("/api/weekly-reports/{filename}")
    def download_report(
        filename: str,
        _context: RequestContext = Depends(request_context),
    ):
        path = safe_report(filename)
        return FileResponse(
            path,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename=path.name,
        )

    @routes.post("/api/weekly-reports/{filename}/reveal")
    def reveal_report(
        filename: str,
        _context: RequestContext = Depends(request_context),
    ):
        """Reveal an existing report in the host OS file manager."""
        path = safe_report(filename)
        if sys.platform == "darwin":
            command = ["open", "-R", str(path)]
        elif sys.platform == "win32":
            command = ["explorer", f"/select,{path}"]
        else:
            command = ["xdg-open", str(path.parent)]
        try:
            subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise HTTPException(status_code=503, detail=f"无法打开周报所在文件夹: {exc}") from exc
        return {
            "schema_version": 1,
            "filename": path.name,
            "folder": str(path.parent),
        }

    @routes.delete("/api/weekly-reports/{filename}")
    def delete_report(
        filename: str,
        _context: RequestContext = Depends(request_context),
    ):
        path = safe_report(filename)
        report = report_item(path)
        try:
            path.unlink()
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"周报删除失败: {exc}") from exc
        return {"schema_version": 1, "report": report}

    return routes
