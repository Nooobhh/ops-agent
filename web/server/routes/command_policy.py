"""Read and mode-only writes for the global command policy."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException

from lib.triage_agent.command_policy import CommandPolicy, CommandPolicyError
from lib.triage_agent.models import StrictModel
from web.server.context import RequestContext, request_context


class CommandPolicyBody(StrictModel):
    mode: Literal["whitelist", "auto", "bypass"]


def router(*, policy: CommandPolicy) -> APIRouter:
    routes = APIRouter()

    @routes.get("/api/command-policy")
    def get_command_policy(
        _context: RequestContext = Depends(request_context),
    ):
        config = policy.load()
        return {"mode": config.mode, "allow": list(config.allow), "deny": list(config.deny)}

    @routes.put("/api/command-policy")
    def set_command_policy(
        body: CommandPolicyBody,
        _context: RequestContext = Depends(request_context),
    ):
        try:
            config = policy.set_mode(body.mode)
        except CommandPolicyError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"mode": config.mode, "allow": list(config.allow), "deny": list(config.deny)}

    return routes
