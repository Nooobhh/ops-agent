"""Persistent and safe Markdown notification templates."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from string import Formatter
from typing import Any, Mapping

from lib.atomic_file import atomic_write_json
from lib.file_lock import FileLock


DEFAULT_MARKDOWN_TEMPLATE = """## 【{severity_upper}】{title}

> 类型：{kind}
> 平台：{platform}
> 主机：{target}
> 服务：{service}
> 指标：{metric}
> 当前值：{value}
> 阈值：{threshold}
> 说明：{description}
> 首次发现：{first_seen}
> 最近观测：{last_updated_at}
> 持续时间：{duration}
"""

TEMPLATE_VARIABLES = (
    {"name": "kind", "label": "消息类型", "description": "告警触发、资源异常、常驻变化等。"},
    {"name": "title", "label": "标题", "description": "告警名称或资源风险标题。"},
    {"name": "severity", "label": "级别", "description": "critical、warning 或 info。"},
    {"name": "severity_upper", "label": "大写级别", "description": "CRITICAL、WARNING 或 INFO。"},
    {"name": "platform", "label": "平台", "description": "hd、iot 等平台标识。"},
    {"name": "target", "label": "主机/对象", "description": "告警主机或资源对象。"},
    {"name": "host", "label": "主机", "description": "主机名；集群级消息可能为空。"},
    {"name": "service", "label": "服务", "description": "服务名或资源类别。"},
    {"name": "resource", "label": "资源", "description": "cpu、memory、filesystem、jvm 或 business。"},
    {"name": "metric", "label": "指标", "description": "指标名称或业务指标展示名。"},
    {"name": "value", "label": "当前值", "description": "格式化后的当前值。"},
    {"name": "threshold", "label": "阈值", "description": "格式化后的告警阈值。"},
    {"name": "description", "label": "描述", "description": "面向运维人员的原因和证据摘要。"},
    {"name": "first_seen", "label": "首次发现", "description": "本次连续异常段的起始时间。"},
    {"name": "last_updated_at", "label": "最近观测", "description": "最近一次有效观测时间。"},
    {"name": "duration", "label": "持续时间", "description": "格式化后的持续时间。"},
    {"name": "status", "label": "状态", "description": "active、异常持续或资源异常。"},
    {"name": "source", "label": "来源", "description": "告警源类型(如 prometheus,或环境包扩展的类型)或 resource_analyzer。"},
    {"name": "alertname", "label": "告警名", "description": "原始告警名称。"},
    {"name": "key", "label": "唯一键", "description": "用于去重的稳定标识。"},
    {"name": "run_id", "label": "排查记录", "description": "自动排查记录 ID，没有时为空。"},
    {"name": "time", "label": "消息时间", "description": "本轮消息生成时间。"},
)
_ALLOWED_FIELDS = {item["name"] for item in TEMPLATE_VARIABLES}
_MAX_TEMPLATE_CHARS = 12000
_SETTINGS_FILE = "notify-settings.json"


class NotificationTemplateError(ValueError):
    """Raised when a user supplied notification template is invalid."""


def validate_template(template: str | None) -> str:
    value = "" if template is None else template
    if not isinstance(value, str):
        raise NotificationTemplateError("通知模板必须是字符串")
    if len(value) > _MAX_TEMPLATE_CHARS:
        raise NotificationTemplateError(
            f"通知模板不能超过 {_MAX_TEMPLATE_CHARS} 个字符",
        )
    try:
        parsed = list(Formatter().parse(value))
    except ValueError as exc:
        raise NotificationTemplateError(f"通知模板格式不正确: {exc}") from exc
    for _literal, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if field_name not in _ALLOWED_FIELDS:
            raise NotificationTemplateError(
                f"不支持的通知模板变量: {{{field_name}}}",
            )
        if format_spec or conversion:
            raise NotificationTemplateError(
                f"通知模板变量不支持格式化修饰: {{{field_name}}}",
            )
    return value.strip()


def render_template(template: str | None, values: Mapping[str, Any]) -> str:
    """Render a validated template, replacing absent values with ``-``."""
    value = validate_template(template)
    value = value or DEFAULT_MARKDOWN_TEMPLATE

    class _Values(dict):
        def __missing__(self, key):
            return "-"

    return value.format_map(_Values({key: _display(item) for key, item in values.items()})).strip()


def load_notification_settings(state_dir: Path) -> dict:
    path = Path(state_dir) / _SETTINGS_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        raw = {}
    raw = raw if isinstance(raw, Mapping) else {}
    try:
        template = validate_template(raw.get("template", ""))
    except NotificationTemplateError:
        template = ""
    return {
        "schema_version": 1,
        "format": "markdown",
        "template": template,
        "updated_at": raw.get("updated_at"),
        "updated_by": raw.get("updated_by"),
    }


def save_notification_settings(
    state_dir: Path,
    settings: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> dict:
    if str(settings.get("format", "markdown")) != "markdown":
        raise NotificationTemplateError("通知格式目前只支持 markdown")
    template = validate_template(settings.get("template", ""))
    normalized = {
        "schema_version": 1,
        "format": "markdown",
        "template": template,
        "updated_at": _utc_iso(now),
        "updated_by": "web",
    }
    state_dir = Path(state_dir)
    path = state_dir / _SETTINGS_FILE
    state_dir.mkdir(parents=True, exist_ok=True)
    with FileLock(str(state_dir / "notify-settings.lock"), timeout=10):
        atomic_write_json(path, normalized)
    return normalized


def effective_template(settings: Mapping[str, Any] | None) -> str:
    template = settings.get("template") if isinstance(settings, Mapping) else None
    return str(template or DEFAULT_MARKDOWN_TEMPLATE)


def _display(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return str(value)


def _utc_iso(now: datetime | None) -> str:
    value = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()

