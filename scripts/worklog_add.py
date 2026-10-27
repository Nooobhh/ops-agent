"""手动/对话提炼的工作日志写入:经数据层追加一条,作者缺省为本机用户。stdout 纯 JSON。

log-work skill 的唯一写入口;LLM 不直接编辑 records/worklog 下的文件。
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from lib.datastore import DataStoreError, open_datastore, validate_author_id
from lib.envpack import env_timezone
from lib.work_items.links import appended_ref
from lib.worklog import WorklogEntry, render_entry

try:
    from scripts._common import emit_err, emit_ok
except ImportError:  # direct script execution
    from _common import emit_err, emit_ok

STATUSES = ("DONE", "WIP", "BLOCKED")
CATEGORIES = ("BUGFIX", "FEATURE", "RESEARCH", "OPS", "DOC", "MEETING")
MAX_PROCESS = 8


def _single_line(value: str, field: str) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise ValueError(f"{field} 不能为空")
    return text


def run(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--title", required=True)
    ap.add_argument("--status", required=True, choices=STATUSES)
    ap.add_argument("--category", required=True, choices=CATEGORIES)
    ap.add_argument("--source", default="manual:cli", help="auto:<主题> 或 manual:<说明>")
    ap.add_argument("--output", default="-")
    ap.add_argument("--related", default="-")
    ap.add_argument("--process", action="append", default=[], help="可重复,最多 8 条")
    ap.add_argument("--date", help="YYYY-MM-DD,缺省北京时间今天,不得晚于今天")
    ap.add_argument("--author", help="用户 ID,缺省本机用户")
    ap.add_argument("--work-item", help="挂到工作项(wi-…);先用 scripts/work_items.py list 查当前事项")
    ap.add_argument("--dry-run", action="store_true", help="只渲染预览不写入")
    a = ap.parse_args(argv)

    today = datetime.now(env_timezone()).date()
    try:
        work_date = date.fromisoformat(a.date) if a.date else today
        if work_date > today:
            raise ValueError("工作日志日期不能晚于北京时间今天")
        if len(a.process) > MAX_PROCESS:
            raise ValueError(f"过程最多 {MAX_PROCESS} 条")
        author = validate_author_id(a.author) if a.author else None
        entry = WorklogEntry(
            status=a.status,
            category=a.category,
            title=_single_line(a.title, "标题"),
            source=_single_line(a.source, "来源"),
            output=_single_line(a.output, "产出"),
            related=_single_line(a.related, "关联"),
            process=[_single_line(item, "过程") for item in a.process],
            ts=datetime.combine(work_date, time(hour=12), tzinfo=env_timezone()),
        )
    except (ValueError, DataStoreError) as exc:
        return emit_err("bad_args", str(exc))

    datastore = open_datastore(REPO_ROOT)
    entry.author = author or datastore.users.owner_id()
    preview = render_entry(entry)
    work_item = None
    if a.work_item:
        try:
            work_item = datastore.work_items.get(a.work_item)
        except DataStoreError as exc:
            return emit_err("bad_work_item", str(exc))
    if a.dry_run:
        return emit_ok(written=False, date=work_date.isoformat(), preview=preview,
                       work_item=work_item["id"] if work_item else None)
    try:
        path = datastore.worklogs.append(entry, author=entry.author)
    except OSError as exc:
        return emit_err("write_failed", f"工作日志写入失败: {exc}")
    ref = None
    if work_item is not None:
        try:
            ref = appended_ref(datastore.worklogs, work_date, entry.title, entry.source)
            datastore.work_items.link(work_item["id"], "worklog", ref, author=entry.author)
        except (LookupError, DataStoreError) as exc:
            return emit_err("link_failed", f"日志已写入,挂到工作项失败: {exc}")
    return emit_ok(written=True, date=work_date.isoformat(), path=str(path),
                   author=entry.author, preview=preview,
                   work_item=work_item["id"] if work_item else None, worklog_ref=ref)


if __name__ == "__main__":
    sys.exit(run())
