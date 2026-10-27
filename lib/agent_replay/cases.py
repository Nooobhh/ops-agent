"""回放用例:按人工清单从历史任务冻结输入、录制池、交互脚本与标注。"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import yaml

from lib.data_root import resolve_data_root
from lib.datastore import open_datastore

from .recordings import Recording, extract_recordings

CASE_SCHEMA_VERSION = 1
CREATE_KINDS = {"manual", "risk", "auto"}
# 回放所需的冻结 state:运行时只读,与主仓 state 演进解耦
STATE_FIXTURE_FILES = (
    "acquisition-settings.json",
    "resource-catalog.json",
    "resource-alert-settings.json",
    "command-policy.json",
)
STATE_FIXTURE_GLOBS = ("graphs/*/graph-latest.json",)
STATE_FIXTURE_DIRS = ("topology",)
_WS = re.compile(r"\s+")


def load_manifest(path: Path) -> Dict[str, Any]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    cases = raw.get("cases") or []
    seen = set()
    for entry in cases:
        case_id = str(entry.get("id") or "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,63}", case_id):
            raise ValueError(f"用例 id 不合法: {case_id!r}")
        if case_id in seen:
            raise ValueError(f"用例 id 重复: {case_id}")
        seen.add(case_id)
        if str(entry.get("create") or "manual") not in CREATE_KINDS:
            raise ValueError(f"{case_id}: create 只能取 {sorted(CREATE_KINDS)}")
        if entry.get("create") == "auto" and not isinstance(entry.get("alert_event"), dict):
            raise ValueError(f"{case_id}: auto 用例必须给出 alert_event")
    return raw


def _read_events(task_dir: Path) -> List[Dict[str, Any]]:
    path = task_dir / "messages.jsonl"
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _norm(value: str) -> str:
    return _WS.sub(" ", value).strip()


def _evidence_texts(recording: Recording) -> Iterable[str]:
    result = recording.result
    for key in ("stdout", "stderr"):
        if isinstance(result.get(key), str):
            yield result[key]
    for step in result.get("steps") or []:
        if isinstance(step, dict):
            for key in ("actual", "raw"):
                if isinstance(step.get(key), str):
                    yield step[key]


def key_evidence(
    recordings: List[Recording],
    events: List[Dict[str, Any]],
    session: Optional[Dict[str, Any]],
) -> List[str]:
    """原任务结论实际引用的录制调用 ID:finalize 证据原文反查 + 决策/审核的 evidence_refs。"""
    by_id = {recording.call_id: recording for recording in recordings}
    keys: List[str] = []

    def add(call_id: str) -> None:
        if call_id in by_id and call_id not in keys:
            keys.append(call_id)

    def collect_refs(value: Any) -> None:
        # 决策在顶层带 evidence_refs,假设审核挂在 prediction_results[] 里
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "evidence_refs" and isinstance(child, list):
                    for ref in child:
                        add(str(ref))
                else:
                    collect_refs(child)
        elif isinstance(value, list):
            for child in value:
                collect_refs(child)

    for event in events:
        if event.get("type") in {"agent_decision", "hypothesis_reviewed"}:
            collect_refs(event.get("payload") or {})
    for item in (session or {}).get("evidence") or []:
        fragment = _norm(str(item))
        if not fragment:
            continue
        for recording in recordings:
            if any(fragment in _norm(text) for text in _evidence_texts(recording)):
                add(recording.call_id)
                break
    return keys


def _approval_policy(events: List[Dict[str, Any]], recordings: List[Recording]) -> Dict[str, Any]:
    requested = {
        str((event.get("payload") or {}).get("approval_id")): event.get("payload") or {}
        for event in events if event.get("type") == "approval_requested"
    }
    budget_grants = 0
    command_rejects: List[str] = []
    for event in events:
        if event.get("type") != "approval_decided":
            continue
        payload = event.get("payload") or {}
        request = requested.get(str(payload.get("approval_id"))) or {}
        if request.get("kind") == "budget" and payload.get("approved"):
            budget_grants += 1
        elif request.get("kind") == "command" and payload.get("decision") == "reject":
            command = _norm(str(request.get("command") or ""))
            if command and command not in command_rejects:
                command_rejects.append(command)
    return {"budget_grants": budget_grants, "command_rejects": command_rejects}


def _source_outcome(task: Dict[str, Any], events: List[Dict[str, Any]], session: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    decisions = [
        (event.get("payload") or {}).get("action")
        for event in events if event.get("type") == "agent_decision"
    ]
    return {
        "status": task.get("status"),
        "failure_kind": task.get("failure_kind"),
        "task_type": task.get("task_type"),
        "platform": task.get("platform"),
        "node": task.get("node"),
        "scope": task.get("scope"),
        "verdict": (session or {}).get("verdict"),
        "last_decision": decisions[-1] if decisions else None,
        "hypothesis_status": (task.get("current_hypothesis") or {}).get("status"),
    }


def build_case(entry: Dict[str, Any], source_repo: Path) -> Dict[str, Any]:
    # 源仓的 state/records 按其数据根定位,只读
    source_repo = Path(source_repo)
    tasks_root = resolve_data_root(source_repo) / "state" / "triage-tasks"
    primary_id = str(entry["source"])
    primary_dir = tasks_root / primary_id
    task = json.loads((primary_dir / "task.json").read_text(encoding="utf-8"))
    events = _read_events(primary_dir)
    session = (
        open_datastore(source_repo).troubleshoot.session(task["run_id"])
        if task.get("run_id") else None
    )
    recordings = extract_recordings(primary_dir)
    for extra in entry.get("extra_sources") or []:
        recordings.extend(extract_recordings(tasks_root / str(extra)))
    kind = str(entry.get("create") or "manual")
    request = dict(entry.get("request") or {})
    if kind == "risk":
        seed = task.get("risk_seed") or {}
        request.setdefault("platform", seed.get("platform"))
        request.setdefault("node", seed.get("node"))
    approvals = _approval_policy(events, recordings)
    if entry.get("budget_grants") is not None:
        approvals["budget_grants"] = int(entry["budget_grants"])
    return {
        "schema_version": CASE_SCHEMA_VERSION,
        "case_id": entry["id"],
        "smoke": bool(entry.get("smoke")),
        "note": entry.get("note") or "",
        "expect": dict(entry.get("expect") or {}),
        "create": {
            "kind": kind,
            "message": str(entry.get("message") or task.get("initial_message") or ""),
            "platform": request.get("platform"),
            "node": request.get("node"),
            # 现行 Web 入口(Composer/风险卡)一律 manual_artifacts=True;历史任务的取值
            # 反映的是当时的入口,回放要测的是现在的行为
            "manual_artifacts": bool(entry.get("manual_artifacts", True)),
            "risk_seed": task.get("risk_seed") if kind == "risk" else None,
            "alert_event": entry.get("alert_event") if kind == "auto" else None,
            # 告警页入口带结构化告警(见前端 alertTaskDraft);旧任务没存这个字段时由清单补
            "alert_context": (
                entry.get("alert_context") or task.get("alert_context")
                if kind == "manual" else None
            ),
            "polling_snapshot": task.get("polling_snapshot"),
        },
        "interaction": {
            "context_reply": entry.get("context_reply"),
            "followups": [str(item) for item in entry.get("followups") or []],
            "control_replies": [str(item) for item in entry.get("control_replies") or []],
            **approvals,
        },
        "recordings": [recording.to_dict() for recording in recordings],
        "labels": {
            "source_tasks": [primary_id, *[str(item) for item in entry.get("extra_sources") or []]],
            "source": _source_outcome(task, events, session),
            "key_evidence": key_evidence(recordings, events, session),
        },
    }


def freeze_state_fixtures(source_state: Path, fixtures_dir: Path) -> List[str]:
    """把回放要读的 state 文件拷进 fixtures;只读源,缺失的跳过。"""
    target_state = Path(fixtures_dir) / "state"
    copied: List[str] = []
    globbed = [
        path.relative_to(source_state).as_posix()
        for pattern in STATE_FIXTURE_GLOBS for path in sorted(Path(source_state).glob(pattern))
    ]
    for relative in (*STATE_FIXTURE_FILES, *globbed):
        source = Path(source_state) / relative
        if source.is_file():
            target = target_state / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(relative)
    for relative in STATE_FIXTURE_DIRS:
        source = Path(source_state) / relative
        if source.is_dir():
            shutil.copytree(source, target_state / relative, dirs_exist_ok=True)
            copied.append(relative + "/")
    return copied


def load_case(path: Path) -> Dict[str, Any]:
    case = json.loads(Path(path).read_text(encoding="utf-8"))
    if case.get("schema_version") != CASE_SCHEMA_VERSION:
        raise ValueError(f"用例 schema 不匹配: {path}")
    return case


def select_cases(cases_dir: Path, selector: str) -> List[Dict[str, Any]]:
    cases = [load_case(path) for path in sorted(Path(cases_dir).glob("*.json"))]
    if selector == "all":
        return cases
    if selector == "smoke":
        return [case for case in cases if case.get("smoke")]
    wanted = [item.strip() for item in selector.split(",") if item.strip()]
    by_id = {case["case_id"]: case for case in cases}
    missing = [item for item in wanted if item not in by_id]
    if missing:
        raise ValueError(f"用例不存在: {', '.join(missing)}")
    return [by_id[item] for item in wanted]
