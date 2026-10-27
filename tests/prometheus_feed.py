"""Prometheus 插件(MCP)告警 feeds 的内存替身:按 ops-mcp 返回结构给当前告警、时段与规则。"""
from datetime import datetime, timezone

from lib.envpack import env_timezone
from lib.plugins import PluginCallError

LABEL_CLUSTERS = {"alpha": "alpha", "beta": "beta"}


def stamp(seconds):
    text = datetime.fromtimestamp(seconds, env_timezone()).strftime("%Y-%m-%d %H:%M:%S")
    return {"server": text, "real": text}


def iso(seconds):
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


def firing(alertname="disk_full", *, labels=None, active_at=None, summary=""):
    labels = {"alertname": alertname, "instance": "hd01:9100", "severity": "critical", "platform": "alpha",
              **(labels or {})}
    return {"alertname": alertname, "state": "firing", "labels": labels, "annotations": {"summary": summary},
            "active_at": stamp(active_at) if active_at is not None else None}


def span(begin, end, alertname="disk_full", *, labels=None):
    labels = {"alertname": alertname, "instance": "hd01:9100", "severity": "critical", "platform": "alpha",
              **(labels or {})}
    return {"alertname": alertname, "labels": labels, "start": stamp(begin), "end": stamp(end)}


class FakeFeed:
    """feed(role, arguments);spans 可为函数 (start 秒, end 秒) → 时段列表,便于模拟截断与二分。"""

    def __init__(self, *, alerts=(), spans=(), rules=(), errors=None, limit=None):
        self.alerts = list(alerts)
        self.spans = spans
        self.rules = list(rules)
        self.errors = dict(errors or {})
        self.limit = limit
        self.calls = []

    def __call__(self, role, arguments):
        self.calls.append((role, dict(arguments)))
        if role in self.errors:
            raise self.errors[role]
        if role == "current":
            return {"data": {"count": len(self.alerts), "alerts": self.alerts}, "warnings": []}
        if role == "rules":
            return {"data": {"count": len(self.rules), "rules": self.rules}, "warnings": []}
        start, end = (parse_cst(arguments["start"]), parse_cst(arguments["end"]))
        spans = self.spans(start, end) if callable(self.spans) else list(self.spans)
        limit = self.limit or arguments.get("limit") or 500
        return {"data": {"step_s": 60, "count": len(spans), "spans": spans[:limit]}, "warnings": []}


def upstream_unavailable(source="victoriametrics"):
    return PluginCallError("UPSTREAM_UNAVAILABLE", f"list_alert_history: 连不上(数据源 {source})")


def parse_cst(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=env_timezone()).timestamp()
