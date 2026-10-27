"""Versioned HTTP adapter for durable triage tasks."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import Field, field_validator

from lib.data_root import resolve_data_root
from lib.datastore import DataStoreError, WorkItemNotFound, open_datastore
from lib.envpack.fields import TargetPlatform
from lib.kb.bases import resolve_knowledge_root
from lib.triage_agent.models import StrictModel, TaskSource, TaskStatus
from lib.triage_agent.model_registry import ModelRegistryError
from lib.triage_agent.model_registry import ReasoningEffort
from lib.triage_agent.command_policy import CommandPolicy
from lib.triage_agent.service import (
    CreateTaskRequest,
    InvalidTaskIdentifier,
    TaskConflict,
    InvalidTaskNode,
    TaskListRequest,
    TriageTaskService,
    _validate_identifier,
)
from lib.triage_agent.targets import infer_target_for_node, resolve_node as resolve_registered_node, target_nodes
from lib.triage_agent.task_store import (
    InvalidTransition,
    TaskNotFound,
)
from web.server.context import RequestContext, request_context


class MessageBody(StrictModel):
    message: str = Field(min_length=1, max_length=4000)

    @field_validator("message")
    @classmethod
    def nonblank_message(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("消息不能为空")
        return stripped


class RenameBody(StrictModel):
    title: str = Field(min_length=1, max_length=120)

    @field_validator("title")
    @classmethod
    def nonblank_title(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("任务名称不能为空")
        return stripped


class ApprovalBody(StrictModel):
    decision: Literal["approve", "revise", "reject"]
    feedback: Optional[str] = Field(default=None, max_length=2000)
    scope: Optional[Literal["once", "task", "always"]] = None


class RuntimeControlsBody(StrictModel):
    platform: Optional[TargetPlatform] = None
    node: Optional[str] = Field(default=None, min_length=1, max_length=128)
    model_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    reasoning_effort: Optional[ReasoningEffort] = None
    command_mode: Optional[Literal["whitelist", "auto", "bypass"]] = None

    @field_validator("node")
    @classmethod
    def validate_node(cls, value: Optional[str]) -> Optional[str]:
        return value.strip() if value is not None else None


class ResourceRiskTaskBody(StrictModel):
    message: str = Field(
        default="请调查该资源风险，先说明当前资源现状，再结合实时证据定位根因。",
        min_length=1,
        max_length=4000,
    )
    model_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    reasoning_effort: Optional[ReasoningEffort] = None
    command_mode: Optional[Literal["whitelist", "auto", "bypass"]] = None
    task_type: Literal["investigation"] = "investigation"

    @field_validator("message")
    @classmethod
    def nonblank_message(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("消息不能为空")
        return stripped


def router(*, service: TriageTaskService, repo_root: Path) -> APIRouter:
    routes = APIRouter()
    repo_root = Path(repo_root).resolve()
    # 任务里的 report_path / kb_doc 是相对数据根的展示路径(见 AgentRuntime._display_path)
    data_root = resolve_data_root(repo_root).resolve()
    datastore = open_datastore(repo_root)
    report_root = datastore.troubleshoot.root.resolve()
    knowledge_root = resolve_knowledge_root(repo_root)
    policy = CommandPolicy(service.store.root.parent / "command-policy.json")

    def known_nodes(platform: str):
        # 与 set_task_context 走同一份拓扑,失败语义也必须一致:那边拓扑不可读
        # 时抛 ActionDenied,这里若吞成空列表,调用方的「空表即跳过校验」会让
        # 任何脏节点写进任务
        try:
            return target_nodes(repo_root, platform)
        except (OSError, ValueError, KeyError) as exc:
            raise TaskConflict(f"平台拓扑不可读,无法校验节点: {platform}") from exc

    def resolve_node(platform: str, node: str):
        try:
            return resolve_registered_node(repo_root, platform, node)
        except (OSError, ValueError, KeyError) as exc:
            raise TaskConflict(f"平台拓扑不可读,无法校验节点: {platform}") from exc

    def infer_platform(node: str):
        try:
            return infer_target_for_node(repo_root, node)
        except (OSError, ValueError, KeyError) as exc:
            raise TaskConflict(f"平台拓扑不可读,无法推断节点所属平台: {node}") from exc

    @routes.post(
        "/api/triage-tasks",
        status_code=status.HTTP_201_CREATED,
    )
    def create_task(
        body: CreateTaskRequest,
        work_item_id: Optional[str] = Query(default=None, max_length=40),
        _context: RequestContext = Depends(request_context),
    ):
        # 从工作项发起:先确认工作项存在,建成即挂上(任务本身不存工作项)
        if work_item_id:
            try:
                datastore.work_items.get(work_item_id)
            except WorkItemNotFound as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        created_by = _context.user.id if _context.user else None
        detail = _call(
            service.create_manual,
            body,
            resolve_node=resolve_node,
            infer_platform=infer_platform,
            known_nodes=known_nodes,
            created_by=created_by,
        )
        if work_item_id:
            try:
                datastore.work_items.link(
                    work_item_id, "task", detail.task.task_id,
                    author=created_by or datastore.users.owner_id(),
                )
            except DataStoreError as exc:
                raise HTTPException(status_code=409, detail=f"任务已建,挂到工作项失败: {exc}") from exc
        return detail

    @routes.post(
        "/api/resource-risks/{risk_ref}/triage-task",
        status_code=status.HTTP_201_CREATED,
    )
    def create_resource_risk_task(
        risk_ref: str,
        body: ResourceRiskTaskBody,
        _context: RequestContext = Depends(request_context),
    ):
        """Create a target-bound investigation from one visible risk card."""
        risk_path = service.store.root.parent / "resource-risks.json"
        try:
            payload = json.loads(risk_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail="资源风险状态不可读") from exc
        findings = payload.get("findings") if isinstance(payload, dict) else None
        finding = next(
            (
                item for item in findings or []
                if isinstance(item, dict) and item.get("key") == risk_ref
            ),
            None,
        )
        if not isinstance(finding, dict):
            raise HTTPException(status_code=404, detail="当前资源风险中没有该引用")
        platform = str(finding.get("platform") or "").strip().lower()
        node = str(finding.get("node") or "").strip()
        evidence = finding.get("evidence") if isinstance(finding.get("evidence"), dict) else {}
        risk_description = str(finding.get("description") or "").strip()
        risk_service = evidence.get("service") or finding.get("service")
        component = (
            evidence.get("component")
            or finding.get("component")
            or risk_service
        )
        risk_seed = {
            "risk_ref": risk_ref,
            "platform": platform,
            "node": node,
            "service": risk_service,
            "component": component,
            "job": evidence.get("job"),
            "metric": evidence.get("metric"),
            "label": evidence.get("label"),
            "resource": finding.get("resource"),
            "alert_type": finding.get("alert_type"),
            "title": finding.get("title"),
            "description": risk_description,
            "value": evidence.get("value", evidence.get("current_value")),
            "unit": evidence.get("unit"),
            "threshold": evidence.get("threshold"),
            "severity": finding.get("severity"),
            "samples": evidence.get("samples"),
            "first_observed_at": finding.get("first_observed_at"),
            "last_updated_at": finding.get("last_updated_at"),
            "snapshot_ref": evidence.get("snapshot_ref"),
            "freshness_seconds": evidence.get("freshness_seconds"),
        }
        if not platform or not node:
            raise HTTPException(status_code=422, detail="资源风险缺少平台或节点")
        message = body.message
        if risk_description:
            message = f"{message}\n\n已发现资源风险：{risk_description[:1200]}"
        request = CreateTaskRequest(
            message=message,
            platform=platform,
            node=node,
            model_id=body.model_id,
            reasoning_effort=body.reasoning_effort,
            command_mode=body.command_mode,
            task_type=body.task_type,
            risk_seed=risk_seed,
            # 风险卡是新版 Web 入口,与 Composer 一致按轮停 waiting_input、由人点完成
            manual_artifacts=True,
        )
        return _call(
            service.create_manual,
            request,
            resolve_node=resolve_node,
            infer_platform=infer_platform,
            known_nodes=known_nodes,
            created_by=_context.user.id if _context.user else None,
        )

    @routes.get("/api/triage-tasks")
    def list_tasks(
        task_status: Optional[TaskStatus] = Query(default=None, alias="status"),
        source: Optional[TaskSource] = None,
        alert_key: Optional[str] = None,
        limit: int = Query(default=50, ge=1, le=200),
        _context: RequestContext = Depends(request_context),
    ):
        return service.list_tasks(TaskListRequest(
            status=task_status,
            source=source,
            alert_key=alert_key,
            limit=limit,
        ))

    @routes.get("/api/triage-tasks/{task_id}")
    def get_task(
        task_id: str,
        _context: RequestContext = Depends(request_context),
    ):
        return _call(service.get_task, task_id)

    @routes.patch("/api/triage-tasks/{task_id}")
    def rename_task(
        task_id: str,
        body: RenameBody,
        _context: RequestContext = Depends(request_context),
    ):
        return _call(service.rename, task_id, body.title)

    @routes.get("/api/triage-tasks/{task_id}/events")
    def get_events(
        task_id: str,
        after: Optional[str] = None,
        _context: RequestContext = Depends(request_context),
    ):
        return _call(service.events, task_id, after)

    @routes.get("/api/triage-tasks/{task_id}/report")
    def get_report(
        task_id: str,
        _context: RequestContext = Depends(request_context),
    ):
        detail = _call(service.get_task, task_id)
        report_path = detail.task.report_path
        if not report_path:
            raise HTTPException(status_code=404, detail="排查报告尚未生成")
        candidate = (data_root / report_path).resolve()
        try:
            candidate.relative_to(report_root)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="排查报告不可用") from exc
        if not candidate.is_file():
            raise HTTPException(status_code=404, detail="排查报告不可用")
        return FileResponse(candidate, media_type="text/markdown; charset=utf-8")

    @routes.get("/api/troubleshoot-reports/{run_id}")
    def get_report_by_run_id(
        run_id: str,
        _context: RequestContext = Depends(request_context),
    ):
        _validate_identifier(run_id, "排查 run_id")
        candidate = (report_root / run_id / "report.md").resolve()
        try:
            candidate.relative_to(report_root)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="排查报告不可用") from exc
        if not candidate.is_file():
            raise HTTPException(status_code=404, detail="排查报告不可用")
        return FileResponse(candidate, media_type="text/markdown; charset=utf-8")

    @routes.get("/api/triage-tasks/{task_id}/kb-doc")
    def get_kb_doc(
        task_id: str,
        _context: RequestContext = Depends(request_context),
    ):
        detail = _call(service.get_task, task_id)
        doc = detail.task.kb_doc
        if not doc:
            raise HTTPException(status_code=404, detail="知识库文档尚未生成")
        candidate = (data_root / doc).resolve()
        try:
            candidate.relative_to(knowledge_root)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="知识库文档不可用") from exc
        if not candidate.is_file():
            raise HTTPException(status_code=404, detail="知识库文档不可用")
        return FileResponse(candidate, media_type="text/markdown; charset=utf-8")

    @routes.get("/api/triage-tasks/{task_id}/artifacts")
    def list_artifacts(
        task_id: str,
        _context: RequestContext = Depends(request_context),
    ):
        detail = _call(service.get_task, task_id)
        if not detail.task.run_id:
            return {"schema_version": 1, "items": []}
        root = (report_root / detail.task.run_id / "artifacts").resolve()
        try:
            root.relative_to(report_root)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="任务产物目录不可用") from exc
        items = []
        if root.is_dir():
            for path in sorted(root.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True):
                if not path.is_file() or path.suffix not in {".md", ".sh", ".py"}:
                    continue
                stat = path.stat()
                items.append({
                    "filename": path.name,
                    "type": path.suffix.removeprefix("."),
                    "size": stat.st_size,
                    "created_at": stat.st_mtime,
                })
        return {"schema_version": 1, "items": items}

    @routes.get("/api/triage-tasks/{task_id}/artifacts/{filename}")
    def get_artifact(
        task_id: str,
        filename: str,
        download: bool = False,
        _context: RequestContext = Depends(request_context),
    ):
        detail = _call(service.get_task, task_id)
        if not detail.task.run_id or Path(filename).name != filename:
            raise HTTPException(status_code=404, detail="任务产物不存在")
        root = (report_root / detail.task.run_id / "artifacts").resolve()
        candidate = (root / filename).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="任务产物不存在") from exc
        if not candidate.is_file() or candidate.suffix not in {".md", ".sh", ".py"}:
            raise HTTPException(status_code=404, detail="任务产物不存在")
        if download:
            return FileResponse(
                candidate,
                media_type="text/plain; charset=utf-8",
                filename=candidate.name,
            )
        return PlainTextResponse(candidate.read_text(encoding="utf-8"))

    @routes.post("/api/triage-tasks/{task_id}/messages")
    def add_message(
        task_id: str,
        body: MessageBody,
        _context: RequestContext = Depends(request_context),
    ):
        return _call(
            service.add_message,
            task_id,
            body.message,
            resolve_node=resolve_node,
            infer_platform=infer_platform,
            known_nodes=known_nodes,
        )

    @routes.patch("/api/triage-tasks/{task_id}/runtime-controls")
    def update_runtime_controls(
        task_id: str,
        body: RuntimeControlsBody,
        _context: RequestContext = Depends(request_context),
    ):
        updates = {
            key: getattr(body, key)
            for key in body.model_fields_set
        }
        return _call(
            service.update_runtime_controls,
            task_id,
            updates,
            known_nodes=known_nodes,
            resolve_node=resolve_node,
        )

    @routes.post("/api/triage-tasks/{task_id}/retry")
    def retry_task(
        task_id: str,
        _context: RequestContext = Depends(request_context),
    ):
        return _call(service.retry, task_id)

    @routes.post("/api/triage-tasks/{task_id}/approvals/{approval_id}")
    def decide_approval(
        task_id: str,
        approval_id: str,
        body: ApprovalBody,
        context: RequestContext = Depends(request_context),
    ):
        user = context.user
        return _call(
            service.decide_approval,
            task_id,
            approval_id,
            decision=body.decision,
            feedback=body.feedback,
            scope=body.scope,
            decided_by={"id": user.id, "name": user.display_name} if user is not None else None,
        )

    @routes.post("/api/triage-tasks/{task_id}/cancel")
    def cancel_task(
        task_id: str,
        _context: RequestContext = Depends(request_context),
    ):
        return _call(service.cancel, task_id)

    @routes.post("/api/triage-tasks/{task_id}/complete")
    def complete_task(
        task_id: str,
        _context: RequestContext = Depends(request_context),
    ):
        return _call(service.complete, task_id)

    @routes.post("/api/triage-tasks/{task_id}/archive")
    def archive_task(
        task_id: str,
        _context: RequestContext = Depends(request_context),
    ):
        return _call(service.set_archived, task_id, True)

    @routes.post("/api/triage-tasks/{task_id}/unarchive")
    def unarchive_task(
        task_id: str,
        _context: RequestContext = Depends(request_context),
    ):
        return _call(service.set_archived, task_id, False)

    from .command_policy import router as command_policy_router
    routes.include_router(command_policy_router(policy=policy))

    return routes


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except TaskNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidTaskNode as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": str(exc),
                "platform": exc.platform,
                "available_nodes": exc.available_nodes,
            },
        ) from exc
    except (TaskConflict, InvalidTransition) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InvalidTaskIdentifier as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ModelRegistryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
