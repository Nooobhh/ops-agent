"""finish 契约与证据核验(A2 §5.2,2026-10-22 定三态规则):核验只标注,不驳回。

模型只「指出」证据:`ref`(step:<n>,本任务 records 的第 n 个步骤)+ `match`(该步输出里能定位到
原始行的特征片段);原文由 Harness 从 records 摘录,报告展示的是服务器原文而非模型的转述。

每个猜测三态,Harness 只降级不升级:
- 成立:窗口内的异常观测(observation)+ 判据(criterion)
- 推翻:两个独立来源(命令不同的步骤)的反证(absence),至少一条时间落在故障窗口内
观测/反证的时间取摘录行里的时间戳;摘录没有时间戳(free、jstat、配置等现状输出)时取该步骤的执行时间
- 缺少证据:其余情况;旁证(related)与知识库只展示,不计入依据
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

SUPPORTED = "supported"
INSUFFICIENT = "insufficient"
REFUTED = "refuted"
STATUS_LABELS = {SUPPORTED: "成立", INSUFFICIENT: "缺少证据", REFUTED: "推翻"}
ROLE_LABELS = {"observation": "异常观测", "criterion": "判据", "absence": "反证", "fact": "事实",
               "related": "旁证"}
MIN_MATCH_CHARS = 8
EXTRACT_LINES = 3
# 观测时间与故障窗口的容差:心跳、GC 等异常往往先于故障出现
WINDOW_TOLERANCE = timedelta(minutes=10)

_STEP_REF = re.compile(r"^step[:\-](\d{1,4})$")
_PLAYBOOK_REF = re.compile(r"^playbook[:\-](\d{1,3})\.(\d{1,3})$")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_MONTHS = {name: index for index, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_TIMESTAMPS = (
    # 2026-10-21 13:18:20 / 2026-10-21T13:18:20 / 2026-10-21 13:18
    re.compile(r"(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})[T ](?P<h>\d{2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?"),
    # Spark / log4j 短年份:26/10/21 13:21:29
    re.compile(r"(?<!\d)(?P<yy>\d{2})/(?P<mo>\d{2})/(?P<d>\d{2}) (?P<h>\d{2}):(?P<mi>\d{2}):(?P<s>\d{2})"),
    # syslog / date 输出:Oct 21 13:21:29
    re.compile(r"(?P<mon>[A-Z][a-z]{2}) +(?P<d>\d{1,2}) (?P<h>\d{2}):(?P<mi>\d{2}):(?P<s>\d{2})"),
)


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["observation", "criterion", "absence"]
    ref: str = Field(min_length=1)
    match: str = Field(min_length=1)
    value: Optional[str] = None
    note: Optional[str] = None


class Pointer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: str = Field(min_length=1)
    match: str = Field(min_length=1)
    note: Optional[str] = None


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    statement: str = Field(min_length=1)
    status: Literal["supported", "insufficient", "refuted"]
    evidence: List[EvidenceItem] = Field(default_factory=list)
    related: List[Pointer] = Field(default_factory=list)


class Window(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: str
    end: str


class FinishArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str = Field(min_length=1)
    incident_window: Optional[Window] = None
    hypotheses: List[Hypothesis] = Field(default_factory=list)
    # 无需猜测的问答(如当前负载)直接给支撑回答的事实
    facts: List[Pointer] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"]
    uncertain: List[str] = Field(default_factory=list)
    remediation: List[str] = Field(default_factory=list)
    severity: Optional[Literal["P0", "P1", "P2", "ok"]] = None
    oplogs: List[str] = Field(default_factory=list)


_POINTER = {"type": "object", "properties": {
    "ref": {"type": "string", "description": "工具结果或开场 playbook 结果里的 ref,如 step:3、playbook:1.2"},
    "match": {"type": "string", "description": "该步输出里一行原文中的特征片段(≥8 字),Harness 据此摘录原文"},
    "note": {"type": "string"},
}, "required": ["ref", "match"]}

FINISH_SCHEMA: Dict[str, Any] = {"type": "function", "function": {
    "name": "finish",
    "description": (
        "收口。证据只需指出位置(ref + match),原文由 Harness 摘录;不会因证据不足驳回,"
        "但每个猜测的状态按规则核验,不满足的降为缺少证据:"
        "成立=故障窗口内的异常观测(observation)+判据(criterion,如阈值/配置);"
        "推翻=两个不同命令在故障窗口内的反证(absence,查过且未见异常);"
        "只看到无法关联的其他错误放 related,猜测保持缺少证据"
    ),
    "parameters": {"type": "object", "properties": {
        "answer": {"type": "string", "description": "给提问者的完整回答"},
        "incident_window": {"type": "object", "properties": {
            "start": {"type": "string", "description": "故障开始,如 2026-10-21 13:18;问现状时填本次排查时段"},
            "end": {"type": "string"},
        }, "required": ["start", "end"]},
        "hypotheses": {"type": "array", "items": {"type": "object", "properties": {
            "statement": {"type": "string"},
            "status": {"type": "string", "enum": ["supported", "insufficient", "refuted"]},
            "evidence": {"type": "array", "items": {"type": "object", "properties": {
                "role": {"type": "string", "enum": ["observation", "criterion", "absence"]},
                **_POINTER["properties"],
                "value": {"type": "string", "description": "观测值或判据值,如 30s;须出现在摘录原文里"},
            }, "required": ["role", "ref", "match"]}},
            "related": {"type": "array", "items": _POINTER},
        }, "required": ["statement", "status"]}},
        "facts": {"type": "array", "items": _POINTER,
                  "description": "无需猜测的问答:支撑回答的事实"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "uncertain": {"type": "array", "items": {"type": "string"}},
        "remediation": {"type": "array", "items": {"type": "string"},
                        "description": "处置建议;写操作须注明需人工审批"},
        "severity": {"type": "string", "enum": ["P0", "P1", "P2", "ok"],
                     "description": "影响程度;确认无异常或纯问答填 ok"},
        "oplogs": {"type": "array", "items": {"type": "string"},
                   "description": "执行过经人工批准的写操作时,引用 knowledge/<oplog 文档>"},
    }, "required": ["answer", "confidence"]},
}}


def normalize(text: str) -> str:
    return " ".join(str(text or "").split())


def step_index(ref: str) -> Optional[int]:
    match = _STEP_REF.match(ref.strip())
    return int(match.group(1)) if match else None


def evidence_sources(session: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """records session → 可引用的证据来源:step:<n> 为节点命令/诊断,playbook:<n>.<k> 为 playbook 步骤。"""
    sources: Dict[str, Dict[str, Any]] = {}
    for index, step in enumerate(session.get("steps") or [], 1):
        if isinstance(step, dict):
            sources[f"step:{index}"] = step
    for index, result in enumerate(session.get("playbook_results") or [], 1):
        steps = result.get("steps") if isinstance(result, dict) else None
        for number, step in enumerate(steps or [], 1):
            if isinstance(step, dict):
                sources[f"playbook:{index}.{number}"] = {
                    "cmd": f"playbook:{result.get('playbook')} {step.get('cmd', '')}",
                    "stdout": str(step.get("actual") or ""),
                    "recorded_at": result.get("recorded_at"),
                }
    return sources


def canonical_ref(ref: str) -> Optional[str]:
    ref = ref.strip()
    index = step_index(ref)
    if index is not None:
        return f"step:{index}"
    match = _PLAYBOOK_REF.match(ref)
    return f"playbook:{int(match.group(1))}.{int(match.group(2))}" if match else None


def extract(sources, ref: str, match: str, value: Optional[str] = None) -> Dict[str, Any]:
    """在 ref 指向的输出里找含 match 的原始行(最多 3 行窗口);找不到给出原因。

    给了 value 时优先取同时含该数值的窗口(配置名与值常分在相邻两行),没有再退回只含 match 的。
    sources 为 evidence_sources() 的结果;也接受步骤列表(视为 step:1..n)。
    """
    if isinstance(sources, list):
        sources = {f"step:{index}": step for index, step in enumerate(sources, 1)}
    if ref.strip().startswith("kb:"):
        return {"ref": ref, "found": False, "reason": "知识库内容只作参考,不是服务器事实"}
    key = canonical_ref(ref)
    if key is None or key not in sources:
        return {"ref": ref, "found": False, "reason": "引用的步骤不存在"}
    needle = normalize(match)
    if len(needle.replace(" ", "")) < MIN_MATCH_CHARS:
        return {"ref": key, "found": False, "reason": f"match 过短(至少 {MIN_MATCH_CHARS} 个字符)"}
    step = sources[key]
    lines = f"{step.get('stdout') or ''}\n{step.get('stderr') or ''}".splitlines()
    fallback = None
    for width in range(1, EXTRACT_LINES + 1):
        for start in range(len(lines) - width + 1):
            window = lines[start:start + width]
            if needle not in normalize(" ".join(window)):
                continue
            text = "\n".join(line.rstrip()[:500] for line in window)
            hit = {"ref": key, "found": True, "cmd": step.get("cmd"), "text": text,
                   "recorded_at": step.get("recorded_at")}
            if not value or _value_ok(value, text):
                return hit
            fallback = fallback or hit
    return fallback or {"ref": key, "found": False, "reason": "该步骤输出里没有这段原文"}


def parse_time(text: str, *, year: Optional[int] = None) -> Optional[datetime]:
    """取文本里第一个可识别的时间戳(不含时区,与窗口同按本地时间比较)。"""
    for pattern in _TIMESTAMPS:
        found = pattern.search(text or "")
        if not found:
            continue
        parts = found.groupdict()
        try:
            if parts.get("y"):
                value_year = int(parts["y"])
            elif parts.get("yy"):
                value_year = 2000 + int(parts["yy"])
            else:
                value_year = year or datetime.now().year
            month = int(parts["mo"]) if parts.get("mo") else _MONTHS.get(parts["mon"].lower())
            if month is None:
                continue
            return datetime(value_year, month, int(parts["d"]), int(parts["h"]), int(parts["mi"]),
                            int(parts.get("s") or 0))
        except (TypeError, ValueError):
            continue
    return None


def _window(args: FinishArgs) -> Optional[Tuple[datetime, datetime]]:
    if args.incident_window is None:
        return None
    start = parse_time(args.incident_window.start + (":00" if args.incident_window.start.count(":") == 1 else ""))
    end = parse_time(args.incident_window.end + (":00" if args.incident_window.end.count(":") == 1 else ""),
                     year=start.year if start else None)
    if start is None or end is None:
        return None
    return (start, end) if start <= end else (end, start)


def _moment(found: Dict[str, Any], window: Tuple[datetime, datetime]) -> Tuple[Optional[datetime], str]:
    """观测时间:摘录行里的时间戳优先;没有时取步骤执行时间(现状输出反映的就是执行时刻)。"""
    moment = parse_time(found["text"], year=window[0].year)
    if moment is not None:
        return moment, "excerpt"
    executed = parse_time(str(found.get("recorded_at") or ""))
    return (executed, "executed") if executed is not None else (None, "none")


def _in_window(found: Dict[str, Any], window: Optional[Tuple[datetime, datetime]]) -> Tuple[Optional[bool], str]:
    """None 表示既无摘录时间也无执行时间;True/False 为是否落在窗口(含容差)内。"""
    if window is None:
        return None, "none"
    moment, source = _moment(found, window)
    if moment is None:
        return None, source
    return window[0] - WINDOW_TOLERANCE <= moment <= window[1] + WINDOW_TOLERANCE, source


def _value_ok(value: Optional[str], text: str) -> bool:
    numbers = _NUMBER.findall(value or "")
    return all(re.search(rf"(?<![\d.]){re.escape(number)}(?![\d.])", text) for number in numbers)


def _check_item(sources, item, window) -> Dict[str, Any]:
    found = extract(sources, item.ref, item.match, item.value)
    check = {"role": item.role, "ref": found["ref"], "match": item.match, "value": item.value,
             "note": item.note, **{key: found[key] for key in ("found", "text", "reason", "cmd") if key in found}}
    if found["found"]:
        if item.value and not _value_ok(item.value, found["text"]):
            check.update(found=False, reason=f"value {item.value} 不在摘录原文里")
        elif item.role in ("observation", "absence"):
            check["in_window"], check["time_source"] = _in_window(found, window)
    return check


def _final_status(hypothesis: Hypothesis, checks: List[Dict[str, Any]], window) -> Tuple[str, List[str]]:
    declared, reasons = hypothesis.status, []
    found = [item for item in checks if item["found"]]
    if declared == SUPPORTED:
        observations = [item for item in found if item["role"] == "observation"]
        if not observations:
            reasons.append("缺少摘录成功的异常观测")
        elif window is None:
            reasons.append("未给出故障时间窗口,观测时间无法核对")
        elif not any(item.get("in_window") for item in observations):
            reasons.append("异常观测的时间(摘录时间或执行时间)与故障窗口对不上")
        if not any(item["role"] == "criterion" for item in found):
            reasons.append("缺少摘录成功的判据(阈值/配置/预期值)")
        return (SUPPORTED, []) if not reasons else (INSUFFICIENT, reasons)
    if declared == REFUTED:
        absences = [item for item in found if item["role"] == "absence"]
        sources = {normalize(re.sub(r"^\[[^\]]*\]\s*", "", str(item.get("cmd") or ""))) for item in absences}
        if len(sources) < 2:
            reasons.append("推翻需要两个不同命令的反证")
        if window is None:
            reasons.append("未给出故障时间窗口,反证时间无法核对")
        elif not any(item.get("in_window") for item in absences):
            reasons.append("反证没有一条时间(摘录时间或执行时间)落在故障窗口内")
        return (REFUTED, []) if not reasons else (INSUFFICIENT, reasons)
    return INSUFFICIENT, []


def verify(args: FinishArgs, sources) -> Dict[str, Any]:
    """摘录全部证据并按三态规则定每个猜测的最终状态;grounded 表示结论有服务器事实依据。"""
    window = _window(args)
    hypotheses = []
    for hypothesis in args.hypotheses:
        checks = [_check_item(sources, item, window) for item in hypothesis.evidence]
        status, reasons = _final_status(hypothesis, checks, window)
        related = [{**extract(sources, item.ref, item.match), "match": item.match, "note": item.note}
                   for item in hypothesis.related]
        hypotheses.append({"statement": hypothesis.statement, "declared": hypothesis.status,
                           "status": status, "reasons": reasons, "evidence": checks, "related": related})
    facts = [{**extract(sources, item.ref, item.match), "match": item.match, "note": item.note}
             for item in args.facts]
    grounded = any(item["status"] == SUPPORTED for item in hypotheses) or (
        not hypotheses and any(item["found"] for item in facts))
    return {"window": args.incident_window.model_dump() if args.incident_window else None,
            "hypotheses": hypotheses, "facts": facts, "grounded": grounded}


def _evidence_line(item: Dict[str, Any]) -> str:
    label = ROLE_LABELS.get(item.get("role") or "fact", "事实")
    if item["found"]:
        return f"  [{label}·{item['ref']}] " + " ⏎ ".join(item["text"].splitlines())
    return f"  [{label}·{item['ref']}·未摘录] {item['match']}({item.get('reason', '')})"


def report_fields(args: FinishArgs, result: Dict[str, Any]) -> Dict[str, Any]:
    """finish → 报告字段。有成立的猜测(或无猜测时有摘录成功的事实)且置信度非 low 才 conclusive。"""
    verdict = "conclusive" if result["grounded"] and args.confidence != "low" else "insufficient"
    supported = [item["statement"] for item in result["hypotheses"] if item["status"] == SUPPORTED]
    evidence: List[str] = []
    for item in result["hypotheses"]:
        header = f"[{STATUS_LABELS[item['status']]}] {item['statement']}"
        if item["status"] != item["declared"]:
            header += f"(模型判为{STATUS_LABELS[item['declared']]},降级:{';'.join(item['reasons'])})"
        evidence.append(header)
        evidence.extend(_evidence_line(check) for check in item["evidence"])
        evidence.extend(_evidence_line({**check, "role": "related"}) for check in item["related"])
    if result["facts"]:
        evidence.append("[事实]")
        evidence.extend(_evidence_line({**check, "role": "fact"}) for check in result["facts"])
    # 未给影响程度时:有成立的根因按最低影响 P2,否则按 ok
    severity = args.severity or ("P2" if supported else "ok")
    return {
        "verdict": verdict,
        "severity": severity,
        "root_cause": ";".join(supported) or args.answer,
        "evidence": evidence,
        "remediation": list(args.remediation),
        "oplogs": list(args.oplogs),
        "finish": {
            "answer": args.answer,
            "confidence": args.confidence,
            "window": result["window"],
            "uncertain": list(args.uncertain) + [
                f"{item['statement']}:{';'.join(item['reasons'])}"
                for item in result["hypotheses"] if item["reasons"]
            ],
            "hypotheses": [{key: item[key] for key in ("statement", "declared", "status")}
                           for item in result["hypotheses"]],
        },
    }
