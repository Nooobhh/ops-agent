"""任务类型材料(A2 §5.5):一个运行时 + 类型材料,类型不决定工具、不阻塞任务。

开场材料由 Harness 预先准备、零 LLM,只在任务首次执行时生成一次(之后只追加,保证前缀稳定):
- qa:建任务时固化的本地资源轮询快照(带采样时间,参考非实时证据)
- investigation / artifact:ContextPack 引用清单(正文由模型按需 kb_read);风险卡片任务另附 RiskSeed
- alert:精确路由命中的登记 playbook 由 Harness 先跑(每个最多一次,遇到成立即停),结果进开场
- artifact:另附本任务已有的执行报告
材料准备失败不让任务失败,在开场里注明,模型照常自主排查。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from lib.envpack import active_env_pack
from lib.triage_agent.alert_playbooks import execute_playbook, select_playbooks
from lib.triage_agent.models import TaskStatus, TriageTask
from lib.triage_agent.sessions import ensure_session, sync_live_report

MAX_REFS = 12
SNAPSHOT_CHARS = 6000
REF_BUCKETS = ("diagnosis", "procedures", "knowledge", "basic_info", "alert_route", "task_route")


def _knowledge_platform(platform: Optional[str]) -> Optional[str]:
    """任务/告警的旧短键 → 知识库平台键(检索按知识库键过滤)。"""
    return active_env_pack().knowledge_key(platform) if platform else platform


def build_materials(runtime, task: TriageTask) -> Tuple[TriageTask, List[str]]:
    kind = task.task_type or "default"
    lines: List[str] = []
    if kind == "qa":
        lines.extend(_snapshot(runtime, task))
    if kind == "alert":
        task, prerun = _alert_prerun(runtime, task)
        lines.extend(prerun)
    if kind in ("investigation", "artifact"):
        task, refs = _context_refs(runtime, task, kind)
        lines.extend(refs)
    if isinstance(task.risk_seed, dict) and task.risk_seed:
        lines.append("【资源风险】" + json.dumps(task.risk_seed, ensure_ascii=False)[:2000]
                     + "\n现状与样本用 read_resource_risk 读取。")
    if kind == "artifact" and task.report_path:
        lines.append(f"【本任务执行报告】{task.report_path};原始输出用 read_task_evidence 按 ref 读取。")
    return task, lines


def _snapshot(runtime, task: TriageTask) -> List[str]:
    snapshot = task.polling_snapshot
    if not snapshot and task.platform:
        reader = getattr(runtime.actions, "latest_resource_snapshot", None)
        snapshot = reader(task.platform, task.node) if callable(reader) else None
    if not isinstance(snapshot, dict) or snapshot.get("available") is False:
        return ["【本地资源快照】无可用快照;需要实时值时上节点查询。"]
    text = json.dumps(snapshot, ensure_ascii=False)
    if len(text) > SNAPSHOT_CHARS:
        text = text[:SNAPSHOT_CHARS] + "…(已截断,完整内容用 read_resource_snapshot 读取)"
    return [f"【本地资源快照】采样时间 {snapshot.get('collected_at') or '未知'};参考,不是实时证据:\n{text}"]


def _context_refs(runtime, task: TriageTask, kind: str) -> Tuple[TriageTask, List[str]]:
    build_context = getattr(runtime.actions, "build_context", None)
    if not callable(build_context):
        return task, []
    alert = task.alert_context or task.alert_event or {}
    risk = task.risk_seed if isinstance(task.risk_seed, dict) else {}
    service = task.service or alert.get("service") or risk.get("service")
    component = task.component or alert.get("component") or risk.get("component")
    from lib.kb.retrieval import RetrievalRequest

    request = RetrievalRequest(
        query=task.context_query or task.initial_message,
        profile=kind,  # 检索 profile 与任务类型同名(investigation / artifact)
        platform=_knowledge_platform(task.platform or alert.get("platform")),
        scope=task.scope or ("node" if task.node else None),
        service=str(service).strip().lower() if service else None,
        component=str(component).strip().lower() if component else None,
        node=task.node or alert.get("node") or alert.get("host"),
        alertname=str(alert.get("alertname") or "") or None,
        artifact_kind=str(alert.get("artifact_kind") or "") or None,
    )
    try:
        pack = build_context(request)
    except Exception as exc:  # noqa: BLE001 — 检索失败不挡排查
        return task, [f"【知识参考】ContextPack 构建失败({type(exc).__name__}),可用 kb_search 自行检索。"]
    if not isinstance(pack, dict):
        return task, []
    if task.risk_seed:
        pack = {**pack, "risk_seed": task.risk_seed}
    task = runtime.store.save_context_pack(task.task_id, pack, stage="initial")
    refs = _reference_lines(pack)
    if not refs:
        return task, ["【知识参考】ContextPack 未召回相关文档,可用 kb_search / kb_grep 自行检索。"]
    return task, ["【知识参考】以下文档可能相关(正文用 kb_read 读取;知识库结论须用现场数据复核):", *refs]


def _reference_lines(pack: Dict[str, Any]) -> List[str]:
    knowledge = pack.get("knowledge_context") or {}
    items = list(pack.get("required_reads") or [])
    for bucket in REF_BUCKETS:
        items.extend(knowledge.get(bucket) or [])
    lines, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            continue
        doc = str(item.get("doc_id") or item.get("path") or "")
        if not doc or doc in seen:
            continue
        seen.add(doc)
        reasons = item.get("reason") or ",".join(str(r) for r in (item.get("retrieval_reasons") or [])[:2])
        category = item.get("category") or item.get("doc_type") or ""
        lines.append(f"- {doc} | {item.get('title') or ''} | {category} | {reasons}".rstrip(" |"))
        if len(lines) >= MAX_REFS:
            break
    return lines


def _alert_prerun(runtime, task: TriageTask) -> Tuple[TriageTask, List[str]]:
    alert = task.alert_context or task.alert_event or {}
    alertname = str(alert.get("alertname") or "")
    platform = str(task.platform or alert.get("platform") or "")
    summary = "【告警】" + json.dumps(alert, ensure_ascii=False, default=str)[:3000]
    if not alertname or not platform:
        return task, [summary, "【告警预跑】缺少 alertname 或集群,未预跑 playbook;请自主排查。"]
    hit = runtime.actions.lookup_route(alertname, platform)
    if hit is None:
        return task, [summary, f"【告警预跑】告警 {alertname} 未命中精确路由,无登记 playbook;请自主排查。"]
    if hit.get("doc"):
        task = runtime.store.update(task.task_id, expected=TaskStatus.RUNNING, matched_doc=hit.get("doc"))
    lines = [summary, f"【告警预跑】告警 {alertname} 精确路由命中 {hit.get('doc') or '(无文档)'}(可 kb_read 阅读)"]
    playbooks, path_selection = select_playbooks(runtime.actions, task, alert, hit)
    if not playbooks:
        return task, [*lines, "路由文档未登记可执行 playbook;请自主排查。"]
    if not task.node:
        return task, [*lines, "缺少告警节点,未预跑 playbook;请自主排查。"]
    task = ensure_session(runtime.store, runtime.actions, task,
                          service=str(hit.get("service") or alert.get("service") or ""))
    for index, playbook in enumerate(playbooks, start=1):
        try:
            result, verification = execute_playbook(
                runtime.store, runtime.actions, runtime.invocations, task,
                invocation_id=f"alert-playbook-{index}", playbook=playbook, path_selection=path_selection,
            )
        except Exception as exc:  # noqa: BLE001 — 预跑失败不挡排查,模型照常自主排查
            lines.append(f"playbook {playbook} 执行失败:{type(exc).__name__}: {str(exc)[:300]}")
            continue
        lines.append(f"playbook {index}:{playbook} → verdict={verification['verdict']}"
                     f"{',结论成立' if verification['matched'] else ''}")
        for number, step in enumerate(result.get("steps") or [], start=1):
            if isinstance(step, dict):
                mark = "✓" if step.get("pass") else "✗"
                actual = " ".join(str(step.get("actual") or "").split())[:300]
                lines.append(f"  - playbook:{index}.{number} [{mark}] {step.get('cmd', '')} | 期望 "
                             f"{step.get('expect', '')} | 实际 {actual}")
        if verification["matched"]:
            break
    lines.append("先解读以上 playbook 结果(可用 playbook:<n>.<k> 作为证据 ref),不足再自主排查。")
    task = sync_live_report(runtime.store, runtime.actions, runtime.store.get(task.task_id), runtime._display_path)
    return task, lines
