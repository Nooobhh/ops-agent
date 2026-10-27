"""数据层:工作记录、排查记录、工作项与用户的统一读写入口(见 protocols)。

本模块只导出标准库依赖的模型与接口;``open_datastore`` 延迟导入本地实现,
避免 lib.record 引用作者规则时与 lib.datastore.local 成环。
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .models import (
    AUTHOR_ID_RE, Author, DataStoreError, WorkItemConflict, WorkItemNotFound,
    validate_author_id, validate_run_id,
)
from .protocols import DataStore, TroubleshootStore, UserDirectory, WorkItemStore, WorklogStore

if TYPE_CHECKING:
    from .local import LocalDataStore


def open_datastore(repo_root: Optional[Path] = None, *, worklog_root: Optional[Path] = None,
                   troubleshoot_root: Optional[Path] = None) -> "LocalDataStore":
    from .local import open_datastore as _open

    return _open(repo_root, worklog_root=worklog_root, troubleshoot_root=troubleshoot_root)


__all__ = [
    "AUTHOR_ID_RE", "Author", "DataStore", "DataStoreError", "TroubleshootStore",
    "UserDirectory", "WorkItemConflict", "WorkItemNotFound", "WorkItemStore", "WorklogStore",
    "open_datastore",
    "validate_author_id", "validate_run_id",
]
