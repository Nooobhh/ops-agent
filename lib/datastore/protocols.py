"""数据层接口:工作记录、排查记录、工作项与用户经此读写。

一阶段只有本地实现(lib.datastore.local);二阶段在本地之上加发件箱同步到公网共享服务
(local-first),接口不变。``root``/``run_dir`` 因此恒指本地数据根下的目录。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Protocol

from .models import Author

if TYPE_CHECKING:
    from lib.record import TroubleshootSession
    from lib.worklog import WorklogEntry


class UserDirectory(Protocol):
    def local_user(self) -> Optional[Author]:
        """本机用户(数据根所有者);未配置或配置损坏返回 None。"""

    def save_local_user(self, user_id: str, display_name: str) -> Author: ...

    def owner_id(self) -> Optional[str]: ...

    def resolve(self, author_id: Optional[str]) -> Optional[Author]:
        """None → 所有者(inferred=True);所有者也未配置时返回 None。"""


class WorklogStore(Protocol):
    root: Path

    def append(self, entry: "WorklogEntry", *, author: Optional[str] = None,
               event_key: Optional[str] = None) -> Optional[Path]:
        """author 缺省取所有者;event_key 已存在时不重复写,返回 None。"""

    def days(self, start: date, end: date, *,
             author_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """按日返回 {date, weekday, entries, available};条目带 author/author_name/author_inferred。"""

    def get(self, day: date, index: int) -> Dict[str, Any]: ...

    def available_days(self) -> List[date]: ...

    def set_weekly_excluded(self, day: date, index: int, excluded: bool) -> bool: ...


class TroubleshootStore(Protocol):
    root: Path

    def run_dir(self, run_id: str) -> Path: ...

    def start(self, run_id: str, *, author: Optional[str] = None,
              **session_fields: Any) -> "TroubleshootSession": ...

    def open(self, run_id: str) -> "TroubleshootSession": ...

    def session(self, run_id: str) -> Optional[Dict[str, Any]]: ...

    def report(self, run_id: str) -> Optional[str]: ...

    def report_ref(self, run_id: str) -> str:
        """报告的展示路径(相对数据根),供 worklog 产出、周报关联报告引用。"""

    def list(self, *, author_id: Optional[str] = None) -> List[Dict[str, Any]]: ...


class WorkItemStore(Protocol):
    """工作项与计划(模型见 lib.datastore.work_items)。挂载只存 task/troubleshoot/worklog 三种引用。"""

    root: Path

    def create(self, fields: Mapping[str, Any], *, author: Optional[str] = None) -> Dict[str, Any]: ...

    def get(self, item_id: str) -> Dict[str, Any]: ...

    def list(self, **filters: Any) -> List[Dict[str, Any]]: ...

    def update(self, item_id: str, changes: Mapping[str, Any], *, author: Optional[str] = None,
               note: Optional[str] = None) -> Dict[str, Any]: ...

    def link(self, item_id: str, kind: str, ref: str, *, author: Optional[str] = None) -> Dict[str, Any]: ...

    def unlink(self, item_id: str, kind: str, ref: str, *, author: Optional[str] = None) -> Dict[str, Any]: ...

    def find_link(self, kind: str, ref: str) -> Optional[Dict[str, Any]]: ...

    def get_plan(self, plan_id: str) -> Dict[str, Any]: ...

    def list_plans(self) -> List[Dict[str, Any]]: ...

    def update_plan(self, plan_id: str, changes: Mapping[str, Any], *,
                    author: Optional[str] = None) -> Dict[str, Any]: ...

    def import_rows(self, rows: List[Mapping[str, Any]], *, plan_id: Optional[str] = None,
                    plan_title: Optional[str] = None, author: Optional[str] = None,
                    dry_run: bool = False) -> Dict[str, Any]: ...


class DataStore(Protocol):
    users: UserDirectory
    worklogs: WorklogStore
    troubleshoot: TroubleshootStore
    work_items: WorkItemStore
