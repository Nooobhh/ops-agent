"""一阶段本地数据层:落盘格式沿用 worklog Markdown 与 session.json/report.md,只在数据根本机盘上。"""
from __future__ import annotations

import dataclasses
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from lib.data_root import ambient_data_root, data_config_path, records_root_for, resolve_data_root
from lib.record import TroubleshootSession, start_session
from lib.worklog import WorklogEntry, append_entry, parse_worklog, set_weekly_report_excluded

from .models import validate_run_id
from .protocols import UserDirectory
from .users import LocalUserDirectory
from .work_items import LocalWorkItemStore

_WEEKDAYS = "一二三四五六日"


class LocalWorklogStore:
    def __init__(self, root: Path, users: UserDirectory) -> None:
        self.root = Path(root)
        self.users = users

    def path_for(self, day: date) -> Path:
        return self.root / f"{day.isoformat()}.md"

    def append(self, entry: WorklogEntry, *, author: Optional[str] = None,
               event_key: Optional[str] = None) -> Optional[Path]:
        author_id = author or entry.author or self.users.owner_id()
        return append_entry(
            dataclasses.replace(entry, author=author_id),
            records_root=self.root, event_key=event_key,
        )

    def day(self, day: date) -> Dict[str, Any]:
        path = self.path_for(day)
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        return {
            "date": day.isoformat(),
            "weekday": _WEEKDAYS[day.weekday()],
            "entries": [self._annotate(entry) for entry in parse_worklog(text)],
            "available": path.is_file(),
        }

    def days(self, start: date, end: date, *,
             author_id: Optional[str] = None) -> List[Dict[str, Any]]:
        result = []
        current = start
        while current <= end:
            item = self.day(current)
            if author_id is not None:
                item["entries"] = [
                    entry for entry in item["entries"] if entry["author"] == author_id
                ]
            result.append(item)
            current += timedelta(days=1)
        return result

    def available_days(self) -> List[date]:
        """有 worklog 文件的日期(升序)。"""
        days = []
        for path in self.root.glob("*.md") if self.root.is_dir() else ():
            try:
                days.append(date.fromisoformat(path.stem))
            except ValueError:
                continue
        return sorted(days)

    def get(self, day: date, index: int) -> Dict[str, Any]:
        entries = self.day(day)["entries"]
        if index < 0 or index >= len(entries):
            raise IndexError("工作日志条目不存在")
        return entries[index]

    def set_weekly_excluded(self, day: date, index: int, excluded: bool) -> bool:
        return set_weekly_report_excluded(self.path_for(day), index, excluded)

    def _annotate(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        author = self.users.resolve(entry.get("author"))
        # 无作者行的条目按所有者推断;所有者也未配置时作者保持未知
        return {
            **entry,
            "author": author.id if author is not None else None,
            "author_name": author.display_name if author is not None else None,
            "author_inferred": author.inferred if author is not None else True,
        }


class LocalTroubleshootStore:
    def __init__(self, root: Path, users: UserDirectory, *, data_root: Optional[Path] = None,
                 records_root: Optional[Path] = None) -> None:
        self.root = Path(root)
        self.users = users
        self.data_root = Path(data_root) if data_root is not None else None
        self.records_root = Path(records_root) if records_root is not None else None

    def run_dir(self, run_id: str) -> Path:
        return self.root / validate_run_id(run_id)

    def start(self, run_id: str, *, author: Optional[str] = None,
              **session_fields: Any) -> TroubleshootSession:
        session = start_session(
            self.root, run_id=validate_run_id(run_id),
            author=author or self.users.owner_id(), **session_fields,
        )
        return dataclasses.replace(session, data_root=self.data_root)

    def open(self, run_id: str) -> TroubleshootSession:
        return TroubleshootSession(dir=self.run_dir(run_id), run_id=run_id, data_root=self.data_root)

    def session(self, run_id: str) -> Optional[Dict[str, Any]]:
        try:
            value = json.loads((self.run_dir(run_id) / "session.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def report(self, run_id: str) -> Optional[str]:
        try:
            return (self.run_dir(run_id) / "report.md").read_text(encoding="utf-8")
        except OSError:
            return None

    def report_ref(self, run_id: str) -> str:
        path = self.run_dir(run_id) / "report.md"
        # 记录根可在代码仓外:展示路径一律写成 records/<相对记录根>,与搬迁前一致
        if self.records_root is not None:
            try:
                return "records/" + path.relative_to(self.records_root).as_posix()
            except ValueError:
                pass
        return path.as_posix()

    def list(self, *, author_id: Optional[str] = None) -> List[Dict[str, Any]]:
        items = []
        for path in sorted(self.root.glob("*/session.json")) if self.root.is_dir() else ():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            author = self.users.resolve(data.get("author"))
            resolved_id = author.id if author is not None else None
            if author_id is not None and resolved_id != author_id:
                continue
            items.append({
                "run_id": path.parent.name,
                "trigger": data.get("trigger"),
                "platform": data.get("platform"),
                "service": data.get("service"),
                "verdict": data.get("verdict"),
                "author": resolved_id,
                "author_name": author.display_name if author is not None else None,
                "author_inferred": author.inferred if author is not None else True,
            })
        return items


class LocalDataStore:
    def __init__(self, data_root: Path, *, worklog_root: Optional[Path] = None,
                 troubleshoot_root: Optional[Path] = None) -> None:
        self.data_root = Path(data_root)
        self.users = LocalUserDirectory(data_config_path(self.data_root, "user.local.yaml"))
        self.records_root = records_root_for(self.data_root)
        records = self.records_root
        self.worklogs = LocalWorklogStore(
            Path(worklog_root) if worklog_root is not None else records / "worklog", self.users,
        )
        self.troubleshoot = LocalTroubleshootStore(
            Path(troubleshoot_root) if troubleshoot_root is not None else records / "troubleshoot",
            self.users, data_root=self.data_root, records_root=records,
        )
        self.work_items = LocalWorkItemStore(records / "work-items")


def open_datastore(repo_root: Optional[Path] = None, *, worklog_root: Optional[Path] = None,
                   troubleshoot_root: Optional[Path] = None) -> LocalDataStore:
    """repo_root 缺省 = ambient_data_root()(拿不到仓库根的库函数/CLI);显式根用于测试与调用方注入。"""
    data_root = resolve_data_root(Path(repo_root)) if repo_root is not None else ambient_data_root()
    return LocalDataStore(data_root, worklog_root=worklog_root, troubleshoot_root=troubleshoot_root)
