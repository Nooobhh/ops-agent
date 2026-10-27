"""录制池:历史环境调用按规范签名索引,回放时三级匹配(精确 → 宽松 → 未命中)。"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from lib.triage_agent.command_policy import render_pipeline

ENV_TOOLS = (
    "run_diagnostic",
    "run_command",
    "run_registered_playbook",
    "read_resource_risk",
    "read_resource_snapshot",
    "read_task_dependencies",
)
_DIGITS = re.compile(r"\d+(?:\.\d+)?")
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True)
class SignatureMap:
    """插件化后新工具名/参数名/诊断 ID 到旧签名的翻译表;缺省为恒等。"""

    tool_aliases: Dict[str, str] = field(default_factory=dict)
    diagnostic_aliases: Dict[str, str] = field(default_factory=dict)
    arg_renames: Dict[str, Dict[str, str]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Optional[Path]) -> "SignatureMap":
        if path is None:
            return cls()
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls(
            tool_aliases=dict(raw.get("tool_aliases") or {}),
            diagnostic_aliases=dict(raw.get("diagnostic_aliases") or {}),
            arg_renames={
                str(tool): dict(renames or {})
                for tool, renames in (raw.get("arg_renames") or {}).items()
            },
        )

    def translate(self, tool: str, args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        renames = self.arg_renames.get(tool) or {}
        translated = {renames.get(key, key): value for key, value in args.items()}
        diagnostic_id = translated.get("diagnostic_id")
        if isinstance(diagnostic_id, str):
            translated["diagnostic_id"] = self.diagnostic_aliases.get(diagnostic_id, diagnostic_id)
        return self.tool_aliases.get(tool, tool), translated


@dataclass(frozen=True)
class CallKey:
    exact: str
    # 宽松骨架:数值抹成 #,同骨架的候选再按数值距离挑最近的一条
    loose: str
    numbers: Tuple[float, ...]


def _canon(value: Dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _command_text(args: Dict[str, Any]) -> str:
    command = args.get("command")
    if not isinstance(command, str):
        command = render_pipeline(args.get("pipeline") or [])
    return _SPACES.sub(" ", command).strip()


def call_key(tool: str, args: Dict[str, Any], *, platform: Optional[str], node: Optional[str]) -> CallKey:
    base: Dict[str, Any] = {"tool": tool}
    numbers: List[float] = []
    if tool == "run_diagnostic":
        params = args.get("params") or {}
        base.update(platform=platform, node=node, diagnostic_id=args.get("diagnostic_id"))
        exact = _canon({**base, "params": params})
        loose_params = {}
        for key in sorted(params):
            value = params[key]
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                numbers.append(float(value))
                loose_params[key] = "#"
            else:
                loose_params[key] = value
        return CallKey(exact, _canon({**base, "params": loose_params}), tuple(numbers))
    if tool == "run_command":
        command = _command_text(args)
        base.update(platform=platform, node=node)
        numbers = [float(item) for item in _DIGITS.findall(command)]
        return CallKey(
            _canon({**base, "command": command}),
            _canon({**base, "command": _DIGITS.sub("#", command)}),
            tuple(numbers),
        )
    if tool == "run_registered_playbook":
        base.update(platform=platform, node=node, playbook=args.get("playbook"))
    elif tool == "read_resource_risk":
        base.update(risk_ref=args.get("risk_ref"))
    elif tool == "read_resource_snapshot":
        base.update(platform=platform, node=node)
    elif tool == "read_task_dependencies":
        base.update(process_id=args.get("process_id"), task_id=args.get("task_id"))
    else:
        raise ValueError(f"不是环境工具: {tool}")
    key = _canon(base)
    return CallKey(key, key, ())


@dataclass(frozen=True)
class Recording:
    call_id: str
    tool: str
    args: Dict[str, Any]
    platform: Optional[str]
    node: Optional[str]
    result: Dict[str, Any]
    source_task: str

    def key(self, signature_map: SignatureMap) -> CallKey:
        tool, args = signature_map.translate(self.tool, self.args)
        return call_key(tool, args, platform=self.platform, node=self.node)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool": self.tool,
            "args": self.args,
            "platform": self.platform,
            "node": self.node,
            "result": self.result,
            "source_task": self.source_task,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Recording":
        return cls(
            call_id=str(raw["call_id"]),
            tool=str(raw["tool"]),
            args=dict(raw.get("args") or {}),
            platform=raw.get("platform"),
            node=raw.get("node"),
            result=dict(raw.get("result") or {}),
            source_task=str(raw.get("source_task") or ""),
        )


def _distance(left: Tuple[float, ...], right: Tuple[float, ...]) -> float:
    return sum(abs(math.log((abs(a) + 1) / (abs(b) + 1))) for a, b in zip(left, right))


class RecordingPool:
    """回放期间唯一的环境数据来源;每次查询记一条 lookup 供指标统计。"""

    def __init__(
        self,
        recordings: List[Recording],
        *,
        signature_map: Optional[SignatureMap] = None,
        snapshot_fallback: Optional[Dict[str, Any]] = None,
    ):
        self.signature_map = signature_map or SignatureMap()
        self.snapshot_fallback = snapshot_fallback
        self.lookups: List[Dict[str, Any]] = []
        self._exact: Dict[str, Recording] = {}
        self._loose: Dict[str, List[Tuple[CallKey, Recording]]] = {}
        for recording in recordings:
            key = recording.key(self.signature_map)
            # 录制按主源在前排序:同签名先到先得,合并的辅助任务只补缺
            self._exact.setdefault(key.exact, recording)
            self._loose.setdefault(key.loose, []).append((key, recording))

    def match(
        self,
        tool: str,
        args: Dict[str, Any],
        *,
        platform: Optional[str],
        node: Optional[str],
        miss_kind: str = "miss",
    ) -> Tuple[Optional[Recording], str]:
        tool, args = self.signature_map.translate(tool, args)
        key = call_key(tool, args, platform=platform, node=node)
        recording = self._exact.get(key.exact)
        kind = "exact"
        if recording is None:
            candidates = [
                (_distance(key.numbers, candidate.numbers), recording)
                for candidate, recording in self._loose.get(key.loose, [])
                if len(candidate.numbers) == len(key.numbers)
            ]
            if candidates:
                recording = min(candidates, key=lambda item: item[0])[1]
                kind = "fuzzy"
            else:
                kind = miss_kind
        self.lookups.append({
            "tool": tool,
            "match": kind,
            "signature": key.exact,
            "recording": recording.call_id if recording else None,
        })
        return recording, kind


_PLAYBOOK_INVOCATION = re.compile(r"^(alert-playbook-\d+|bootstrap-playbook)$")


def _looks_like_env_result(tool: str, result: Any) -> bool:
    """只收真实环境产出;命令审批放行便条、失败对象等不是可回放的观测结果。"""
    if not isinstance(result, dict) or result.get("failed"):
        return False
    if tool in {"run_diagnostic", "run_command"}:
        return "stdout" in result or isinstance(result.get("execution"), dict)
    if tool == "run_registered_playbook":
        return "verdict" in result or "steps" in result
    if tool == "read_resource_risk":
        return "finding" in result
    if tool == "read_resource_snapshot":
        return "available" in result
    return True


def extract_recordings(task_dir: Path) -> List[Recording]:
    """从一个历史任务目录取出全部可回放的环境调用(按调用顺序)。"""
    task_dir = Path(task_dir)
    task = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    invocations_path = task_dir / "invocations.json"
    invocations = (
        json.loads(invocations_path.read_text(encoding="utf-8"))
        if invocations_path.is_file() else {}
    )
    calls: Dict[str, Dict[str, Any]] = {}
    for line in (task_dir / "messages.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        payload = event.get("payload") or {}
        if event.get("type") == "tool_call":
            calls[str(payload.get("tool_call_id"))] = {
                "name": payload.get("name"),
                "arguments": payload.get("arguments") or {},
            }
        elif event.get("type") == "agent_playbook_result":
            calls.setdefault(str(payload.get("invocation_id")), {
                "name": "run_registered_playbook",
                "arguments": {"playbook": payload.get("playbook")},
            })
    recordings: List[Recording] = []
    for call_id, invocation in invocations.items():
        if not isinstance(invocation, dict) or invocation.get("status") != "done":
            continue
        call = calls.get(call_id)
        if call is None and _PLAYBOOK_INVOCATION.match(call_id):
            continue  # 预跑 playbook 缺少对应事件时无法知道 playbook 路径
        if call is None or call["name"] not in ENV_TOOLS:
            continue
        result = invocation.get("result")
        if not _looks_like_env_result(call["name"], result):
            continue
        args = dict(call["arguments"])
        target = result.get("target") if isinstance(result.get("target"), dict) else {}
        node = args.get("node") or target.get("node") or result.get("node") or task.get("node")
        platform = target.get("platform") or result.get("platform") or task.get("platform")
        if call["name"] == "run_command" and not isinstance(args.get("pipeline"), list):
            args["command"] = result.get("command") or args.get("command") or ""
        recordings.append(Recording(
            call_id=call_id,
            tool=call["name"],
            args=args,
            platform=platform,
            node=node,
            result=result,
            source_task=str(task.get("task_id") or task_dir.name),
        ))
    return recordings
