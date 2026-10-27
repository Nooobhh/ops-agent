import json

import pytest

from lib.alerts.model import AlertEvent
from lib.alerts.notify import render_alert_notification, render_resource_risk_block
from lib.alerts.templates import (
    DEFAULT_MARKDOWN_TEMPLATE,
    load_notification_settings,
    render_template,
    save_notification_settings,
)


def test_template_renders_known_variables_and_defaults_missing_values():
    assert render_template("**{title}** {value} {threshold}", {
        "title": "CPU 异常", "value": "95%",
    }) == "**CPU 异常** 95% -"


def test_template_rejects_unknown_and_dynamic_format_fields():
    with pytest.raises(ValueError, match="不支持的通知模板变量"):
        render_template("{__class__}", {})
    with pytest.raises(ValueError, match="不支持格式化修饰"):
        render_template("{title!r}", {"title": "x"})


def test_notification_settings_roundtrip_and_default_contract(tmp_path):
    initial = load_notification_settings(tmp_path)
    assert initial["format"] == "markdown"
    assert initial["template"] == ""

    saved = save_notification_settings(tmp_path, {
        "format": "markdown", "template": "## {title}",
    })
    assert saved["template"] == "## {title}"
    assert load_notification_settings(tmp_path)["template"] == "## {title}"
    assert json.loads((tmp_path / "notify-settings.json").read_text())["schema_version"] == 1
    assert DEFAULT_MARKDOWN_TEMPLATE


def test_custom_template_is_shared_by_alert_and_resource_messages():
    template = "[{kind}] {severity_upper} {title} @ {target} | {value} | {description}"
    event = AlertEvent(
        source="prometheus", platform="alpha", alertname="CPUHigh",
        severity="warning", host="node1", host_raw="node1", service="system",
        state="firing", detail="CPU 超过阈值",
    )
    alert = render_alert_notification(
        event, {"first_seen": "2026-09-03T00:00:00+00:00"}, None,
        template=template,
    )
    resource = render_resource_risk_block({
        "key": "resource:alpha:cpu:node1", "severity": "critical",
        "platform": "alpha", "node": "node1", "resource": "cpu",
        "title": "CPU 持续高负载", "description": "P95 95%",
        "evidence": {"p95": 95, "warning_pct": 80, "critical_pct": 90},
    }, template=template)

    assert alert == "[告警触发] WARNING CPUHigh @ node1 | - | CPU 超过阈值"
    assert resource == "[资源异常] CRITICAL CPU 持续高负载 @ node1 | 95.0% | P95 95%"
