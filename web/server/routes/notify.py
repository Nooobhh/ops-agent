"""Notification channel configuration and saved-endpoint probes."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from lib.alerts.channels import CHANNEL_TYPES, load_channels, save_channel
from lib.alerts.notify import FeishuNotifier, WecomNotifier
from lib.alerts.templates import (
    DEFAULT_MARKDOWN_TEMPLATE,
    TEMPLATE_VARIABLES,
    NotificationTemplateError,
    load_notification_settings,
    save_notification_settings,
)
from lib.envpack import env_timezone
from web.server.context import RequestContext, request_context


_WECOM_KEY = re.compile(r"[A-Za-z0-9-]+")
_FEISHU_PATH = re.compile(r"^/open-apis/bot/v2/hook/.+")


class ChannelUpdate(BaseModel):
    enabled: bool
    webhook_key: str | None = None
    webhook: str | None = None
    secret: str | None = None


class TestPayload(BaseModel):
    webhook_key: str | None = None
    webhook: str | None = None
    secret: str | None = None


class NotificationSettingsUpdate(BaseModel):
    format: Literal["markdown"] = "markdown"
    template: str = Field(default="", max_length=12000)


def router(*, state_dir: Path) -> APIRouter:
    routes = APIRouter()
    state_dir = Path(state_dir)

    def all_statuses() -> dict:
        channels = load_channels(state_dir)
        return {
            "wecom": _channel_status("wecom", channels.get("wecom")),
            "feishu": _channel_status("feishu", channels.get("feishu")),
        }

    @routes.get("/api/notify/channels")
    def get_channels(_context: RequestContext = Depends(request_context)) -> dict:
        return all_statuses()

    @routes.get("/api/notify/settings")
    def get_notification_settings(
        _context: RequestContext = Depends(request_context),
    ) -> dict:
        return _notification_settings_payload(state_dir)

    @routes.put("/api/notify/settings")
    def put_notification_settings(
        body: NotificationSettingsUpdate,
        _context: RequestContext = Depends(request_context),
    ) -> dict:
        try:
            save_notification_settings(state_dir, body.model_dump())
        except NotificationTemplateError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _notification_settings_payload(state_dir)

    @routes.post("/api/notify/channels/{channel_type}")
    def update_channel(
        channel_type: str,
        body: ChannelUpdate,
        _context: RequestContext = Depends(request_context),
    ) -> dict:
        _require_channel(channel_type)
        if channel_type == "wecom" and body.webhook_key:
            if _WECOM_KEY.fullmatch(body.webhook_key) is None:
                raise HTTPException(status_code=422, detail="企微 webhook key 格式不正确")
        if channel_type == "feishu" and body.webhook:
            _validate_feishu_webhook(body.webhook)

        current = load_channels(state_dir).get(channel_type)
        current = current if isinstance(current, dict) else {}
        if channel_type == "wecom":
            credential = body.webhook_key or current.get("webhook_key")
            credentials_update = (
                {"webhook_key": body.webhook_key}
                if body.webhook_key is not None else None
            )
        else:
            credential = body.webhook or current.get("webhook")
            credentials_update = {}
            if body.webhook is not None:
                credentials_update["webhook"] = body.webhook
            if body.secret is not None:
                credentials_update["secret"] = body.secret
            if not credentials_update:
                credentials_update = None

        if body.enabled and not credential:
            raise HTTPException(status_code=422, detail="请先填写渠道凭证再启用")
        save_channel(
            state_dir,
            channel_type,
            enabled=body.enabled,
            credentials_update=credentials_update,
        )
        return all_statuses()

    @routes.post("/api/notify/channels/{channel_type}/test")
    def test_channel(
        channel_type: str,
        body: TestPayload = TestPayload(),
        _context: RequestContext = Depends(request_context),
    ) -> dict:
        _require_channel(channel_type)
        entry = load_channels(state_dir).get(channel_type)
        entry = entry if isinstance(entry, dict) else {}
        unused_queue = state_dir / ".notify-probe-unused.jsonl"
        if channel_type == "wecom":
            key = body.webhook_key or entry.get("webhook_key")
            if not key:
                raise HTTPException(status_code=422, detail="该渠道尚未配置凭证")
            if _WECOM_KEY.fullmatch(key) is None:
                raise HTTPException(
                    status_code=422,
                    detail="企微 webhook key 格式不正确",
                )
            notifier = WecomNotifier(key, unused_queue)
        else:
            webhook = body.webhook or entry.get("webhook")
            if not webhook:
                raise HTTPException(status_code=422, detail="该渠道尚未配置凭证")
            _validate_feishu_webhook(webhook)
            secret = (
                body.secret
                if body.secret is not None
                else entry.get("secret", "")
            )
            notifier = FeishuNotifier(webhook, secret, unused_queue)
        timestamp = datetime.now(env_timezone()).strftime("%Y-%m-%d %H:%M:%S")
        return notifier.probe(f"OpsAgent 通知渠道测试消息 {timestamp}")

    return routes


def _require_channel(channel_type: str) -> None:
    if channel_type not in CHANNEL_TYPES:
        raise HTTPException(status_code=404, detail="通知渠道不存在")


def _notification_settings_payload(state_dir: Path) -> dict:
    settings = load_notification_settings(state_dir)
    return {
        "format": settings["format"],
        "template": settings["template"],
        "effective_template": settings["template"] or DEFAULT_MARKDOWN_TEMPLATE,
        "default_template": DEFAULT_MARKDOWN_TEMPLATE,
        "variables": [dict(item) for item in TEMPLATE_VARIABLES],
        "updated_at": settings.get("updated_at"),
    }


def _channel_status(channel_type: str, raw_entry) -> dict:
    entry = raw_entry if isinstance(raw_entry, dict) else {}
    credential = entry.get("webhook_key" if channel_type == "wecom" else "webhook")
    configured = bool(credential)
    payload = {
        "enabled": bool(entry.get("enabled", False)),
        "configured": configured,
        "hint": _credential_hint(channel_type, str(credential)) if configured else None,
        "credential_masked": (
            _mask_credential(channel_type, str(credential)) if configured else None
        ),
        "credential_plain": str(credential) if configured else None,
        "updated_at": entry.get("updated_at"),
    }
    if channel_type == "feishu":
        payload["signed"] = bool(entry.get("secret"))
        payload["secret_plain"] = (
            str(entry["secret"])
            if configured and entry.get("secret")
            else None
        )
    return payload


def _credential_hint(channel_type: str, credential: str) -> str:
    value = _credential_token(channel_type, credential)
    return f"尾号 {value[-4:]}" if len(value) >= 4 else "已配置"


def _mask_credential(channel_type: str, credential: str) -> str:
    value = _credential_token(channel_type, credential)
    if len(value) >= 12:
        masked = f"{value[:4]}****{value[-4:]}"
    elif len(value) >= 4:
        masked = f"****{value[-4:]}"
    else:
        masked = "已配置"
    return f"…/hook/{masked}" if channel_type == "feishu" else masked


def _credential_token(channel_type: str, credential: str) -> str:
    if channel_type == "feishu":
        return urlparse(credential).path.rstrip("/").rsplit("/", 1)[-1]
    return credential


def _validate_feishu_webhook(webhook: str) -> None:
    try:
        parsed = urlparse(webhook)
        valid_port = parsed.port in (None, 443)
    except ValueError:
        valid_port = False
        parsed = urlparse("")
    if not (
        parsed.scheme == "https"
        and parsed.hostname == "open.feishu.cn"
        and valid_port
        and _FEISHU_PATH.fullmatch(parsed.path) is not None
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    ):
        raise HTTPException(status_code=422, detail="飞书 webhook 必须是官方机器人地址")
