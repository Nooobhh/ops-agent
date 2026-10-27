"""本机用户:数据根所有者的 ID 与显示名,新记录的作者来源。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field

from lib.datastore import DataStoreError
from lib.datastore.users import DISPLAY_NAME_MAX, LocalUserDirectory
from lib.triage_agent.models import StrictModel
from web.server.context import RequestContext, request_context


class LocalUserBody(StrictModel):
    id: str = Field(min_length=1, max_length=64)
    display_name: str = Field(default="", max_length=DISPLAY_NAME_MAX)


def router(*, users: LocalUserDirectory) -> APIRouter:
    routes = APIRouter()

    def payload() -> dict:
        user, error = users.load()
        return {
            "schema_version": 1,
            "configured": user is not None,
            "user": user.to_dict() if user is not None else None,
            "error": error,
        }

    @routes.get("/api/local-user")
    def get_local_user(_context: RequestContext = Depends(request_context)) -> dict:
        return payload()

    @routes.put("/api/local-user")
    def put_local_user(
        body: LocalUserBody,
        _context: RequestContext = Depends(request_context),
    ) -> dict:
        try:
            users.save_local_user(body.id, body.display_name)
        except DataStoreError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return payload()

    return routes
