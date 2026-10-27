"""档位开关(A2 §4.1)与预算硬上限(§3.8 / §11 N3)。

A3.7 起只剩自主档(引导档评估无稳定收益,已合并)。开关取值在任务首次执行时固化进
task.harness_switches,任务内不随代码常量变化。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any, Dict, Optional

# 硬上限:防失控与控成本,所有档位一致;到顶进入收口回合,不能续期
HARD_TURNS = 80
HARD_SECONDS = 60 * 60
HARD_NODE_COMMANDS = 200
HARD_INPUT_TOKENS = 6_000_000
# 连续非法工具参数达到此数进入收口回合(§5.1)
INVALID_ARGUMENT_LIMIT = 3
# 人工批准一次续期放宽的软预算,不超过硬上限
GRANT_TURNS = 25
GRANT_SECONDS = 15 * 60
GRANT_NODE_COMMANDS = 60


@dataclass(frozen=True)
class TierSwitches:
    tier: str
    # 单次工具结果给模型的投影上限(字符,超出保留头尾)
    projection_chars: int
    soft_turns: int
    soft_seconds: int
    soft_node_commands: int

    def snapshot(self) -> Dict[str, Any]:
        return asdict(self)


AUTONOMOUS = TierSwitches("autonomous", 64 * 1024, 60, 40 * 60, 150)


def switches_from(snapshot: Optional[Dict[str, Any]]) -> TierSwitches:
    """任务快照优先;评估期 guided 快照里多出的字段(guidance)忽略。"""
    if snapshot:
        names = {item.name for item in fields(TierSwitches)}
        return TierSwitches(**{key: value for key, value in snapshot.items() if key in names})
    return AUTONOMOUS


@dataclass(frozen=True)
class Budget:
    turns: int
    seconds: int
    node_commands: int


def soft_budget(switches: TierSwitches, grants: int) -> Budget:
    return Budget(
        turns=min(HARD_TURNS, switches.soft_turns + grants * GRANT_TURNS),
        seconds=min(HARD_SECONDS, switches.soft_seconds + grants * GRANT_SECONDS),
        node_commands=min(HARD_NODE_COMMANDS, switches.soft_node_commands + grants * GRANT_NODE_COMMANDS),
    )


HARD_BUDGET = Budget(HARD_TURNS, HARD_SECONDS, HARD_NODE_COMMANDS)
