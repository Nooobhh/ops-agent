"""一批工具调用(模型一次发出的 tool_calls)的执行策略,按 failure_scope 处理失败。

- 已执行命令的非零退出码不是 Harness 拒绝;模型可自纠的软失败只跳过依赖型调用,
  独立只读调用继续
- 硬失败(不可恢复、远端基础设施故障、failure_scope=hard)跳过同批其余调用
- 额度耗尽跳过其余调用后交回调用方,由它决定续期还是终止
每个被跳过的调用都补一条 tool_result,模型历史始终协议完整。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from .diagnostics import DIAGNOSTIC_NOT_REGISTERED
from .errors import BudgetExhausted
from .model_client import ToolCall
from .models import TriageTask


@dataclass
class BatchOutcome:
    task: TriageTask
    # 某个调用让任务进入终态/挂起(审批、needs_input 等)时的任务
    terminal: Optional[TriageTask] = None
    # 本批至少有一个调用执行成功(连续驳回计数据此清零)
    executed: bool = False
    rejections: List[Dict[str, Any]] = field(default_factory=list)
    last_rejection: Optional[Exception] = None
    budget_exhausted: Optional[BudgetExhausted] = None


def counts_as_root_rejection(exc: Exception) -> bool:
    """Synthetic evidence-reference misses do not create a new root cause.

    Nor does an unregistered diagnostic id: the rejection carries the
    nearest registered ids, and the tool-turn budget still bounds retries.
    """
    return getattr(exc, "code", None) not in {
        "EVIDENCE_NOT_EXECUTED", DIAGNOSTIC_NOT_REGISTERED,
    }


def run_tool_batch(
    task: TriageTask,
    calls: Sequence[ToolCall],
    *,
    batch_id: str,
    handle: Callable[..., tuple],
    skip: Callable[..., None],
    refresh: Callable[[str], TriageTask],
    is_recoverable: Callable[[Exception], bool],
    continue_after_error: Callable[[str, Exception], bool],
    is_dependent: Callable[[str], bool],
) -> BatchOutcome:
    """逐个执行;handle(task, call, batch_id=, batch_index=, batch_size=) 返回 (task, terminal)。

    不可恢复的异常在跳过其余调用后原样抛出。
    """
    outcome = BatchOutcome(task=task)
    skipped: set[int] = set()
    for index, call in enumerate(calls):
        if index in skipped:
            continue
        if outcome.terminal is not None:
            skip(
                task.task_id, call, "任务已进入终态，本调用未执行",
                batch_id=batch_id, batch_index=index,
            )
            continue
        try:
            task, terminal = handle(
                task,
                call,
                batch_id=batch_id,
                batch_index=index,
                batch_size=len(calls),
            )
            outcome.executed = True
        except BudgetExhausted as exc:
            # 与轮次/时限同属预算耗尽:由调用方决定续期,不按驳回计数
            for rest_index in range(index + 1, len(calls)):
                skip(
                    task.task_id,
                    calls[rest_index],
                    f"{exc}，本调用未执行",
                    batch_id=batch_id,
                    batch_index=rest_index,
                )
            outcome.task = refresh(task.task_id)
            outcome.budget_exhausted = exc
            return outcome
        except Exception as exc:
            if not is_recoverable(exc):
                for rest_index in range(index + 1, len(calls)):
                    skip(
                        task.task_id,
                        calls[rest_index],
                        "前序调用发生硬失败，本调用未执行",
                        batch_id=batch_id,
                        batch_index=rest_index,
                    )
                raise
            if counts_as_root_rejection(exc):
                outcome.rejections.append({
                    "call_id": call.call_id,
                    "tool": call.name,
                    "error": str(exc),
                    "code": str(getattr(exc, "code", "") or type(exc).__name__),
                })
                outcome.last_rejection = exc
            # 软失败已经作为 tool_result 落档。继续执行同批后续
            # 调用，让独立只读诊断仍有机会提供后续假设所需的证据。
            # 每个后续调用仍会独立经过 admission、安全和预算检查。
            task = refresh(task.task_id)
            if continue_after_error(call.name, exc):
                for rest_index in range(index + 1, len(calls)):
                    rest = calls[rest_index]
                    if not is_dependent(rest.name):
                        continue
                    skip(
                        task.task_id,
                        rest,
                        "依赖的前序调用失败，本调用未执行",
                        batch_id=batch_id,
                        batch_index=rest_index,
                    )
                    skipped.add(rest_index)
                continue
            for rest_index in range(index + 1, len(calls)):
                skip(
                    task.task_id,
                    calls[rest_index],
                    "当前调用失败，本调用未执行",
                    batch_id=batch_id,
                    batch_index=rest_index,
                )
            break
        if terminal:
            outcome.terminal = task
    outcome.task = task
    return outcome
