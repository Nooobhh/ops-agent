"""Harness 档位(A2 §4.5;A3.7 起只剩自主档)。

引导档评估无稳定收益,已合并回自主档;结构档(旧编排器)已删除。新任务一律固化为 autonomous。
harness_tier 为空或 structured 的是 A3.7 之前由旧编排器执行的任务:历史可读,不再执行或续查。
guided 只出现在 A3.6 评估期任务里,按自主档运行(开关取值以任务快照为准)。
"""
from __future__ import annotations

from .models import TriageTask

CURRENT_TIER = "autonomous"
_NEW_RUNTIME_TIERS = frozenset({"autonomous", "guided"})
LEGACY_TASK_MESSAGE = "旧版 Agent 已下线(A3.7):该任务只能查看,请新建任务继续排查"


def uses_new_runtime(task: TriageTask) -> bool:
    return task.harness_tier in _NEW_RUNTIME_TIERS
