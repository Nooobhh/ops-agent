"""数据层共享模型:只依赖标准库,供 lib.record 等底层模块引用而不成环。"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Dict

# 用户 ID:小写字母数字开头,可含 _ . -;跨机唯一由二阶段共享服务注册保证
AUTHOR_ID_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
# 排查 run_id 与 records/troubleshoot 子目录同名,禁止路径分隔与 ..
RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


class DataStoreError(ValueError):
    pass


class WorkItemNotFound(DataStoreError):
    pass


class WorkItemConflict(DataStoreError):
    """同一引用已挂到别的工作项,或同一告警已有未关闭工作项。"""

    def __init__(self, message: str, owner_id: str) -> None:
        super().__init__(message)
        self.owner_id = owner_id


@dataclass(frozen=True)
class Author:
    id: str
    display_name: str
    # 旧记录/未写作者的新记录按数据根所有者推断
    inferred: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def validate_author_id(value: object) -> str:
    text = str(value or "").strip()
    if not AUTHOR_ID_RE.fullmatch(text):
        raise DataStoreError("用户 ID 只能用小写字母、数字、下划线、点和连字符,且以字母或数字开头,最长 64 位")
    return text


def validate_run_id(value: object) -> str:
    text = str(value or "")
    if not RUN_ID_RE.fullmatch(text) or ".." in text:
        raise DataStoreError(f"run_id 不合法: {value!r}")
    return text
