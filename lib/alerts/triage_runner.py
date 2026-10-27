"""Automatic read-only evidence collection for a single alert event."""
from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Any, Dict

from lib.datastore import open_datastore
from lib.envpack import active_env_pack
from lib.runner import PlaybookTimeout, run_playbook
from lib.triage import iter_alert_entries, match_alert

from .model import AlertEvent


@dataclass
class TriageOutcome:
    status: str
    run_id: Optional[str] = None
    matched_doc: Optional[str] = None
    playbook: Optional[str] = None
    playbooks: List[str] = field(default_factory=list)
    verdict: Optional[str] = None
    severity: Optional[str] = None
    conclusion_hint: Optional[str] = None
    failed_steps: List[dict] = field(default_factory=list)
    error: Optional[str] = None


def run_auto_triage(event: AlertEvent, routing: dict, pool,
                    records_root=None) -> TriageOutcome:
    # 显式 records_root(测试注入)时 worklog 落其同级目录,与数据根默认布局一致
    datastore = open_datastore(
        troubleshoot_root=records_root,
        worklog_root=Path(records_root).parent / "worklog" if records_root is not None else None,
    )
    if event.platform not in active_env_pack().cluster_keys("managed"):
        return TriageOutcome(status="not_applicable", error=f"unsupported platform: {event.platform}")
    if event.host is None:
        return TriageOutcome(status="not_applicable", error="missing normalized host")

    hit = match_alert(routing, event.alertname, event.platform)
    if hit is None:
        return TriageOutcome(status="not_applicable", error="no routing match")
    if hit.platform not in (event.platform, "shared"):
        # 双保险:拦截手工编辑或损坏索引造成的平台错配;shared 文档跨平台放行
        return TriageOutcome(status="not_applicable", matched_doc=hit.doc,
                             error=f"platform mismatch: doc={hit.platform} event={event.platform}")
    playbooks = _ordered_playbooks(hit)
    if not playbooks:
        return TriageOutcome(status="not_applicable", matched_doc=hit.doc, error="matched doc has no playbook")
    registered = _registered_playbooks(routing)
    unregistered = next((path for path in playbooks if path not in registered), None)
    if unregistered:
        return TriageOutcome(
            status="not_applicable", matched_doc=hit.doc,
            error=f"unregistered playbook: {unregistered}",
        )

    run_id = _new_run_id()
    session = datastore.troubleshoot.start(
        run_id,
        node=event.host,
        trigger=event.alertname,
        platform=event.platform,
        service=hit.service,
    )
    try:
        executed: list[str] = []
        result_dict: dict = {}
        for playbook in playbooks:
            result = run_playbook(
                pool, event.host, active_env_pack().playbook_path(playbook), run_id=run_id, timeout=180,
            )
            result_dict = result.to_session_dict()
            session.record_playbook_result(result_dict)
            executed.append(playbook)
            if result_dict.get("verdict") in {"conclusive", "needs_llm"}:
                break
        _write_context(session.dir, event, hit.doc)
        outcome = TriageOutcome(
            status="done",
            run_id=run_id,
            matched_doc=hit.doc,
            playbook=executed[-1] if executed else None,
            playbooks=executed,
            verdict=result_dict.get("verdict"),
            severity=result_dict.get("severity"),
            conclusion_hint=result_dict.get("conclusion_hint"),
            failed_steps=[step for step in result_dict.get("steps", []) if step.get("pass") is False],
        )
        _emit_worklog(outcome, event, datastore)
        return outcome
    except PlaybookTimeout as exc:
        outcome = TriageOutcome(
            status="failed", run_id=run_id, matched_doc=hit.doc,
            playbook=executed[-1] if "executed" in locals() and executed else None,
            playbooks=executed if "executed" in locals() else [],
            error=str(exc),
        )
        _emit_worklog(outcome, event, datastore)
        return outcome
    except Exception as exc:
        outcome = TriageOutcome(
            status="failed", run_id=run_id, matched_doc=hit.doc,
            playbook=executed[-1] if "executed" in locals() and executed else None,
            playbooks=executed if "executed" in locals() else [],
            error=f"{type(exc).__name__}: {exc}",
        )
        _emit_worklog(outcome, event, datastore)
        return outcome


def _emit_worklog(outcome: TriageOutcome, event: AlertEvent, datastore) -> None:
    try:
        from lib.worklog import append_from_alert
        append_from_alert(outcome, event, datastore=datastore)
    except Exception as exc:
        import sys
        print(f"WARN worklog auto-emit 失败: {exc}", file=sys.stderr)


def _registered_playbooks(routing: Dict[str, Any]) -> set:
    out = set()
    for entry in iter_alert_entries(routing):
        if entry.get("playbook"):
            out.add(entry["playbook"])
        for ref in entry.get("playbooks") or []:
            path = ref.get("path") if isinstance(ref, dict) else ref
            if path:
                out.add(str(path))
        for path in _diagnostic_playbooks(entry.get("diagnostic_paths")):
            out.add(path)
    for doc in routing.get("docs") or []:
        if doc.get("playbook"):
            out.add(doc["playbook"])
        for ref in doc.get("playbooks") or []:
            path = ref.get("path") if isinstance(ref, dict) else ref
            if path:
                out.add(str(path))
        for path in _diagnostic_playbooks(doc.get("diagnostic_paths")):
            out.add(path)
    return out


def _ordered_playbooks(hit) -> list[str]:
    refs = list(hit.playbooks or [])
    values = []
    if refs:
        for index, ref in enumerate(refs, start=1):
            path = ref.get("path") if isinstance(ref, dict) else ref
            if path:
                order = ref.get("order", index) if isinstance(ref, dict) else index
                values.append((int(order or index), str(path)))
    else:
        for path_index, path_item in enumerate(hit.diagnostic_paths or [], start=1):
            if not isinstance(path_item, dict):
                continue
            path_order = int(path_item.get("order") or path_index)
            for ref_index, ref in enumerate(path_item.get("playbooks") or [], start=1):
                path = ref.get("path") if isinstance(ref, dict) else ref
                if path:
                    ref_order = ref.get("order", ref_index) if isinstance(ref, dict) else ref_index
                    values.append((path_order * 10000 + int(ref_order or ref_index), str(path)))
    if values:
        return [path for _, path in sorted(values, key=lambda item: (item[0], item[1]))]
    return [str(hit.playbook)] if hit.playbook else []


def _diagnostic_playbooks(paths) -> set[str]:
    out = set()
    for item in paths or []:
        if not isinstance(item, dict):
            continue
        for ref in item.get("playbooks") or []:
            path = ref.get("path") if isinstance(ref, dict) else ref
            if path:
                out.add(str(path))
    return out


def _new_run_id() -> str:
    return f"al-{datetime.now().strftime('%Y%m%d-%H%M')}-{secrets.token_hex(2)}"


def _write_context(run_dir: Path, event: AlertEvent, matched_doc: str) -> None:
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "alert-context.json").write_text(
        json.dumps({
            "key": event.key,
            "source": event.source,
            "platform": event.platform,
            "alertname": event.alertname,
            "severity": event.severity,
            "host": event.host,
            "host_raw": event.host_raw,
            "service": event.service,
            "detail": event.detail,
            "matched_doc": matched_doc,
            "event": asdict(event),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
