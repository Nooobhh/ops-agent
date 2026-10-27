"""playbook JSON 契约解析与校验(spec §6)。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

VERDICTS = {"conclusive", "needs_llm", "insufficient"}
SEVERITIES = {"P0", "P1", "P2", "ok"}
_STEP_KEYS = {"name", "cmd", "expect", "actual", "pass", "raw"}


@dataclass
class Step:
    name: str
    cmd: str
    expect: str
    actual: str
    pass_: bool
    raw: str


@dataclass
class PlaybookResult:
    playbook: str
    node: str
    verdict: str
    severity: str
    conclusion_hint: str
    steps: List[Step]
    metrics: Dict[str, float] = field(default_factory=dict)
    next_playbook: Optional[str] = None
    raw_dir: Optional[str] = None

    def to_session_dict(self) -> Dict[str, Any]:
        """转成 session.json 里 playbook_results[] 一条的格式(pass_ → pass 键名)。"""
        return {
            "playbook": self.playbook,
            "node": self.node,
            "verdict": self.verdict,
            "severity": self.severity,
            "conclusion_hint": self.conclusion_hint,
            "steps": [{"name": s.name, "cmd": s.cmd, "expect": s.expect,
                       "actual": s.actual, "pass": s.pass_, "raw": s.raw}
                      for s in self.steps],
            "metrics": dict(self.metrics),
            "next_playbook": self.next_playbook,
            "raw_dir": self.raw_dir,
        }


def parse_result(text: str) -> PlaybookResult:
    decoder = json.JSONDecoder()
    data = None
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            data, _ = decoder.raw_decode(text[i:])
            break
        except json.JSONDecodeError:
            continue
    if data is None:
        raise ValueError(f"输出中没有可解析 JSON: {text[:200]!r}")
    if data.get("verdict") not in VERDICTS:
        raise ValueError(f"非法 verdict: {data.get('verdict')!r}")
    if data.get("severity") not in SEVERITIES:
        raise ValueError(f"非法 severity: {data.get('severity')!r}")
    for k in ("playbook", "node"):
        if k not in data:
            raise ValueError(f"缺少顶层字段 {k!r}: {list(data.keys())}")
    steps = []
    for s in data.get("steps", []):
        missing = _STEP_KEYS - set(s)
        if missing:
            raise ValueError(f"step 缺字段 {missing}: {s.get('name')!r}")
        if not isinstance(s["pass"], bool):
            raise ValueError(f"step.pass 必须是 JSON bool,得到 {type(s['pass']).__name__}: {s['pass']!r}")
        steps.append(Step(name=s["name"], cmd=s["cmd"], expect=s["expect"],
                          actual=s["actual"], pass_=s["pass"], raw=s["raw"]))
    return PlaybookResult(
        playbook=data["playbook"], node=data["node"], verdict=data["verdict"],
        severity=data["severity"], conclusion_hint=data.get("conclusion_hint", ""),
        steps=steps, metrics=dict(data.get("metrics", {})),
        next_playbook=data.get("next_playbook"), raw_dir=data.get("raw_dir"),
    )


def patrol_node_healthy(result: Dict[str, Any]) -> bool:
    """巡检快照里单节点判「通过」:无执行错误、无失败步骤、severity 为 ok。

    刻意不看 verdict —— 巡检 playbook 一律 emit needs_llm(采集与判读分离),
    以 verdict 判健康会让通过数恒为 0。与 lib/alerts/sources.py 的告警判定同口径。
    """
    if "error" in result:
        return False
    if any(step.get("pass") is False for step in (result.get("steps") or [])):
        return False
    return str(result.get("severity") or "").lower() == "ok"
