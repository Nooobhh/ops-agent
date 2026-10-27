"""告警源:核心内置 Prometheus(经插件 feeds.alerts);其余来源由环境包 ``alert_sources`` 扩展按集群提供。"""
from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from lib.envpack import EnvPack, EnvPackError, active_env_pack, env_timezone
from lib.envpack.extensions import extension_decl, load_object

from .model import AlertEvent, normalize_host


class SourceError(Exception):
    def __init__(self, source: str, message: str):
        self.source = source
        super().__init__(f"{source}: {message}")


@dataclass(frozen=True)
class AlertSourceContext:
    """交给环境包告警源工厂的一次 tick 上下文;扩展源按集群各一个,源 id 为 ``类型:集群``。"""
    name: str
    source_id: str
    config: Mapping[str, Any]
    platform: str
    platform_config: Any
    nodes_by_platform: Mapping[str, Mapping[str, Any]]
    query_start_ms: int
    query_end_ms: int
    credentials: Any
    http_get: Callable[..., Any]


def alert_source_factory(pack: EnvPack, source_type: str) -> Callable[[AlertSourceContext], Any]:
    """环境包 ``alert_sources`` 扩展里该类型的工厂;未声明即显式失败(不当作空来源)。"""
    declared = extension_decl(pack, "alert_sources") or {}
    if not isinstance(declared, Mapping):
        raise EnvPackError("extensions.alert_sources 须为 {类型: 模块:对象}")
    ref = declared.get(source_type)
    if not ref:
        raise EnvPackError(f"告警源类型 {source_type!r} 未在环境包 extensions.alert_sources 声明")
    return load_object(pack, ref)


class PrometheusSource:
    """经 Prometheus 插件(MCP)取告警:当前告警、告警时段与规则定义三个 feeds 角色。

    feed(role, arguments) 返回插件的归一结果(``data`` / ``warnings``),失败抛异常;
    label_clusters 为插件 scope 的「标签值 → 集群旧短键」。
    时段是 MCP 按 1 分钟网格并成的;窗口起点取整到整分钟,同一时段在相邻 tick 里起止一致,
    重叠窗口里重复出现的事件靠 event_id 去重。
    """

    HISTORY_LIMIT = 1000
    MAX_SPLIT_DEPTH = 4

    def __init__(self, feed: Callable[[str, Dict[str, Any]], Mapping[str, Any]], label_clusters: Mapping[str, str],
                 nodes_by_platform: dict, query_start_ms=None, query_end_ms=None):
        self.feed = feed
        self.label_clusters = dict(label_clusters)
        self.nodes_by_platform = dict(nodes_by_platform)
        self.query_start_ms = query_start_ms
        self.query_end_ms = query_end_ms
        self.last_definitions: List[dict] = []

    def fetch(self) -> List[AlertEvent]:
        try:
            snapshot = self.fetch_snapshot()
            if self.query_start_ms is None or self.query_end_ms is None:
                return snapshot
            self.last_definitions = self._definitions(self._data("rules", {}))
            start = int(self.query_start_ms) // 60_000 * 60
            end = int(self.query_end_ms) // 1000
            spans, step = self._history(start, end, depth=0)
            return self._events_from_spans(spans, snapshot, window_start=start, window_end=end, step=step)
        except SourceError:
            raise
        except Exception as exc:
            code = getattr(exc, "code", "")
            raise SourceError("prometheus", f"{code}: {exc}" if code else str(exc)) from exc

    def fetch_snapshot(self) -> List[AlertEvent]:
        events = []
        for item in self._data("current", {"state": "firing"}).get("alerts") or []:
            labels = item.get("labels") or {}
            annotations = item.get("annotations") or {}
            platform, host, host_raw = self._place(labels)
            events.append(AlertEvent(
                source="prometheus",
                platform=platform,
                alertname=str(item.get("alertname") or labels.get("alertname") or ""),
                severity=_normalize_severity(labels.get("severity"), default="warning"),
                host=host,
                host_raw=host_raw,
                service=str(labels.get("service") or labels.get("job") or ""),
                state="firing",
                detail=_truncate(annotations.get("summary") or annotations.get("description") or ""),
                source_active_at=_stamp_to_iso(item.get("active_at")),
            ))
        return events

    def _data(self, role: str, arguments: Dict[str, Any]) -> Mapping[str, Any]:
        data = self.feed(role, arguments).get("data")
        if not isinstance(data, Mapping):
            raise ValueError(f"{role} 返回结构无效")
        return data

    def _history(self, start: int, end: int, *, depth: int) -> Tuple[List[Tuple[float, float, Mapping]], int]:
        """时段 (起, 止, 标签);超出条数上限就把窗口对半(落在整分钟)重查,仍超即失败。"""
        data = self._data("history", {"start": _cst(start), "end": _cst(end), "limit": self.HISTORY_LIMIT})
        spans = data.get("spans")
        if not isinstance(spans, list):
            raise ValueError("history 缺少 spans")
        step = int(data.get("step_s") or 60)
        if int(data.get("count") or 0) > len(spans):
            middle = (start + end) // 2 // 60 * 60
            if depth >= self.MAX_SPLIT_DEPTH or not start < middle < end:
                raise SourceError("prometheus", f"告警时段超过 {self.HISTORY_LIMIT} 条,二分 {depth} 次仍截断")
            first, _ = self._history(start, middle, depth=depth + 1)
            second, _ = self._history(middle, end, depth=depth + 1)
            return first + second, step
        out = []
        for span in spans:
            begin, finish = _stamp_seconds(span.get("start")), _stamp_seconds(span.get("end"))
            if begin is None or finish is None or finish < begin:
                raise ValueError("history 时段起止无效")
            out.append((begin, finish, span.get("labels") or {}))
        return out, step

    def _events_from_spans(self, spans, snapshot: List[AlertEvent], *, window_start: int, window_end: int,
                           step: int) -> List[AlertEvent]:
        snapshot_by_identity = {
            (event.platform, event.alertname, event.host or event.host_raw): event
            for event in snapshot
        }
        grouped: Dict[tuple, Dict[str, Any]] = {}
        for begin, finish, labels in spans:
            alertname = str(labels.get("alertname") or "")
            if not alertname:
                raise ValueError("history 时段缺少 alertname")
            platform, host, host_raw = self._place(labels)
            item = grouped.setdefault((platform, alertname, host or host_raw), {
                "intervals": [], "labels": labels, "host": host, "host_raw": host_raw,
            })
            item["intervals"].append((begin, finish))

        out: List[AlertEvent] = []
        for identity, item in grouped.items():
            platform, alertname, _host_identity = identity
            labels = item["labels"]
            snapshot_event = snapshot_by_identity.get(identity)
            # 同一主机多条序列(如按任务号区分)合并;间隔不超过 2 个步长视为连续,与 MCP 分段口径一致
            for begin, finish in _merge_intervals(item["intervals"], max_gap=2 * step):
                open_segment = finish >= window_end - 2 * step
                common = {
                    "source": "prometheus",
                    "platform": platform,
                    "alertname": alertname,
                    "severity": (
                        snapshot_event.severity
                        if snapshot_event is not None
                        else _normalize_severity(labels.get("severity"), default="warning")
                    ),
                    "host": item["host"],
                    "host_raw": item["host_raw"],
                    "service": (
                        snapshot_event.service
                        if snapshot_event is not None
                        else str(labels.get("service") or labels.get("job") or "")
                    ),
                    "maintenance": False,
                    "detail": snapshot_event.detail if snapshot_event is not None else "",
                    "source_active_at": (
                        snapshot_event.source_active_at
                        if open_segment and snapshot_event is not None and snapshot_event.source_active_at
                        else _epoch_seconds_to_iso(begin)
                    ),
                }
                # 已收口且起点被窗口截断的段,上一 tick 已经见过它的开始:只出收口,免得重复记一次「新告警」
                if open_segment or begin > window_start:
                    out.append(AlertEvent(**common, state="firing",
                                          source_last_seen_at=_epoch_seconds_to_iso(finish)))
                if not open_segment:
                    out.append(AlertEvent(**common, state="ok",
                                          source_resolved_at=_epoch_seconds_to_iso(finish + step)))
        return sorted(out, key=_event_source_timestamp)

    def _definitions(self, data: Mapping[str, Any]) -> List[dict]:
        definitions = []
        for rule in data.get("rules") or []:
            labels = rule.get("labels") or {}
            definitions.append({
                "alertname": str(rule.get("name") or ""),
                "group": str(rule.get("group") or ""),
                "severity": _normalize_severity(labels.get("severity"), default="warning"),
                # v2 规则多数不带平台(平台在告警序列标签上),此时记 unknown
                "platform": self._platform(labels),
                "state": str(rule.get("state") or "inactive").lower(),
            })
        return definitions

    def _platform(self, labels: Mapping[str, Any]) -> str:
        cluster = self.label_clusters.get(str(labels.get("platform") or ""))
        return cluster if cluster in active_env_pack().cluster_keys("managed") else "unknown"

    def _place(self, labels: Mapping[str, Any]) -> Tuple[str, Optional[str], str]:
        """标签 → (平台, 归一主机, 原始主机);平台标签不在映射里时按 FQDN 推断。"""
        host_raw = str(labels.get("instance") or labels.get("host") or "")
        platform = self._platform(labels)
        if platform == "unknown":
            platform = _platform_from_fqdn(host_raw, self.nodes_by_platform)
        if platform == "unknown":
            host = next((
                normalized
                for nodes in self.nodes_by_platform.values()
                for normalized in [normalize_host(host_raw, nodes)]
                if normalized is not None
            ), None)
        else:
            host = normalize_host(host_raw, self.nodes_by_platform.get(platform, {}))
        return platform, host, host_raw


def _normalize_severity(value, default="unknown") -> str:
    text = str(value or "").lower()
    if text in {"critical", "crit", "p0"}:
        return "critical"
    if text in {"warning", "warn", "p1", "p2"}:
        return "warning"
    if text == "unknown":
        return "unknown"
    return default


def _truncate(value: Any, limit: int = 200) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit]


def _epoch_seconds_to_iso(value: Any) -> str:
    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()


def _merge_intervals(intervals: List[Tuple[float, float]], *, max_gap: float) -> List[Tuple[float, float]]:
    merged: List[List[float]] = []
    for begin, finish in sorted(intervals):
        if merged and begin - merged[-1][1] <= max_gap:
            merged[-1][1] = max(merged[-1][1], finish)
        else:
            merged.append([begin, finish])
    return [(begin, finish) for begin, finish in merged]


def _stamp_seconds(stamp: Any) -> Optional[float]:
    """ops-mcp 时间双视图 → epoch 秒。取 real(已校正上游时钟偏差);字符串无时区,按环境时区(CST)解析。"""
    text = stamp.get("real") if isinstance(stamp, Mapping) else None
    if not text:
        return None
    try:
        return datetime.strptime(str(text), "%Y-%m-%d %H:%M:%S").replace(tzinfo=env_timezone()).timestamp()
    except ValueError:
        return None


def _stamp_to_iso(stamp: Any) -> Optional[str]:
    seconds = _stamp_seconds(stamp)
    return None if seconds is None else _epoch_seconds_to_iso(seconds)


def _cst(seconds: int) -> str:
    """epoch 秒 → ops-mcp 时间参数(环境时区 'YYYY-MM-DD HH:MM:SS')。"""
    return datetime.fromtimestamp(seconds, tz=env_timezone()).strftime("%Y-%m-%d %H:%M:%S")


def _event_source_timestamp(event: AlertEvent) -> str:
    if event.state == "ok":
        return event.source_resolved_at or event.source_active_at or ""
    return event.source_active_at or event.source_last_seen_at or ""


def _platform_from_fqdn(host_raw: str, nodes_by_platform: Mapping[str, Any]) -> str:
    value = str(host_raw or "").strip().lower().rstrip(".")
    if value.count(":") == 1:
        host, port = value.rsplit(":", 1)
        if port.isdigit():
            value = host
    domain_labels = set(value.split(".")[1:])
    matches = [
        str(platform)
        for platform in nodes_by_platform
        if str(platform).lower() in domain_labels
    ]
    return matches[0] if len(matches) == 1 else "unknown"
