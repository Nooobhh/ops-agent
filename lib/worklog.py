"""Atomic worklog rendering and auto-emit adapters."""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

from lib.atomic_file import atomic_write_text
from lib.envpack import env_timezone
from lib.file_lock import FileLock
from lib.playbook import patrol_node_healthy

_ENTRY_HEADING_RE = re.compile(r"^## \[[^]]+\] \[[^]]+\] .+$")
_ENTRY_RE = re.compile(r"^## \[(?P<status>[^]]+)\] \[(?P<category>[^]]+)\] (?P<title>.*)$")
_WEEKLY_REPORT_EXCLUSION_LINE = "- **周报**: 忽略"
_AUTHOR_PREFIX = "- **作者**:"


def _now() -> datetime:
    """时间一律取环境时区(env.yaml timezone),不读宿主时区(错时区曾致 worklog 落错天)。"""
    return datetime.now(env_timezone())


@dataclass
class WorklogEntry:
    status: str
    category: str
    title: str
    source: str
    output: str
    related: str
    process: List[str]
    ts: datetime
    # 作者 ID;None = 不写作者行,读时推断为数据根所有者(旧条目同口径)
    author: Optional[str] = None


def render_entry(entry: WorklogEntry) -> str:
    """渲染 worklog 条目文本;预览与落盘同源,所见即所写。"""
    return _render_entry(entry)


def _render_entry(entry: WorklogEntry) -> str:
    process = "  ".join(
        f"{index}. {item}" for index, item in enumerate(entry.process, start=1)
    ) or "-"
    author = f"{_AUTHOR_PREFIX} {entry.author}\n" if entry.author else ""
    return (
        f"## [{entry.status}] [{entry.category}] {entry.title}\n"
        f"- **来源**: {entry.source}\n"
        f"{author}"
        f"- **产出**: {entry.output}\n"
        f"- **关联**: {entry.related}\n"
        f"- **过程**: {process}\n"
    )


def _empty_entry(status: str, category: str, title: str) -> dict[str, Any]:
    return {
        "status": status,
        "category": category,
        "title": title,
        "source": "-",
        "author": None,
        "output": "-",
        "related": "-",
        "process": [],
        "weekly_report_excluded": False,
        "raw": "",
    }


def parse_worklog(text: str) -> list[dict[str, Any]]:
    """解析一天的 worklog;作者行只进 author 字段,不进 raw(周报模型输入与旧格式一致)。"""
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        match = _ENTRY_RE.match(line.strip())
        if match:
            current = _empty_entry(
                match.group("status"), match.group("category"), match.group("title")
            )
            entries.append(current)
            continue
        if current is None:
            continue
        if line.startswith(_AUTHOR_PREFIX):
            current["author"] = line.split(":", 1)[1].strip() or None
            continue
        if line.startswith("- **来源**:"):
            current["source"] = line.split(":", 1)[1].strip()
        elif line.startswith("- **产出**:"):
            current["output"] = line.split(":", 1)[1].strip()
        elif line.startswith("- **关联**:"):
            current["related"] = line.split(":", 1)[1].strip()
        elif line.startswith("- **过程**:"):
            value = line.split(":", 1)[1].strip()
            current["process"] = [] if value == "-" else [
                item.strip() for item in re.split(r"\s{2,}", value) if item.strip()
            ]
        elif line.startswith("- **周报**:"):
            current["weekly_report_excluded"] = (
                line.split(":", 1)[1].strip() == "忽略"
            )
        current["raw"] += line + "\n"
    for index, entry in enumerate(entries):
        entry["entry_index"] = index
    return entries


def append_entry(entry: WorklogEntry, records_root: Path,
                 event_key=None) -> Optional[Path]:
    """文件层原语:业务代码经 lib.datastore 的 WorklogStore.append 调用,不直接拼 worklog 目录。"""
    path = Path(records_root) / f"{entry.ts.strftime('%Y-%m-%d')}.md"
    written = _atomic_append(
        path,
        _render_entry(entry),
        f"# {entry.ts.strftime('%Y-%m-%d')}\n\n",
        event_key=event_key,
    )
    return path if written else None


def _atomic_append(path: Path, chunk: str, header: str,
                   event_key=None) -> bool:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(f"{path}.lock"):
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        source_line = f"- **来源**: {event_key}"
        if event_key is not None and source_line in current.splitlines():
            return False
        content = current if current else header
        if not content.endswith("\n\n"):
            content = content.rstrip("\n") + "\n\n"
        content += chunk.rstrip("\n") + "\n\n"

        atomic_write_text(path, content)
    return True


def set_weekly_report_excluded(
    path: Path,
    entry_index: int,
    excluded: bool,
) -> bool:
    """Toggle one entry's weekly-report exclusion marker under the worklog lock."""
    path = Path(path)
    if entry_index < 0:
        raise IndexError("工作日志条目不存在")
    with FileLock(f"{path}.lock"):
        if not path.is_file():
            raise FileNotFoundError(path)
        content = path.read_text(encoding="utf-8")
        lines = content.splitlines(keepends=True)
        starts = [
            index for index, line in enumerate(lines)
            if _ENTRY_HEADING_RE.match(line.rstrip("\r\n"))
        ]
        if entry_index >= len(starts):
            raise IndexError("工作日志条目不存在")

        start = starts[entry_index]
        end = starts[entry_index + 1] if entry_index + 1 < len(starts) else len(lines)
        marker_indexes = [
            index for index in range(start + 1, end)
            if lines[index].rstrip("\r\n") == _WEEKLY_REPORT_EXCLUSION_LINE
        ]
        currently_excluded = bool(marker_indexes)
        if currently_excluded == excluded:
            return currently_excluded

        if excluded:
            insert_at = next(
                (
                    index + 1 for index in range(start + 1, end)
                    if lines[index].startswith("- **来源**:")
                ),
                start + 1,
            )
            newline = "\r\n" if lines[start].endswith("\r\n") else "\n"
            lines.insert(insert_at, _WEEKLY_REPORT_EXCLUSION_LINE + newline)
        else:
            for index in reversed(marker_indexes):
                del lines[index]
        atomic_write_text(path, "".join(lines))
        return excluded


def entry_from_troubleshoot(
    session_dir: Path,
    ts: datetime | None = None,
    kb_doc: str | None = None,
) -> Optional[WorklogEntry]:
    """从排查 session 构建 worklog 条目(不落盘);session 未收口返回 None。

    kb_doc:agent 落档路径的知识文档不写 session.json 的 kb_promotion,
    由调用方从任务态补传。
    """
    session_dir = Path(session_dir)
    data = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
    verdict = data.get("verdict")
    if verdict is None:
        return None
    run_id = data["run_id"]
    approved = any(step.get("approved") is True for step in data.get("steps") or [])
    if verdict == "conclusive":
        status, category = "DONE", "BUGFIX" if approved else "OPS"
    elif verdict in {"needs_llm", "insufficient"}:
        status, category = "WIP", "RESEARCH"
    else:
        status, category = "WIP", "RESEARCH"

    output_parts = ["report.md"]
    promotion = data.get("kb_promotion") or {}
    if promotion.get("doc"):
        output_parts.append(str(promotion["doc"]))
    if kb_doc and kb_doc not in output_parts:
        output_parts.append(str(kb_doc))
    playbooks = [
        str(item.get("playbook"))
        for item in data.get("playbook_results") or []
        if item.get("playbook")
    ]
    process = []
    if data.get("root_cause"):
        process.append(_short(f"根因:{data['root_cause']}"))
    process.extend(_short(f"证据:{item}") for item in (data.get("evidence") or [])[:3])
    process.append(_short(f"结论:{verdict}"))
    process = _ensure_process(process, "生成排查报告")[:5]
    return WorklogEntry(
        status=status,
        category=category,
        title=str(data["trigger"]),
        source=f"auto:troubleshoot:{run_id}",
        output=" + ".join(output_parts),
        related=", ".join(playbooks) if playbooks else "-",
        process=process,
        ts=ts or _now(),
    )


def _datastore(worklog_root: Optional[Path], datastore):
    """自动 emit 一律经数据层落盘;未注入时按 ambient 数据根打开(CLI 约定从仓库根运行)。"""
    if datastore is not None:
        return datastore
    from lib.datastore import open_datastore

    return open_datastore(worklog_root=worklog_root)


def append_from_troubleshoot(
    session_dir: Path,
    ts: datetime | None = None,
    worklog_root: Path | None = None,
    kb_doc: str | None = None,
    *,
    datastore=None,
    author: Optional[str] = None,
) -> Optional[Path]:
    try:
        entry = entry_from_troubleshoot(session_dir, ts=ts, kb_doc=kb_doc)
        if entry is None:
            return None
    except Exception as exc:
        _warn_auto_emit(exc)
        return None
    return _datastore(worklog_root, datastore).worklogs.append(
        entry, author=author, event_key=entry.source,
    )


def append_from_patrol(snapshot_path: Path,
                       ts: datetime | None = None, *, datastore=None) -> Optional[Path]:
    try:
        snapshot_path = Path(snapshot_path)
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        run_id = snapshot["run_id"]
        platform = snapshot["platform"]
        playbook = Path(snapshot["playbook"]).stem
        nodes = snapshot["nodes"]
        total = len(nodes)
        passed = sum(1 for result in nodes.values() if patrol_node_healthy(result))
        failed = total - passed
        event_key = f"auto:patrol:{run_id}"
        entry = WorklogEntry(
            status="DONE",
            category="OPS",
            title=f"巡检 {platform} {playbook}",
            source=event_key,
            output="snapshot.json + summary.md",
            related=f"{total} 台节点",
            process=[f"扫 {total} 台节点", f"{passed} 台通过 / {failed} 台异常", "生成汇总报告"],
            ts=ts or _now(),
        )
    except Exception as exc:
        _warn_auto_emit(exc)
        return None
    return _datastore(None, datastore).worklogs.append(entry, event_key=event_key)


def append_from_alert(triage_outcome, event, *,
                      worklog_root: Optional[Path] = None,
                      ts: datetime | None = None, datastore=None) -> Optional[Path]:
    try:
        store = _datastore(worklog_root, datastore)
        run_id = (
            _value(triage_outcome, "run_id", None)
            or _value(triage_outcome, "triage_run_id", None)
        )
        alertname = _value(event, "alertname")
        host = _value(event, "host")
        source = _value(event, "source")
        severity = _value(event, "severity")
        identity = run_id or f"{alertname}:{host}"
        event_key = f"auto:alert:{identity}"
        # 产出写相对数据根的展示路径,存在性按数据层判断
        report = None
        if run_id and (store.troubleshoot.run_dir(run_id) / "report.md").exists():
            report = store.troubleshoot.report_ref(run_id)
        process = []
        matched_doc = _value(triage_outcome, "matched_doc", None)
        playbook = _value(triage_outcome, "playbook", None)
        hint = _value(triage_outcome, "conclusion_hint", None)
        if matched_doc:
            process.append(_short(f"匹配:{matched_doc}"))
        if playbook:
            process.append(_short(f"采证:{playbook}"))
        if hint:
            process.append(_short(f"结论:{hint}"))
        process = _ensure_process(process, "完成告警采证")
        entry = WorklogEntry(
            status="DONE",
            category="OPS",
            title=f"告警采证 {alertname} @ {host}",
            source=event_key,
            output=report or "-",
            related=f"{source}, {severity}",
            process=process[:8],
            ts=ts or _now(),
        )
    except Exception as exc:
        _warn_auto_emit(exc)
        return None
    return store.worklogs.append(entry, event_key=event_key)


def _value(value, name: str, default=...):
    if isinstance(value, dict):
        result = value.get(name, default)
    else:
        result = getattr(value, name, default)
    if result is ...:
        raise KeyError(name)
    return result


def _short(value: str) -> str:
    value = str(value).replace("\n", " ").strip()
    return value if len(value) <= 20 else value[:19] + "…"


def _ensure_process(process: List[str], fallback: str) -> List[str]:
    if not process:
        process.append(fallback)
    while len(process) < 3:
        process.append(fallback)
    return process


def _warn_auto_emit(exc: Exception) -> None:
    print(f"WARN worklog auto-emit 失败: {exc}", file=sys.stderr)
