"""粘贴表格解析:在线表格整块复制(制表符分隔)或 CSV → 计划行。

表头至少要有「具体事项」;「项目节点」列可选——有则导入为计划(节点为空的行沿用上一行节点,
兼容合并单元格),节点写「产物验证」的行是上一节点的验收项;没有则是批量登记的单条事项。
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional

from lib.datastore import DataStoreError

HEADERS = {
    "node": ("项目节点", "节点", "阶段"),
    "done": ("完成状态", "完成", "是否完成"),
    "title": ("具体事项", "事项", "标题", "任务"),
    "start_date": ("开始时间", "开始日期", "开始"),
    "due_date": ("截止时间", "截止日期", "截止", "完成时间"),
}
ACCEPTANCE_NODES = frozenset({"产物验证", "验收"})
_TRUE = frozenset({"true", "是", "1", "√", "✓", "✔", "☑", "已完成", "完成", "done", "yes", "y", "x"})
_FALSE = frozenset({"", "false", "否", "0", "☐", "□", "未完成", "todo", "no", "n"})
_DATE_RE = re.compile(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?")
MAX_ROWS = 500


@dataclass
class ParsedTable:
    has_nodes: bool
    rows: List[Dict[str, Any]] = field(default_factory=list)


def parse_table(text: str) -> ParsedTable:
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        raise DataStoreError("没有可导入的内容")
    delimiter = "\t" if "\t" in lines[0] else ","
    records = list(csv.reader(io.StringIO(str(text).strip()), delimiter=delimiter))
    header_index, columns = _find_header(records)
    has_nodes = "node" in columns
    rows: List[Dict[str, Any]] = []
    last_node: Optional[str] = None
    for number, record in enumerate(records[header_index + 1:], start=header_index + 2):
        cells = {key: _cell(record, index) for key, index in columns.items()}
        # 空行(复选框列常带 FALSE)跳过
        if not any(value for key, value in cells.items() if key != "done"):
            continue
        title = cells.get("title") or ""
        if not title:
            raise DataStoreError(f"第 {number} 行缺少具体事项")
        node, kind = None, "task"
        if has_nodes:
            node = cells.get("node") or last_node
            if node in ACCEPTANCE_NODES:
                if last_node is None:
                    raise DataStoreError(f"第 {number} 行的产物验证前面没有项目节点")
                node, kind = last_node, "acceptance"
            elif node is None:
                raise DataStoreError(f"第 {number} 行缺少项目节点")
            else:
                last_node = node
        start, due = _date(cells.get("start_date"), number), _date(cells.get("due_date"), number)
        if start and due and start > due:
            raise DataStoreError(f"第 {number} 行开始日期晚于截止日期")
        rows.append({
            "line": number, "node": node, "kind": kind, "title": title,
            "start_date": start, "due_date": due, "done": _done(cells.get("done"), number),
        })
        if len(rows) > MAX_ROWS:
            raise DataStoreError(f"单次最多导入 {MAX_ROWS} 行")
    if not rows:
        raise DataStoreError("表头下没有事项")
    return ParsedTable(has_nodes=has_nodes, rows=rows)


def _find_header(records: List[List[str]]) -> tuple[int, Dict[str, int]]:
    for index, record in enumerate(records):
        names = [_clean(cell) for cell in record]
        columns: Dict[str, int] = {}
        for key, aliases in HEADERS.items():
            position = next((i for i, name in enumerate(names) if name in aliases), None)
            if position is not None:
                columns[key] = position
        if "title" in columns:
            return index, columns
    raise DataStoreError("找不到表头:至少要有「具体事项」列")


def _clean(value: str) -> str:
    return " ".join(str(value or "").split())


def _cell(record: List[str], index: int) -> str:
    return _clean(record[index]) if index < len(record) else ""


def _date(value: Optional[str], number: int) -> Optional[str]:
    if not value:
        return None
    match = _DATE_RE.match(value)
    try:
        if match is None:
            raise ValueError(value)
        return date(*(int(part) for part in match.groups())).isoformat()
    except ValueError as exc:
        raise DataStoreError(f"第 {number} 行日期无法识别: {value}") from exc


def _done(value: Optional[str], number: int) -> bool:
    text = (value or "").strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise DataStoreError(f"第 {number} 行完成状态无法识别: {value}")
