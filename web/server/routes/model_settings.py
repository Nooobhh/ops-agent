"""Sanitized model registry settings endpoints."""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict

from lib.triage_agent.model_registry import (
    AgentTypePolicy,
    ModelConflict,
    ModelNotFound,
    ModelRegistry,
    ModelRegistryError,
    ModelUpsert,
    PublicModelSettings,
    WorkflowDefaults,
)
from lib.triage_agent.models import TaskStatus
from lib.triage_agent.task_store import TaskStore
from web.server.context import RequestContext, request_context


_ACTIVE_STATUSES = {
    TaskStatus.QUEUED,
    TaskStatus.RUNNING,
    TaskStatus.WAITING_INPUT,
    TaskStatus.WAITING_APPROVAL,
    TaskStatus.WAITING_OPERATION,
}


class AgentPoliciesBody(BaseModel):
    """Strict request body for the five task-type Agent policies."""

    model_config = ConfigDict(extra="forbid")

    qa: AgentTypePolicy
    investigation: AgentTypePolicy
    alert: AgentTypePolicy
    artifact: AgentTypePolicy
    default: AgentTypePolicy


def router(
    *,
    registry: ModelRegistry,
    store: TaskStore,
    reconcile: Callable[[], None],
) -> APIRouter:
    routes = APIRouter()

    @routes.get("/api/model-settings")
    def get_settings(
        _context: RequestContext = Depends(request_context),
    ) -> PublicModelSettings:
        try:
            return registry.public_settings()
        except ModelRegistryError as exc:
            return PublicModelSettings(error=str(exc))

    @routes.post(
        "/api/model-settings/models",
        status_code=status.HTTP_201_CREATED,
    )
    def create_model(
        body: ModelUpsert,
        _context: RequestContext = Depends(request_context),
    ) -> PublicModelSettings:
        result = _call(registry.create_model, body)
        reconcile()
        return result

    @routes.put("/api/model-settings/models/{model_id}")
    def update_model(
        model_id: str,
        body: ModelUpsert,
        _context: RequestContext = Depends(request_context),
    ) -> PublicModelSettings:
        result = _call(registry.update_model, model_id, body)
        reconcile()
        return result

    @routes.delete("/api/model-settings/models/{model_id}")
    def delete_model(
        model_id: str,
        _context: RequestContext = Depends(request_context),
    ) -> PublicModelSettings:
        active_model_ids = {
            task.model_profile_id
            for task in store.list(limit=10000)
            if (
                task.status in _ACTIVE_STATUSES
                and task.model_profile_id is not None
            )
        }
        result = _call(
            registry.delete_model,
            model_id,
            active_model_ids=active_model_ids,
        )
        reconcile()
        return result

    @routes.put("/api/model-settings/defaults")
    def update_defaults(
        body: WorkflowDefaults,
        _context: RequestContext = Depends(request_context),
    ) -> PublicModelSettings:
        result = _call(registry.update_defaults, body)
        reconcile()
        return result

    @routes.put("/api/model-settings/agent-policies")
    def update_agent_policies(
        body: AgentPoliciesBody,
        _context: RequestContext = Depends(request_context),
    ) -> PublicModelSettings:
        result = _call(
            registry.update_agent_policies,
            {
                key: AgentTypePolicy.model_validate(value)
                for key, value in body.model_dump().items()
            },
        )
        reconcile()
        return result

    return routes


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except ModelNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ModelConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ModelRegistryError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
