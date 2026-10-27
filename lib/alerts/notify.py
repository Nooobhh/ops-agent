"""Notification rendering, channel delivery, and retry orchestration."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, List, Optional
from urllib import request

from .model import AlertEvent
from .templates import render_template

WECOM_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
MAX_MARKDOWN_BYTES = 4096
MAX_FLUSH_PER_TICK = 10


class BaseNotifier:
    channel_type = "unknown"

    def __init__(self, queue_path):
        self.queue_path = Path(queue_path)
        self.dropped_expired = 0

    def dispatch(self, blocks: List[str]) -> None:
        pages = _paginate([_fit_block(block, MAX_MARKDOWN_BYTES) for block in blocks])
        if len(pages) > 1:
            fitted_pages = []
            for i, page in enumerate(pages, 1):
                header = f"(第 {i}/{len(pages)} 页)\n"
                budget = MAX_MARKDOWN_BYTES - len(header.encode("utf-8"))
                fitted_pages.append(header + _fit_block(page, budget))
            pages = fitted_pages
        for page in pages:
            if not self.send(page):
                self._enqueue(page)

    def flush_queue(self, limit: int | None = None) -> None:
        self._flush_queue(limit, eligible_lines=None)

    def _flush_queue(self, limit: int | None, *, eligible_lines: int | None) -> None:
        if not self.queue_path.exists():
            return
        now = datetime.now(timezone.utc)
        keep: List[str] = []
        attempted = 0
        for index, line in enumerate(self.queue_path.read_text(encoding="utf-8").splitlines()):
            if eligible_lines is not None and index >= eligible_lines:
                keep.append(line)
                continue
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                ts = _parse_iso(item.get("ts"))
                content = str(item.get("content") or "")
            except Exception:
                self.dropped_expired += 1
                continue
            if ts is None or now - ts >= timedelta(hours=24):
                self.dropped_expired += 1
                continue
            if limit is not None and attempted >= max(0, limit):
                keep.append(line)
                continue
            attempted += 1
            if not self.send(content):
                keep.append(line)
        self.queue_path.parent.mkdir(parents=True, exist_ok=True)
        self.queue_path.write_text(
            "".join(line + "\n" for line in keep),
            encoding="utf-8",
        )

    def _queue_line_count(self) -> int:
        try:
            return len(self.queue_path.read_text(encoding="utf-8").splitlines()) \
                if self.queue_path.exists() else 0
        except OSError:
            return 0

    def _flush_queue_snapshot(self, limit: int, eligible_lines: int) -> None:
        self._flush_queue(limit, eligible_lines=eligible_lines)

    def send(self, content: str) -> bool:
        raise NotImplementedError

    def probe(self, content: str) -> dict:
        raise NotImplementedError

    def _enqueue(self, content: str) -> None:
        self.queue_path.parent.mkdir(parents=True, exist_ok=True)
        with self.queue_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "content": content,
            }, ensure_ascii=False) + "\n")


class WecomNotifier(BaseNotifier):
    channel_type = "wecom"

    def __init__(self, webhook_key, queue_path):
        super().__init__(queue_path)
        self.webhook_key = str(webhook_key)

    def send_markdown(self, content: str) -> bool:
        return self._send_result(content)["ok"]

    def send(self, content: str) -> bool:
        return self.send_markdown(content)

    def probe(self, content: str) -> dict:
        return self._send_result(content)

    def _send_result(self, content: str) -> dict:
        body = json.dumps(
            {"msgtype": "markdown", "markdown": {"content": content}},
            ensure_ascii=False,
        ).encode("utf-8")
        req = request.Request(
            f"{WECOM_URL}?key={self.webhook_key}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=10) as resp:
                if getattr(resp, "status", 200) != 200:
                    return {"ok": False, "code": getattr(resp, "status", None), "msg": "HTTP 请求失败"}
                data = json.loads(resp.read().decode("utf-8"))
                code = data.get("errcode")
                return {"ok": code == 0, "code": code, "msg": str(data.get("errmsg") or "")}
        except Exception as exc:
            return _transport_error(exc)


class FeishuNotifier(BaseNotifier):
    channel_type = "feishu"

    def __init__(self, webhook_url, secret, queue_path):
        super().__init__(queue_path)
        self.webhook_url = str(webhook_url)
        self.secret = str(secret or "")

    def dispatch(self, blocks: List[str]) -> None:
        super().dispatch([_markdown_to_text(block) for block in blocks])

    def send(self, content: str) -> bool:
        return self._send_result(content)["ok"]

    def probe(self, content: str) -> dict:
        return self._send_result(content)

    def _send_result(self, content: str) -> dict:
        payload = {"msg_type": "text", "content": {"text": content}}
        if self.secret:
            timestamp = str(int(time.time()))
            signing_key = f"{timestamp}\n{self.secret}".encode("utf-8")
            payload["timestamp"] = timestamp
            payload["sign"] = base64.b64encode(
                hmac.new(signing_key, b"", digestmod=hashlib.sha256).digest()
            ).decode("ascii")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(
            self.webhook_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=10) as resp:
                if getattr(resp, "status", 200) != 200:
                    return {"ok": False, "code": getattr(resp, "status", None), "msg": "HTTP 请求失败"}
                data = json.loads(resp.read().decode("utf-8"))
                code = data.get("code")
                return {"ok": code == 0, "code": code, "msg": str(data.get("msg") or "")}
        except Exception as exc:
            return _transport_error(exc)


class MultiNotifier:
    def __init__(self, notifiers: List[BaseNotifier]):
        self.notifiers = list(notifiers)

    def dispatch(self, blocks: List[str]) -> None:
        queue_snapshots = [notifier._queue_line_count() for notifier in self.notifiers]
        self._parallel("dispatch", blocks)
        self._parallel_flush_snapshots(queue_snapshots)

    def flush_queue(self) -> None:
        self._parallel("flush_queue", MAX_FLUSH_PER_TICK)

    def _parallel(self, method: str, argument) -> None:
        if not self.notifiers:
            return
        with ThreadPoolExecutor(max_workers=len(self.notifiers)) as executor:
            futures = [
                (notifier, executor.submit(getattr(notifier, method), argument))
                for notifier in self.notifiers
            ]
            for notifier, future in futures:
                try:
                    future.result()
                except Exception as exc:
                    _warn_channel_exception(notifier, method, exc)

    def _parallel_flush_snapshots(self, queue_snapshots: List[int]) -> None:
        if not self.notifiers:
            return
        with ThreadPoolExecutor(max_workers=len(self.notifiers)) as executor:
            futures = [
                (
                    notifier,
                    executor.submit(
                        notifier._flush_queue_snapshot,
                        MAX_FLUSH_PER_TICK,
                        eligible_lines,
                    ),
                )
                for notifier, eligible_lines in zip(self.notifiers, queue_snapshots)
            ]
            for notifier, future in futures:
                try:
                    future.result()
                except Exception as exc:
                    _warn_channel_exception(notifier, "flush_queue", exc)


def _markdown_to_text(content: str) -> str:
    text = re.sub(r"</?font(?:\s+[^>]*)?>", "", content, flags=re.IGNORECASE)
    text = text.replace("**", "").replace("`", "")
    return re.sub(r"^> ", "", text, flags=re.MULTILINE)


def _transport_error(exc: Exception) -> dict:
    return {"ok": False, "code": None, "msg": f"网络请求失败: {type(exc).__name__}"}


def _warn_channel_exception(notifier: BaseNotifier, method: str, exc: Exception) -> None:
    channel = getattr(notifier, "channel_type", None)
    if not channel or channel == "unknown":
        channel = notifier.__class__.__name__
    print(
        f"warning: notification channel {channel} {method} failed: {type(exc).__name__}",
        file=sys.stderr,
    )


_SEVERITY_COLOR = {"CRITICAL": "warning", "WARNING": "comment", "UNKNOWN": "comment"}


def render_event_block(event: AlertEvent, entry, triage_outcome: Optional[Any]) -> str:
    """企微群机器人 markdown 渲染;首行与接手行格式锁定(测试断言)。"""
    target = event.host or event.host_raw
    sev = event.severity.upper()
    color = _SEVERITY_COLOR.get(sev, "comment")
    lines = [f"【<font color=\"{color}\">{sev}·{event.platform}</font>】**{event.alertname}** @ `{target}`"]
    lines.append(f"> 来源 `{event.source}` · 服务 `{event.service or 'n/a'}`")
    lines.append(f"> 首见 `{entry.get('first_seen') or 'n/a'}`")
    lines.append(f"> 分级 **{entry.get('class') or 'n/a'}** · {entry.get('class_reason') or entry.get('reason') or 'n/a'}")

    if triage_outcome is None:
        lines.append("> <font color=\"comment\">知识库未命中 → 人工接手建议</font>")
        return "\n".join(lines)

    matched_doc = getattr(triage_outcome, "matched_doc", None)
    if matched_doc:
        lines.append(f"> 命中文档 `{matched_doc}`")
    else:
        lines.append("> <font color=\"comment\">知识库未命中 → 人工接手建议</font>")
    if getattr(triage_outcome, "verdict", None):
        lines.append(f"> verdict **{triage_outcome.verdict}**")
    if getattr(triage_outcome, "conclusion_hint", None):
        lines.append(f"> {triage_outcome.conclusion_hint}")
    for step in getattr(triage_outcome, "failed_steps", None) or []:
        lines.append(
            f"> <font color=\"warning\">✗ {step.get('name')}</font> `{step.get('actual')}`(expect `{step.get('expect')}`)"
        )
    if getattr(triage_outcome, "run_id", None):
        lines.append(f"接手: records/troubleshoot/{triage_outcome.run_id}")
    elif getattr(triage_outcome, "error", None):
        lines.append(f"> <font color=\"warning\">采证失败: {triage_outcome.error}</font>")
    return "\n".join(lines)


def render_alert_notification(
    event: AlertEvent,
    entry,
    triage_outcome: Optional[Any],
    *,
    template: str | None = None,
    kind: str = "告警触发",
    title: str | None = None,
    description: str | None = None,
    target: str | None = None,
) -> str:
    """Render an alert with the configured template or legacy fallback."""
    if not template:
        return render_event_block(event, entry, triage_outcome)
    return render_template(template, alert_template_values(
        event,
        entry,
        triage_outcome,
        kind=kind,
        title=title,
        description=description,
        target=target,
    ))


def render_resource_risk_block(
    finding: dict,
    *,
    template: str | None = None,
    now: datetime | None = None,
) -> str:
    """Render one analyzer finding for a webhook notification."""
    evidence = finding.get("evidence") or {}
    resource = str(finding.get("resource") or "resource")
    metric = evidence.get("label") or evidence.get("metric") or resource
    service = evidence.get("service") or evidence.get("job") or resource
    value, threshold = _resource_value_threshold(
        resource, evidence, finding.get("severity"),
    )
    values = {
        "kind": "资源预测告警" if finding.get("alert_type") == "forecast" else "资源异常",
        "title": finding.get("title") or "资源风险",
        "severity": finding.get("severity") or "warning",
        "severity_upper": str(finding.get("severity") or "warning").upper(),
        "platform": finding.get("platform"),
        "target": finding.get("node"),
        "host": finding.get("node"),
        "service": service,
        "resource": resource,
        "metric": metric,
        "value": value,
        "threshold": threshold,
        "description": finding.get("description"),
        "first_seen": finding.get("first_observed_at") or finding.get("last_updated_at"),
        "last_updated_at": finding.get("last_updated_at"),
        "duration": _duration_label(finding.get("duration_seconds")),
        "status": "异常持续",
        "source": "resource_analyzer",
        "alertname": finding.get("title"),
        "key": finding.get("key"),
        "run_id": None,
        "time": (
            now.isoformat()
            if isinstance(now, datetime)
            else finding.get("last_updated_at")
        ),
    }
    if template:
        return render_template(template, values)
    return render_template(None, values)


def alert_template_values(
    event: AlertEvent,
    entry: dict,
    triage_outcome: Optional[Any],
    *,
    kind: str = "告警触发",
    title: str | None = None,
    description: str | None = None,
    target: str | None = None,
) -> dict:
    """Build the stable variable map exposed to user-authored templates."""
    outcome_description = getattr(triage_outcome, "conclusion_hint", None)
    if description is None:
        description = event.detail or outcome_description or "检测到告警，请及时排查。"
    severity = str(event.severity or "warning")
    first_seen = entry.get("first_seen") or event.source_active_at
    return {
        "kind": kind,
        "title": title or event.alertname,
        "severity": severity,
        "severity_upper": severity.upper(),
        "platform": event.platform,
        "target": target or event.host or event.host_raw,
        "host": event.host or event.host_raw,
        "service": event.service,
        "resource": None,
        "metric": None,
        "value": None,
        "threshold": None,
        "description": description,
        "first_seen": first_seen,
        "last_updated_at": event.source_last_seen_at or event.source_active_at or first_seen,
        "duration": _duration_label(_elapsed_seconds(first_seen)),
        "status": "active",
        "source": event.source,
        "alertname": event.alertname,
        "key": event.key,
        "run_id": getattr(triage_outcome, "run_id", None),
        "time": event.source_last_seen_at or event.source_active_at or first_seen,
    }


def _resource_value_threshold(
    resource: str,
    evidence: dict,
    severity: str | None = None,
) -> tuple[str | None, str | None]:
    if resource == "cpu":
        limit = (
            evidence.get("critical_pct")
            if severity == "critical"
            else evidence.get("warning_pct")
        )
        return _percent(evidence.get("p95")), _percent(limit)
    if resource == "memory":
        limit = (
            evidence.get("critical_available_pct")
            if severity == "critical"
            else evidence.get("warning_available_pct")
        )
        return _percent(evidence.get("available_pct")), _percent(limit)
    if resource == "filesystem":
        return _hours(evidence.get("eta_hours")), _percent(evidence.get("target_pct"))
    if resource == "jvm":
        if evidence.get("metric") == "heap_used_pct":
            limit = evidence.get("danger_pct") if severity == "critical" else evidence.get("warning_pct")
            return _percent(evidence.get("heap_used_pct")), _percent(limit)
        return _percent(evidence.get("after_gc_pct") or evidence.get("trend_current_pct")), _percent(evidence.get("danger_pct"))
    if resource == "business":
        value = _number_with_unit(evidence.get("value"), evidence.get("unit"))
        return value, _number_with_unit(evidence.get("threshold"), evidence.get("unit"))
    return None, None


def _percent(value) -> str | None:
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return None


def _hours(value) -> str | None:
    try:
        return f"{float(value):.1f} 小时"
    except (TypeError, ValueError):
        return None


def _number_with_unit(value, unit) -> str | None:
    try:
        number = f"{float(value):.1f}"
    except (TypeError, ValueError):
        return None
    return {
        "seconds": f"{number} 秒",
        "percent": f"{number}%",
        "per_second": f"{number}/秒",
    }.get(str(unit), number)


def _duration_label(seconds) -> str:
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return "-"
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    if days:
        return f"{days}天 {hours}小时"
    if hours:
        return f"{hours}小时 {minutes}分钟"
    if minutes:
        return f"{minutes}分钟"
    return f"{seconds}秒"


def _elapsed_seconds(value) -> int:
    parsed = _parse_iso(value)
    if parsed is None:
        return 0
    return max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))


def render_chronic_transition_block(
    event: AlertEvent, entry: dict, group: dict,
) -> str:
    """Render the one-time watch -> chronic group transition."""
    target = event.host or event.host_raw or "n/a"
    anchor = group.get("first_upgraded_at") or entry.get("first_seen")
    lines = [
        f"【<font color=\"comment\">CHRONIC·{event.platform or 'n/a'}</font>】"
        f"**{event.alertname or 'n/a'}** @ `{target}` 转为常驻",
        f"> 来源 `{event.source or 'n/a'}` · 首见 `{entry.get('first_seen') or 'n/a'}`",
        f"> 转型锚点 `{anchor or 'n/a'}` · 已存续 {_elapsed_label(anchor)}",
        "> <font color=\"comment\">持续告警已降噪，不再自动排查；后续仅关注主机增量和集群级恶化</font>",
    ]
    return _fit_block("\n".join(lines), MAX_MARKDOWN_BYTES)


def render_host_added_block(
    event: AlertEvent, entry: dict, new_hosts: list[str], active_count: int,
) -> str:
    """Render an append-only chronic host-history delta."""
    hosts = [str(host) for host in (new_hosts or []) if host]
    rendered_hosts = "、".join(f"`{host}`" for host in hosts) or "`n/a`"
    lines = [
        f"【<font color=\"comment\">CHRONIC·{event.platform or 'n/a'}</font>】"
        f"**{event.alertname or 'n/a'}** 新增受影响主机 {rendered_hosts}",
        f"> 新增 {len(hosts)} 台 · 当前受影响 {active_count} 台",
        f"> 首见 `{entry.get('first_seen') or 'n/a'}` · 来源 `{event.source or 'n/a'}`",
    ]
    return _fit_block("\n".join(lines), MAX_MARKDOWN_BYTES)


def render_cluster_wide_block(
    event: AlertEvent, entry: dict, cluster_info: dict,
) -> str:
    """Render a chronic group crossing the cluster-wide coverage threshold."""
    active_count = cluster_info.get("active_count", "n/a")
    denominator = cluster_info.get("denominator", "n/a")
    threshold = cluster_info.get("threshold", "n/a")
    lines = [
        f"【<font color=\"warning\">CLUSTER-WIDE·{event.platform or 'n/a'}</font>】"
        f"**{event.alertname or 'n/a'}** 覆盖 {active_count}/{denominator} 台,"
        "超 70%,疑似集群级问题",
        f"> 当前受影响 {active_count} 台 · 分母 {denominator} · threshold {threshold}",
        f"> 首见 `{entry.get('first_seen') or 'n/a'}` · 来源 `{event.source or 'n/a'}`",
    ]
    return _fit_block("\n".join(lines), MAX_MARKDOWN_BYTES)


def _elapsed_label(value) -> str:
    parsed = _parse_iso(value)
    if parsed is None:
        return "n/a"
    seconds = max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _paginate(blocks: List[str]) -> List[str]:
    pages: List[str] = []
    current: List[str] = []
    budget = MAX_MARKDOWN_BYTES - 32 if len(blocks) > 1 else MAX_MARKDOWN_BYTES
    for block in blocks:
        candidate = "\n\n".join([*current, block]) if current else block
        if current and len(candidate.encode("utf-8")) > budget:
            pages.append("\n\n".join(current))
            current = [block]
        else:
            current = [*current, block]
    if current:
        pages.append("\n\n".join(current))
    return pages


def _fit_block(block: str, limit: int) -> str:
    if len(block.encode("utf-8")) <= limit:
        return block
    lines = block.splitlines()
    first = lines[0] if lines else ""
    handoff = next((line for line in reversed(lines) if line.startswith("接手: records/")), "")
    middle_lines = [line for line in lines[1:] if line != handoff]
    prefix = first
    suffix = ("\n" + handoff) if handoff else ""
    marker = "\n...\n"
    budget = limit - len((prefix + marker + suffix).encode("utf-8"))
    if budget < 0:
        return _truncate_utf8(prefix, limit)
    middle = _truncate_utf8("\n".join(middle_lines), budget)
    return prefix + marker + middle + suffix


def _truncate_utf8(text: str, limit: int) -> str:
    out = []
    used = 0
    for ch in text:
        size = len(ch.encode("utf-8"))
        if used + size > limit:
            break
        out.append(ch)
        used += size
    return "".join(out)


def _parse_iso(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
