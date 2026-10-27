"""粘贴表格解析:制表符/CSV、产物验证归节点、合并单元格沿用节点、日期与勾选格式。"""
from __future__ import annotations

import pytest

from lib.datastore import DataStoreError
from lib.work_items.table import parse_table

SHEET = (
    "项目节点\t完成状态\t具体事项\t开始时间\t截止时间\n"
    "重复计算治理\tTRUE\tSpark/Hive 审计日志通道打通\t2026/8/17\t2026/8/21\n"
    "\tFALSE\t底表多维重复扫描识别\t2026/8/24\t2026/8/25\n"
    "产物验证\tFALSE\tSparkSQL类型任务执行机时下降,平均最长耗时控制在300s内\t\t\n"
    "调度空转调优\t\t采集调度实例历史日志\t2026-09-07\t2026年9月11日\n"
    "\tFALSE\t\t\t\n"
)


def test_parse_plan_sheet():
    table = parse_table(SHEET)
    assert table.has_nodes
    assert [(row["node"], row["kind"], row["done"]) for row in table.rows] == [
        ("重复计算治理", "task", True),
        ("重复计算治理", "task", False),
        ("重复计算治理", "acceptance", False),
        ("调度空转调优", "task", False),
    ]
    assert table.rows[0]["start_date"] == "2026-08-17" and table.rows[0]["due_date"] == "2026-08-21"
    assert table.rows[2]["start_date"] is None
    assert table.rows[3]["due_date"] == "2026-09-11"


def test_parse_csv_without_nodes_and_leading_noise():
    table = parse_table('本周临时任务\n具体事项,截止时间\n"证书续期, 核对到期日",2026-10-28\n')
    assert not table.has_nodes
    assert table.rows == [{"line": 3, "node": None, "kind": "task", "title": "证书续期, 核对到期日",
                           "start_date": None, "due_date": "2026-10-28", "done": False}]


@pytest.mark.parametrize("text, message", [
    ("", "没有可导入"),
    ("标题A\t截止\nx\t2026-10-01\n", "表头"),
    ("项目节点\t具体事项\n产物验证\t验收\n", "前面没有项目节点"),
    ("项目节点\t具体事项\n\tx\n", "缺少项目节点"),
    ("具体事项\t截止时间\nx\t下周\n", "日期无法识别"),
    ("具体事项\t截止时间\nx\t2026-02-30\n", "日期无法识别"),
    ("具体事项\t完成状态\nx\t也许\n", "完成状态"),
    ("具体事项\t开始时间\t截止时间\nx\t2026-10-02\t2026-10-01\n", "晚于"),
    ("具体事项\t截止时间\n\t2026-10-01\n", "缺少具体事项"),
    ("具体事项\n", "没有事项"),
])
def test_parse_errors(text, message):
    with pytest.raises(DataStoreError, match=message):
        parse_table(text)
