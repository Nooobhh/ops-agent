"""Safe worker and queue health projection for the triage workspace."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends

from lib.triage_agent.model_registry import ModelRegistry
from lib.triage_agent.models import TaskStatus
from lib.triage_agent.supervisor import WorkerSupervisor
from lib.triage_agent.task_store import TaskStore
from web.server.context import RequestContext, request_context


_QUEUE_STATUSES = (
    TaskStatus.QUEUED,
    TaskStatus.RUNNING,
    TaskStatus.WAITING_INPUT,
    TaskStatus.WAITING_APPROVAL,
    TaskStatus.WAITING_OPERATION,
)


def router(
    *,
    store: TaskStore,
    supervisor: Optional[WorkerSupervisor],
    registry: ModelRegistry,
) -> APIRouter:
    routes = APIRouter()

    @routes.get("/api/triage-health")
    def triage_health(
        _context: RequestContext = Depends(request_context),
    ) -> dict:
        model_configured = registry.is_configured()
        if not model_configured:
            worker = {"state": "not_configured", "pid": None}
        elif supervisor is None:
            worker = {
                "state": "stopped",
                "pid": None,
            }
        else:
            raw_health = supervisor.health()
            worker = {
                "state": str(raw_health.get("state") or "stopped"),
                "pid": raw_health.get("pid"),
            }
        tasks = store.list(limit=10000)
        queue = {
            status.value: sum(task.status is status for task in tasks)
            for status in _QUEUE_STATUSES
        }
        return {
            "schema_version": 1,
            "model": "configured" if model_configured else "not_configured",
            "can_create": (
                model_configured and worker["state"] == "running"
            ),
            "worker": worker,
            "queue": queue,
        }

    return routes
