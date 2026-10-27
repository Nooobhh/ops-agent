"""Shared request context dependency, reserved for future authentication."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import Request

from lib.datastore import Author


@dataclass(frozen=True)
class RequestContext:
    # 一阶段 = 本机用户(数据根所有者);二阶段多用户改由登录会话填充
    user: Optional[Author] = None


def request_context(request: Request) -> RequestContext:
    users = getattr(request.app.state, "users", None)
    return RequestContext(user=users.local_user() if users is not None else None)
