"""工作项与计划:列表、详情(挂载解析)、编辑、挂载、计划导入、周期分桶。写一律经 WorkItemStore。"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field

from lib.datastore import DataStoreError, WorkItemConflict, WorkItemNotFound
from lib.datastore.work_items import CLOSED_STATUSES, STATUSES, validate_link
from lib.envpack import env_timezone
from lib.triage_agent.models import StrictModel
from lib.work_items.detail import index_for, resolve
from lib.work_items.links import assign_entries, weekly_buckets
from lib.work_items.suggest import candidates, suggest, unassigned_entries
from lib.work_items.table import parse_table
from web.server.context import RequestContext, request_context

Status = Literal["todo", "in_progress", "waiting", "done", "cancelled"]
Priority = Literal["high", "normal", "low"]
MAX_RANGE_DAYS = 31


class WorkItemCreate(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=4000)
    # Web 只建人工与告警来源;计划经导入,渠道只经 M11 适配器
    source: Literal["manual", "alert"] = "manual"
    source_ref: Optional[str] = Field(default=None, max_length=300)
    status: Status = "todo"
    assignee: Optional[str] = Field(default=None, max_length=64)
    priority: Priority = "normal"
    start_date: Optional[str] = Field(default=None, max_length=10)
    due_date: Optional[str] = Field(default=None, max_length=10)
    plan_id: Optional[str] = Field(default=None, max_length=40)
    plan_node: Optional[str] = Field(default=None, max_length=60)


class WorkItemPatch(StrictModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=4000)
    status: Optional[Status] = None
    assignee: Optional[str] = Field(default=None, max_length=64)
    priority: Optional[Priority] = None
    start_date: Optional[str] = Field(default=None, max_length=10)
    due_date: Optional[str] = Field(default=None, max_length=10)
    plan_id: Optional[str] = Field(default=None, max_length=40)
    plan_node: Optional[str] = Field(default=None, max_length=60)
    note: Optional[str] = Field(default=None, max_length=500)


class LinkBody(StrictModel):
    kind: Literal["task", "troubleshoot", "worklog"]
    ref: str = Field(min_length=1, max_length=160)


class PlanPatch(StrictModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=4000)


class RangeBody(StrictModel):
    start_date: str = Field(min_length=10, max_length=10)
    end_date: str = Field(min_length=10, max_length=10)


class ImportBody(StrictModel):
    text: str = Field(min_length=1, max_length=200_000)
    plan_id: Optional[str] = Field(default=None, max_length=40)
    plan_title: Optional[str] = Field(default=None, max_length=200)


def summary(item: Dict[str, Any]) -> Dict[str, Any]:
    """列表用:去掉 history,挂载只给计数。"""
    result = {key: value for key, value in item.items() if key not in {"history", "links"}}
    result["link_count"] = len(item.get("links") or [])
    return result


def _range(start_date: str, end_date: str) -> tuple[date, date]:
    try:
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="日期必须是 YYYY-MM-DD") from exc
    if end < start or (end - start).days >= MAX_RANGE_DAYS:
        raise HTTPException(status_code=422, detail=f"日期范围须在 1~{MAX_RANGE_DAYS} 天内")
    return start, end


def _match_client(model_registry):
    """匹配建议用周报默认模型(同属工作记录归纳);只读调用,不进任何任务快照。"""
    from lib.triage_agent.model_client import ModelConfig, OpenAICompatibleClient

    selection = model_registry.resolve("weekly")
    material = model_registry.client_material_for_task(selection, source="weekly")
    return OpenAICompatibleClient(ModelConfig(
        base_url=material.base_url,
        api_key=material.api_key,
        model=selection.model_name,
        reasoning_effort=selection.reasoning_effort,
        timeout_seconds=material.timeout_seconds,
        max_tool_turns=material.max_tool_turns,
    ), context={"client_role": "work-item-match"})


def router(*, datastore, task_store, model_registry=None) -> APIRouter:
    routes = APIRouter()
    items = datastore.work_items

    def author(context: RequestContext) -> Optional[str]:
        return context.user.id if context.user else datastore.users.owner_id()

    def call(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except WorkItemNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except WorkItemConflict as exc:
            raise HTTPException(status_code=409, detail={"message": str(exc), "owner_id": exc.owner_id}) from exc
        except DataStoreError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def check_ref(kind: str, ref: str) -> None:
        """挂载对象须存在:任务、排查记录、worklog 条目;先校验引用格式再触盘。"""
        call(validate_link, kind, ref)
        try:
            if kind == "task":
                task_store.get(ref)
            elif kind == "troubleshoot":
                if datastore.troubleshoot.session(ref) is None:
                    raise LookupError(ref)
            else:
                day, position = ref.split("#", 1)
                datastore.worklogs.get(date.fromisoformat(day), int(position))
        except (LookupError, ValueError, OSError, DataStoreError) as exc:
            raise HTTPException(status_code=404, detail=f"挂载对象不存在: {kind} {ref}") from exc
        except Exception as exc:  # noqa: BLE001 — TaskNotFound 等任务存储异常
            raise HTTPException(status_code=404, detail=f"挂载对象不存在: {kind} {ref}") from exc

    def index():
        return index_for(items.list(), task_store)

    @routes.get("/api/work-items")
    def list_items(
        status: Optional[List[Status]] = Query(default=None),
        include_closed: bool = False,
        plan_id: Optional[str] = None,
        source: Optional[str] = None,
        assignee: Optional[str] = None,
        q: Optional[str] = Query(default=None, max_length=200),
        _context: RequestContext = Depends(request_context),
    ):
        found = items.list(status=status, include_closed=include_closed or bool(status),
                           plan_id=plan_id, source=source, assignee=assignee, q=q)
        return {"schema_version": 1, "today": _today(), "items": [summary(item) for item in found]}

    @routes.post("/api/work-items", status_code=201)
    def create_item(body: WorkItemCreate, _context: RequestContext = Depends(request_context)):
        item = call(items.create, body.model_dump(), author=author(_context))
        return {"schema_version": 1, "item": item}

    @routes.get("/api/work-items/by-link")
    def by_link(
        kind: Literal["task", "troubleshoot", "worklog"],
        ref: str = Query(min_length=1, max_length=160),
        _context: RequestContext = Depends(request_context),
    ):
        derived = False
        if kind == "worklog":
            try:
                day = date.fromisoformat(ref.split("#", 1)[0])
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="worklog 引用须是 日期#序号") from exc
            owner = assign_entries(index(), datastore.worklogs.days(day, day)).get(ref)
            item = call(items.get, owner[0]) if owner else None
            derived = bool(owner and owner[1])
        else:
            item = call(items.find_link, kind, ref)
        return {"schema_version": 1, "item": summary(item) if item else None, "derived": derived}

    @routes.get("/api/work-items/worklog-map")
    def worklog_map(start_date: str, end_date: str, _context: RequestContext = Depends(request_context)):
        start, end = _range(start_date, end_date)
        assignments = assign_entries(index(), datastore.worklogs.days(start, end))
        cache: Dict[str, Dict[str, Any]] = {}
        refs = {}
        for ref, (item_id, derived) in assignments.items():
            if item_id not in cache:
                cache[item_id] = call(items.get, item_id)
            item = cache[item_id]
            refs[ref] = {"id": item_id, "title": item["title"], "status": item["status"], "derived": derived}
        return {"schema_version": 1, "refs": refs}

    @routes.get("/api/work-items/weekly")
    def weekly(start_date: str, end_date: str, _context: RequestContext = Depends(request_context)):
        start, end = _range(start_date, end_date)
        link_index = index()
        assignments = assign_entries(link_index, datastore.worklogs.days(start, end))
        buckets = weekly_buckets(items.list(), assignments, start, end, today=date.fromisoformat(_today()))
        return {
            "schema_version": 1,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "next_start_date": (end + timedelta(days=1)).isoformat(),
            "next_end_date": (end + timedelta(days=(end - start).days + 1)).isoformat(),
            "items": [{**entry, "item": summary(entry["item"])} for entry in buckets],
        }

    @routes.post("/api/work-items/match-suggestions")
    def match_suggestions(body: RangeBody, _context: RequestContext = Depends(request_context)):
        """本期未挂载的 worklog → 工作项建议;只返回,确认由前端逐条走挂载接口。"""
        start, end = _range(body.start_date, body.end_date)
        all_items = items.list()
        days = datastore.worklogs.days(start, end)
        entries = unassigned_entries(days, assign_entries(index_for(all_items, task_store), days))
        pool = candidates(all_items, start, end)
        if not entries or not pool:
            return {"schema_version": 1, "suggestions": [], "unassigned": len(entries)}
        if model_registry is None:
            raise HTTPException(status_code=503, detail="匹配建议模型不可用")
        try:
            client = _match_client(model_registry)
        except Exception as exc:  # noqa: BLE001 — 模型未配置等
            raise HTTPException(status_code=503, detail=f"匹配建议模型不可用: {exc}") from exc
        plans = {plan["id"]: plan["title"] for plan in items.list_plans()}
        try:
            found = suggest(client, entries, pool, plans)
        except Exception as exc:  # noqa: BLE001 — 超时、协议错误、非 JSON
            raise HTTPException(status_code=502, detail=f"匹配建议生成失败: {exc}") from exc
        return {"schema_version": 1, "suggestions": found, "unassigned": len(entries)}

    @routes.get("/api/work-items/{item_id}")
    def get_item(item_id: str, _context: RequestContext = Depends(request_context)):
        item = call(items.get, item_id)
        plan = call(items.get_plan, item["plan_id"]) if item.get("plan_id") else None
        links = resolve(item, task_store=task_store, troubleshoot=datastore.troubleshoot,
                        worklogs=datastore.worklogs, index=index())
        return {"schema_version": 1, "today": _today(), "item": item, "plan": plan, "links": links}

    @routes.patch("/api/work-items/{item_id}")
    def patch_item(item_id: str, body: WorkItemPatch, _context: RequestContext = Depends(request_context)):
        changes = body.model_dump(exclude_unset=True)
        note = changes.pop("note", None)
        item = call(items.update, item_id, changes, author=author(_context), note=note)
        return {"schema_version": 1, "item": item}

    @routes.post("/api/work-items/{item_id}/links", status_code=201)
    def add_link(item_id: str, body: LinkBody, _context: RequestContext = Depends(request_context)):
        call(items.get, item_id)
        check_ref(body.kind, body.ref)
        item = call(items.link, item_id, body.kind, body.ref, author=author(_context))
        return {"schema_version": 1, "item": item}

    @routes.delete("/api/work-items/{item_id}/links")
    def remove_link(
        item_id: str,
        kind: Literal["task", "troubleshoot", "worklog"],
        ref: str = Query(min_length=1, max_length=160),
        _context: RequestContext = Depends(request_context),
    ):
        item = call(items.unlink, item_id, kind, ref, author=author(_context))
        return {"schema_version": 1, "item": item}

    @routes.get("/api/work-item-plans")
    def list_plans(_context: RequestContext = Depends(request_context)):
        all_items = items.list()
        plans = []
        for plan in items.list_plans():
            members = [item for item in all_items if item.get("plan_id") == plan["id"]]
            plans.append({
                **{key: value for key, value in plan.items() if key != "history"},
                "progress": _progress(members),
            })
        return {"schema_version": 1, "plans": plans}

    @routes.get("/api/work-item-plans/{plan_id}")
    def get_plan(plan_id: str, _context: RequestContext = Depends(request_context)):
        plan = call(items.get_plan, plan_id)
        members = items.list(plan_id=plan_id)
        nodes = []
        for node in plan.get("nodes") or []:
            node_items = [item for item in members if item.get("plan_node") == node]
            # 节点内按计划顺序(开始日期),产物验证排最后
            node_items.sort(key=lambda item: (item.get("kind") == "acceptance",
                                              item.get("start_date") or item.get("due_date") or "9999"))
            nodes.append({"name": node, "progress": _progress(node_items),
                          "items": [summary(item) for item in node_items]})
        loose = [summary(item) for item in members if not item.get("plan_node")]
        return {"schema_version": 1, "today": _today(), "plan": plan, "progress": _progress(members),
                "nodes": nodes, "unassigned": loose}

    @routes.patch("/api/work-item-plans/{plan_id}")
    def patch_plan(plan_id: str, body: PlanPatch, _context: RequestContext = Depends(request_context)):
        plan = call(items.update_plan, plan_id, body.model_dump(exclude_unset=True), author=author(_context))
        return {"schema_version": 1, "plan": plan}

    def run_import(body: ImportBody, context: RequestContext, *, dry_run: bool) -> Dict[str, Any]:
        table = call(parse_table, body.text)
        result = call(items.import_rows, table.rows, plan_id=body.plan_id or None,
                      plan_title=(body.plan_title or "").strip() or None,
                      author=author(context), dry_run=dry_run)
        return {"schema_version": 1, "has_nodes": table.has_nodes, **result}

    @routes.post("/api/work-item-imports/preview")
    def preview_import(body: ImportBody, _context: RequestContext = Depends(request_context)):
        return run_import(body, _context, dry_run=True)

    @routes.post("/api/work-item-imports", status_code=201)
    def apply_import(body: ImportBody, _context: RequestContext = Depends(request_context)):
        return run_import(body, _context, dry_run=False)

    return routes


def _today() -> str:
    """环境时区的今天:前端据此标逾期,不自己猜时区。"""
    return datetime.now(env_timezone()).date().isoformat()


def _progress(members: List[Dict[str, Any]]) -> Dict[str, Any]:
    counted = [item for item in members if item.get("status") != "cancelled"]
    today = _today()
    return {
        "total": len(counted),
        "done": sum(1 for item in counted if item.get("status") == "done"),
        "by_status": {status: sum(1 for item in members if item.get("status") == status) for status in STATUSES},
        "overdue": sum(1 for item in counted if item.get("status") not in CLOSED_STATUSES
                       and item.get("due_date") and item["due_date"] < today),
    }
