"""排查 session 落档:session.json(结构化)+ report.md(人读)。

session.json = LLM 与人的完整交接件(所有命令/输出/verdict);
report.md    = 人读结论,按固定 5 段(结论/证据/根因/处置建议/沉淀建议)生成。
"""
from __future__ import annotations

import json
import sys
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from lib.datastore.models import AUTHOR_ID_RE as _AUTHOR_ID_RE
from lib.envpack import env_timezone
from lib.kb.bases import discover_bases, resolve_doc_id, resolve_knowledge_root, split_doc_path
from lib.sanitize import redact_secrets

REPORT_RENDERER_VERSION = "record-report-v2"

SESSION_SCHEMA_KEYS = {
    "run_id", "node", "trigger", "platform", "service",
    "playbook_results", "steps",
    "verdict", "severity", "root_cause", "evidence", "remediation", "oplogs",
    "report_renderer_version", "amend_count", "last_amended_at",
    "superseded_evidence",
}


class OplogMissing(Exception):
    pass


class AmendmentRejected(ValueError):
    """Model-correctable amendment contract failure."""


@dataclass
class TroubleshootSession:
    dir: Path
    run_id: str
    # 校验 oplog 时据此找知识库根;记录根可在数据根之外,不能再从 dir 向上推
    data_root: Optional[Path] = field(default=None, compare=False)

    def _load(self) -> Dict[str, Any]:
        return json.loads((self.dir / "session.json").read_text(encoding="utf-8"))

    def _save(self, data: Dict[str, Any]) -> None:
        (self.dir / "session.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False),
            encoding="utf-8",
        )

    def update_context(
        self,
        *,
        platform: str,
        node: str,
        service: str = "",
        session_type: str = "triage",
        nodes: Optional[List[str]] = None,
    ) -> None:
        data = self._load()
        data.update({"platform": platform, "node": node, "service": service})
        if session_type != "triage" or nodes is not None:
            data["session_type"] = session_type
            data["nodes"] = list(nodes or [node])
        else:
            data.pop("session_type", None)
            data.pop("nodes", None)
        self._save(data)

    def record_playbook_result(self, result: Dict[str, Any]) -> None:
        data = self._load()
        item = dict(result)
        item.setdefault("recorded_at", _now_shanghai())
        data["playbook_results"].append(item)
        if data.get("report_mode") == "execution":
            data.setdefault("operations", []).append({
                "kind": "playbook",
                "index": len(data["playbook_results"]) - 1,
            })
        self._save(data)
        self._write_live_report(data)

    def record_deep_dive_step(self, cmd: str, exit_code: int,
                              stdout: str, stderr: str = "",
                              approved: bool = False) -> None:
        data = self._load()
        data["steps"].append({
            "cmd": cmd, "exit_code": exit_code,
            "stdout": redact_secrets(stdout), "stderr": redact_secrets(stderr),
            "approved": approved, "recorded_at": _now_shanghai(),
        })
        if data.get("report_mode") == "execution":
            data.setdefault("operations", []).append({
                "kind": "step",
                "index": len(data["steps"]) - 1,
            })
        self._save(data)
        self._write_live_report(data)

    def record_write_operation(self, entry: Dict[str, Any]) -> None:
        """人工批准的写操作结论(按 id 幂等覆盖);有 oplog 引用时并入 oplogs,finish 的 oplog 闸据此通过。"""
        data = self._load()
        item = _redact_values(dict(entry))
        item.setdefault("recorded_at", _now_shanghai())
        operations = [op for op in data.get("write_operations") or [] if op.get("id") != item.get("id")]
        data["write_operations"] = [*operations, item]
        if item.get("oplog"):
            data["oplogs"] = list(dict.fromkeys([*(data.get("oplogs") or []), item["oplog"]]))
        self._save(data)
        if data.get("report_mode") == "execution":
            self._write_live_report(data)
        elif (self.dir / "report.md").exists():
            (self.dir / "report.md").write_text(_render_report(data), encoding="utf-8")

    def record_turn_summary(self, summary: str) -> None:
        """Append one visible Agent answer to the live execution report."""
        text = redact_secrets(str(summary)).strip()
        if not text:
            return
        data = self._load()
        data.setdefault("turn_summaries", []).append({
            "recorded_at": _now_shanghai(),
            "summary": text,
        })
        self._save(data)
        if data.get("steps") or data.get("playbook_results"):
            self._write_live_report(data)

    def _write_live_report(self, data: Optional[Dict[str, Any]] = None) -> None:
        value = data or self._load()
        if value.get("report_mode") != "execution":
            return
        (self.dir / "report.md").write_text(
            _render_report(value), encoding="utf-8",
        )

    def finalize(self, verdict: str, severity: str, root_cause: str,
                 evidence: List[str], remediation: List[str],
                 oplogs: Optional[List[str]] = None) -> None:
        data = self._load()
        approved_steps = any(step.get("approved") is True for step in data.get("steps") or [])
        oplog_list = list(oplogs or [])
        if approved_steps:
            _validate_oplogs(oplog_list, self.data_root or _repo_root_from_session_dir(self.dir))
        data["verdict"] = verdict
        data["severity"] = severity
        data["root_cause"] = root_cause
        data["evidence"] = list(evidence)
        data["remediation"] = list(remediation)
        data["oplogs"] = oplog_list
        # 首轮 finalize 就是第 1 次收口；后续 amend 在此基础上递增。
        data["amend_count"] = max(int(data.get("amend_count") or 0), 1)
        data.setdefault("last_amended_at", None)
        data.setdefault("superseded_evidence", [])
        data["report_renderer_version"] = REPORT_RENDERER_VERSION
        self._save(data)
        (self.dir / "report.md").write_text(
            _render_report(data), encoding="utf-8",
        )

    def record_finish(
        self,
        *,
        verdict: str,
        severity: str,
        root_cause: str,
        evidence: List[str],
        remediation: List[str],
        finish: Dict[str, Any],
        oplogs: Optional[List[str]] = None,
    ) -> None:
        """新运行时收口:首轮写入,续查时证据与建议只增不减、收口次数递增。

        evidence 是带核验标记的条目(不要求逐字出现在采证原文里,核验结果已在标记中);
        finish 存回答、置信度与不确定项,报告渲染时附在结论处。
        """
        data = self._load()
        approved_steps = any(step.get("approved") is True for step in data.get("steps") or [])
        oplog_list = list(dict.fromkeys([*(data.get("oplogs") or []), *(oplogs or [])]))
        if approved_steps:
            _validate_oplogs(oplog_list, self.data_root or _repo_root_from_session_dir(self.dir))
        first = data.get("verdict") is None
        data["verdict"] = verdict
        data["severity"] = severity
        data["root_cause"] = root_cause
        data["evidence"] = list(dict.fromkeys([*(data.get("evidence") or []), *evidence]))
        data["remediation"] = list(dict.fromkeys([*(data.get("remediation") or []), *remediation]))
        data["oplogs"] = oplog_list
        data["finish"] = finish
        if first:
            data["amend_count"] = max(int(data.get("amend_count") or 0), 1)
            data.setdefault("last_amended_at", None)
        else:
            data["amend_count"] = int(data.get("amend_count") or 0) + 1
            data["last_amended_at"] = _now_shanghai()
        data.setdefault("superseded_evidence", [])
        data["report_renderer_version"] = REPORT_RENDERER_VERSION
        self._save(data)
        (self.dir / "report.md").write_text(_render_report(data), encoding="utf-8")

    def amend(
        self,
        *,
        verdict: Optional[str] = None,
        severity: Optional[str] = None,
        root_cause: Optional[str] = None,
        add_evidence: Optional[List[str]] = None,
        supersede: Optional[List[Dict[str, str]]] = None,
        add_remediation: Optional[List[str]] = None,
    ) -> None:
        data = self._load()
        current_evidence = list(data.get("evidence") or [])
        replacements = list(supersede or [])
        normalized = {_normalize_text(item): item for item in current_evidence}
        removed: set[str] = set()
        superseded_log = list(data.get("superseded_evidence") or [])
        for item in replacements:
            old = str(item.get("evidence") or "")
            reason = str(item.get("reason") or "")
            match = normalized.get(_normalize_text(old))
            if not match:
                raise AmendmentRejected(f"找不到要取代的旧证据: {old}")
            removed.add(match)
            superseded_log.append({
                "evidence": match,
                "reason": reason,
                "amended_at": _now_shanghai(),
            })

        additions = list(add_evidence or [])
        if additions:
            collected = _session_text(self._load())
            for item in additions:
                if _normalize_text(item) not in collected:
                    raise AmendmentRejected(
                        f"新增证据不是本次采证原文: {item[:200]}"
                    )
        merged = [item for item in current_evidence if item not in removed]
        for item in additions:
            if item not in merged:
                merged.append(item)
        data["evidence"] = merged
        if verdict is not None:
            data["verdict"] = verdict
        if severity is not None:
            data["severity"] = severity
        if root_cause is not None:
            data["root_cause"] = root_cause
        if add_remediation:
            remediation = list(data.get("remediation") or [])
            for item in add_remediation:
                if item not in remediation:
                    remediation.append(item)
            data["remediation"] = remediation
        data["superseded_evidence"] = superseded_log
        data["amend_count"] = int(data.get("amend_count") or 0) + 1
        data["last_amended_at"] = _now_shanghai()
        data["report_renderer_version"] = REPORT_RENDERER_VERSION
        self._save(data)
        (self.dir / "report.md").write_text(
            _render_report(data), encoding="utf-8",
        )

    def record_kb_promotion(self, *, doc: str, verified_at: str) -> None:
        """记录本次排查触发的知识库晋升;重渲染 report(渲染器条件段,老记录不受影响)。"""
        data = self._load()
        data["kb_promotion"] = {"doc": doc, "verified_at": verified_at}
        self._save(data)
        (self.dir / "report.md").write_text(
            _render_report(data), encoding="utf-8",
        )


def start_session(records_root: Path, *, run_id: str, node: str, trigger: str,
                  platform: str, service: str, session_type: str = "triage",
                  nodes: Optional[List[str]] = None,
                  report_mode: str = "conclusion",
                  author: Optional[str] = None) -> TroubleshootSession:
    """在 records/troubleshoot/<run_id>/ 起一个新 session,写入初始 session.json。

    author 为 None 时不写该键,读时按数据根所有者推断(与旧记录同口径)。
    """
    d = Path(records_root) / run_id
    d.mkdir(parents=True, exist_ok=True)
    data = {
        "run_id": run_id, "node": node, "trigger": trigger,
        "platform": platform, "service": service,
        "playbook_results": [], "steps": [],
        "verdict": None, "severity": None,
        "root_cause": None, "evidence": [], "remediation": [], "oplogs": [],
        "amend_count": 0, "last_amended_at": None, "superseded_evidence": [],
        "report_renderer_version": REPORT_RENDERER_VERSION,
    }
    if report_mode == "execution":
        data["report_mode"] = "execution"
        data["turn_summaries"] = []
        data["operations"] = []
    if session_type != "triage" or nodes is not None:
        data["session_type"] = session_type
        data["nodes"] = list(nodes) if nodes is not None else [node]
    if author:
        data["author"] = author
    (d / "session.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return TroubleshootSession(dir=d, run_id=run_id)


def _repo_root_from_session_dir(session_dir: Path) -> Path:
    path = Path(session_dir).resolve()
    for parent in [path, *path.parents]:
        if parent.name == "records":
            return parent.parent
    return path.parent


def _validate_oplogs(oplogs: List[str], repo_root: Path) -> None:
    """引用写作 knowledge/<文档 ID>,knowledge/ 指知识库根(可在 ops-agent 之外)。

    迁移前的 knowledge/oplog/... 经 _redirects.yaml 映射后校验。
    """
    if not oplogs:
        raise OplogMissing("approved 步骤需要 knowledge/<库>/oplog/**.md oplog")
    knowledge_root = resolve_knowledge_root(repo_root)
    bases = discover_bases(knowledge_root)
    for item in oplogs:
        path = Path(item)
        if path.is_absolute() or path.suffix != ".md" or len(path.parts) < 3 \
                or path.parts[0] != "knowledge":
            raise OplogMissing(f"oplog 路径无效或不存在: {item}")
        doc_id = resolve_doc_id(knowledge_root, Path(*path.parts[1:]).as_posix())
        base, inner = split_doc_path(doc_id, bases)
        if base is None or not inner.parts or inner.parts[0] != "oplog" \
                or not (knowledge_root / doc_id).is_file():
            raise OplogMissing(f"oplog 路径无效或不存在: {item}")


def _render_report(data: Dict[str, Any]) -> str:
    if data.get("report_mode") == "execution":
        return _render_execution_report(data)
    lines: List[str] = []
    lines.append(f"# 排查报告 · {data['run_id']}\n")
    lines.append(f"- 节点: `{data['node']}`  · 平台/服务: `{data['platform']}/{data['service']}`")
    nodes = data.get("nodes")
    if data.get("session_type") == "survey" \
            or isinstance(nodes, list) and len(nodes) > 1:
        node_set = nodes if isinstance(nodes, list) and nodes else [data["node"]]
        rendered_nodes = ", ".join(f"`{node}`" for node in node_set)
        lines.append(
            f"- 类型: `{data.get('session_type', 'triage')}` · 节点集: {rendered_nodes}"
        )
    lines.append(f"- 触发: {data['trigger']}")
    lines.append(f"- Verdict: **{data['verdict'] or 'n/a'}** · Severity: {data['severity'] or 'n/a'}\n")
    amend_count = int(data.get("amend_count") or 0)
    if amend_count and data.get("last_amended_at"):
        timestamp = str(data["last_amended_at"]).replace("T", " ")[:16]
        lines.append(f"> 已更新 · {timestamp} · 第 {amend_count} 次收口\n")

    lines.append("## 根因\n")
    lines.append(str(data.get("root_cause") or "n/a") + "\n")
    lines.extend(_finish_lines(data))

    lines.append("## 证据\n")
    for e in data.get("evidence") or []:
        lines.append(f"- {e}")
    lines.append("")

    if data.get("playbook_results"):
        lines.append("## Playbook 执行\n")
        for r in data["playbook_results"]:
            lines.append(f"### {r.get('playbook','?')} → verdict={r.get('verdict','?')}\n")
            for s in r.get("steps", []):
                mark = "✓" if s.get("pass") else "✗"
                lines.append(f"- {mark} `{s.get('cmd','')}` — expect: {s.get('expect','')} · actual: {s.get('actual','')}")
            m = r.get("metrics") or {}
            if m:
                lines.append(f"- metrics: {m}")
            lines.append("")

    if data.get("steps"):
        lines.append("## 深挖命令\n")
        for s in data["steps"]:
            lines.append(f"- `$ {s.get('cmd','')}` (exit={s.get('exit_code','?')})")
        lines.append("")

    lines.extend(_write_operation_lines(data))

    lines.append("## 处置建议\n")
    for r in data.get("remediation") or []:
        lines.append(f"- {r}")
    lines.append("")

    if data.get("superseded_evidence"):
        lines.append("## 已修正\n")
        for item in data["superseded_evidence"]:
            lines.append(
                f"- ~~{item.get('evidence', '')}~~: {item.get('reason', '')}"
            )
        lines.append("")

    if data.get("kb_promotion"):
        p = data["kb_promotion"]
        lines.append("## 知识库沉淀\n")
        lines.append(f"- 晋升 verified: `{p['doc']}`(依据本次排查,{p['verified_at']})")
        lines.append("")

    return "\n".join(lines)


def _render_execution_report(data: Dict[str, Any]) -> str:
    """Render a live, append-shaped audit report for Agent-Ops tasks."""
    lines: List[str] = [f"# 执行报告 · {data['run_id']}\n"]
    lines.append(
        f"- 平台/服务: `{data['platform']}/{data['service']}` · 入口节点: `{data['node']}`"
    )
    nodes = data.get("nodes")
    if isinstance(nodes, list) and nodes:
        lines.append("- 节点集: " + ", ".join(f"`{item}`" for item in nodes))
    lines.append(f"- 任务: {data['trigger']}")
    lines.append("- 说明: 本报告随服务器查询与命令执行持续更新，输出已脱敏。\n")

    lines.append("## 操作记录\n")
    operation_index = 0
    operations = data.get("operations") or [
        *({"kind": "playbook", "index": index} for index, _ in enumerate(data.get("playbook_results") or [])),
        *({"kind": "step", "index": index} for index, _ in enumerate(data.get("steps") or [])),
    ]
    for operation in operations:
        operation_index += 1
        kind = operation.get("kind")
        item_index = int(operation.get("index") or 0)
        if kind == "playbook":
            results = data.get("playbook_results") or []
            if item_index >= len(results):
                continue
            result = results[item_index]
        else:
            steps = data.get("steps") or []
            if item_index >= len(steps):
                continue
            step = steps[item_index]
            when = str(step.get("recorded_at") or "-").replace("T", " ")[:19]
            lines.append(f"### {operation_index}. `{step.get('cmd', '')}` · {when}\n")
            lines.append(f"- exit: `{step.get('exit_code', '?')}`")
            stdout = str(step.get("stdout") or "").rstrip()
            stderr = str(step.get("stderr") or "").rstrip()
            if stdout:
                lines.extend(["", "**stdout**", "", "```text", stdout, "```"])
            if stderr:
                lines.extend(["", "**stderr**", "", "```text", stderr, "```"])
            lines.append("")
            continue
        when = str(result.get("recorded_at") or "-").replace("T", " ")[:19]
        lines.append(
            f"### {operation_index}. Playbook `{result.get('playbook', '?')}` · {when}\n"
        )
        lines.append(
            f"- verdict: `{result.get('verdict', 'n/a')}` · severity: `{result.get('severity', 'n/a')}`"
        )
        for step in result.get("steps") or []:
            mark = "✓" if step.get("pass") else "✗"
            lines.append(f"- {mark} `$ {step.get('cmd', '')}`")
            actual = str(step.get("actual") or "").strip()
            if actual:
                lines.extend(["", "```text", actual, "```"])
        lines.append("")
    if operation_index == 0:
        lines.append("尚未执行服务器操作。\n")
    lines.extend(_write_operation_lines(data))

    summaries = data.get("turn_summaries") or []
    if summaries:
        lines.append("## Agent 阶段总结\n")
        for index, item in enumerate(summaries, start=1):
            when = str(item.get("recorded_at") or "-").replace("T", " ")[:19]
            lines.append(f"### {index}. {when}\n")
            lines.append(str(item.get("summary") or "-").strip() + "\n")

    if data.get("verdict") is not None:
        lines.append("## 当前结论\n")
        lines.append(
            f"- Verdict: **{data.get('verdict')}** · Severity: **{data.get('severity') or 'n/a'}**"
        )
        lines.append(f"- 原因: {data.get('root_cause') or 'n/a'}")
        for item in data.get("remediation") or []:
            lines.append(f"- 建议: {item}")
        lines.append("")
        if data.get("finish"):
            lines.extend(_finish_lines(data))
            lines.append("### 证据\n")
            lines.extend(f"- {item}" for item in data.get("evidence") or [])
            lines.append("")
    return "\n".join(lines)


def _redact_values(value: Any) -> Any:
    """逐个字符串值打码(不按键名:token_digest 这类摘要不是凭据,不能被整值替换)。"""
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, list):
        return [_redact_values(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_values(item) for key, item in value.items()}
    return value


_OUTCOME_LABELS = {
    "succeeded": "成功", "partial": "部分成功", "failed": "失败", "unverified": "已完成未复核",
    "unknown": "结果未知", "noop": "无需执行", "not_executed": "未执行", "executed": "已执行",
}


def _write_operation_lines(data: Dict[str, Any]) -> List[str]:
    """人工批准的写操作一节;没有 write_operations 键的旧记录输出为空(报告逐字节不变)。"""
    operations = data.get("write_operations") or []
    if not operations:
        return []
    lines = ["## 写操作\n"]
    for index, op in enumerate(operations, start=1):
        outcome = str(op.get("outcome") or "")
        lines.append(f"### {index}. {op.get('title') or op.get('id')} · {_OUTCOME_LABELS.get(outcome, outcome)}\n")
        if op.get("kind") == "plugin":
            lines.append(f"- 插件: `{op.get('plugin')}` · 计划: `{op.get('plan_id')}`"
                         + (f" · 请求: `{op['request_id']}`" if op.get("request_id") else ""))
        else:
            lines.append("- 节点: " + ", ".join(f"`{node}`" for node in op.get("nodes") or []))
        when = str(op.get("decided_at") or "-").replace("T", " ")[:19]
        lines.append(f"- 审批人: {op.get('approver') or 'n/a'} · 批准时间: {when}"
                     + (f" · 令牌摘要: `{op['token_digest']}`" if op.get("token_digest") else ""))
        if op.get("summary"):
            lines.append(f"- 结论: {op['summary']}")
        if op.get("steps"):
            lines.append("- 步骤: " + ", ".join(str(ref) for ref in op["steps"]))
        if op.get("oplog"):
            lines.append(f"- oplog: `{op['oplog']}`")
        elif op.get("oplog_error"):
            lines.append(f"- oplog 未生成: {op['oplog_error']}")
        lines.append("")
    return lines


def _finish_lines(data: Dict[str, Any]) -> List[str]:
    """新运行时 finish 的回答、置信度与不确定项;老记录没有 finish 键,输出为空。"""
    finish = data.get("finish")
    if not isinstance(finish, dict):
        return []
    lines = ["### 回答\n", str(finish.get("answer") or "-").strip() + "\n"]
    lines.append(f"- 置信度: {finish.get('confidence') or 'n/a'}")
    window = finish.get("window")
    if isinstance(window, dict):
        lines.append(f"- 故障窗口: {window.get('start')} ~ {window.get('end')}")
    uncertain = [str(item) for item in finish.get("uncertain") or []]
    if uncertain:
        lines.append("- 不确定项:")
        lines.extend(f"  - {item}" for item in uncertain)
    lines.append("")
    return lines


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


_EVIDENCE_VALUE_KEYS = frozenset({"stdout", "stderr", "actual"})


def collect_evidence_fragments(data: Any) -> List[str]:
    """Return only raw diagnostic/playbook output eligible for citations.

    Session metadata (node, platform, command names, and JSON field names) is
    deliberately excluded.  Evidence must come from command stdout/stderr or
    a playbook step's actual value, otherwise a conclusion could cite a host
    label as if it were an observed fact.
    """
    fragments: List[str] = []

    def collect_value(value: Any) -> None:
        if isinstance(value, str):
            fragments.append(_normalize_text(value))
        elif isinstance(value, bool):
            fragments.append(str(value).lower())
        elif isinstance(value, (int, float)):
            fragments.append(str(value))
        elif isinstance(value, dict):
            for child in value.values():
                collect_value(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                collect_value(child)

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in _EVIDENCE_VALUE_KEYS:
                    collect_value(child)
                else:
                    visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)

    visit(data)
    return fragments


def _session_text(data: Dict[str, Any]) -> str:
    """Backward-compatible joined form used by amendment validation."""
    return "\n".join(collect_evidence_fragments(data))


def _now_shanghai() -> str:
    return datetime.now(env_timezone()).isoformat()


def migrate_legacy_records(records_root: Path) -> List[str]:
    """Upgrade legacy sessions to the current record schema without changing findings."""
    root = Path(records_root)
    migrated: List[str] = []
    for run_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        session_path = run_dir / "session.json"
        if not session_path.is_file():
            continue
        data = json.loads(session_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("run_id") != run_dir.name:
            # Malformed or non-session JSON is left for record lint to report;
            # migration must never invent identity or overwrite its meaning.
            continue
        changed = False
        for key, default in (
            ("amend_count", 0),
            ("last_amended_at", None),
            ("superseded_evidence", []),
        ):
            if key not in data:
                data[key] = default
                changed = True
        if data.get("report_renderer_version") != REPORT_RENDERER_VERSION:
            data["report_renderer_version"] = REPORT_RENDERER_VERSION
            changed = True
        if not changed:
            continue
        session = TroubleshootSession(dir=run_dir, run_id=run_dir.name)
        session._save(data)
        report_path = run_dir / "report.md"
        if report_path.is_file():
            report_path.write_text(_render_report(data), encoding="utf-8")
        migrated.append(run_dir.name)
    return migrated


def lint_records(records_root: Path) -> List[tuple[str, str]]:
    records_root = Path(records_root)
    errors: List[tuple[str, str]] = []
    for run_dir in sorted(p for p in records_root.iterdir() if p.is_dir()):
        run_id = run_dir.name
        session_path = run_dir / "session.json"
        if not session_path.exists():
            # Survey scripts may leave a staging directory containing only
            # generated artifacts before a real session is created.
            children = [item for item in run_dir.iterdir() if item.name != ".DS_Store"]
            if children and all(item.name == "artifacts" for item in children):
                continue
            errors.append((run_id, "缺少 session.json"))
            continue
        data = None
        try:
            data = json.loads(session_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append((run_id, f"session.json 非法 JSON: {exc}"))
        if data is not None:
            missing = sorted(SESSION_SCHEMA_KEYS - set(data))
            if missing:
                errors.append((run_id, f"session.json 缺少键: {', '.join(missing)}"))
            if data.get("run_id") != run_id:
                errors.append((run_id, "session.json run_id 与目录名不一致"))
            if "author" in data and not (
                isinstance(data["author"], str) and _AUTHOR_ID_RE.fullmatch(data["author"])
            ):
                errors.append((run_id, "session.json author 必须是合法用户 ID"))
            for key in ("steps", "playbook_results", "evidence", "remediation", "oplogs", "write_operations"):
                if key in data and not isinstance(data[key], list):
                    errors.append((run_id, f"session.json {key} 必须是 list"))
            if "session_type" in data:
                session_type = data["session_type"]
                if not isinstance(session_type, str) \
                        or session_type not in {"triage", "survey"}:
                    errors.append((run_id, "session.json session_type 必须是 triage 或 survey"))
            if "nodes" in data:
                nodes = data["nodes"]
                if not isinstance(nodes, list):
                    errors.append((run_id, "session.json nodes 必须是 list"))
                elif not nodes:
                    errors.append((run_id, "session.json nodes 必须至少包含一个节点"))
                elif not all(isinstance(node, str) for node in nodes):
                    errors.append((run_id, "session.json nodes 元素必须全部是 str"))
                elif data.get("node") != nodes[0]:
                    errors.append((run_id, "session.json node 必须等于 nodes[0]"))
            if data.get("verdict") is not None \
                    and any(step.get("approved") is True for step in data.get("steps") or []) \
                    and not data.get("oplogs"):
                errors.append((run_id, "approved 步骤已 finalize 时必须记录 oplogs"))
            report_path = run_dir / "report.md"
            if report_path.exists():
                actual = report_path.read_text(encoding="utf-8").rstrip()
                if data.get("report_renderer_version") is not None \
                        and data.get("report_renderer_version") != REPORT_RENDERER_VERSION:
                    errors.append((run_id, "report 渲染器版本不符,需重渲染迁移"))
                else:
                    try:
                        rendered = _render_report(data).rstrip()
                    except KeyError:
                        rendered = None
                    if rendered != actual:
                        errors.append((run_id, "report.md 与 session.json 重渲染不一致"))
        for json_path in sorted(run_dir.glob("*.json")):
            if json_path.name != "session.json":
                errors.append((run_id, f"run 目录根部禁止 session.json 之外的 JSON 文件: {json_path.name}"))
    return errors


def _main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] != "lint":
        print("usage: python -m lib.record lint <records_root>", file=sys.stderr)
        return 2
    errors = lint_records(Path(args[1]))
    for run_id, reason in errors:
        print(f"{run_id}: {reason}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(_main())
