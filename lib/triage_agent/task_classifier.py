"""No-tool task type classifier and Prometheus alert text extractor."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from lib.envpack import active_env_pack
from lib.kb.frontmatter import SERVICES
from lib.triage import load_routing, match_alert

from .model_client import ModelClient
from .targets import resolve_node as resolve_registered_node
from .task_types import (
    AlertEnvelope,
    ArtifactKind,
    TaskClassificationOutcome,
    TaskTypeDecision,
    envelope_from_mapping,
)


CLASSIFIER_VERSION = "task-type-v1"
CLASSIFICATION_VERSION = "task-classification-v2"
_ALERT_LABEL = re.compile(
    r"(?:告警项目|告警名|alertname|alert\s*name)"
    r"(?:\s*[】\]])?\s*[:：]\s*(.+)",
    re.IGNORECASE,
)
# 中台自身推送的通知(lib/alerts/templates 默认模板):「【WARNING】标题」+「类型/平台/主机/服务」
# 字段行。从企微复制出来可能是原始 Markdown(## / > / ** / <font>),也可能是渲染后的纯文本
_NOTIFY_TITLE = re.compile(r"^【(critical|warning|info)】\s*(.+)$", re.IGNORECASE)
_NOTIFY_FIELD = re.compile(r"^(类型|平台|主机|服务|说明|首次发现)\s*[:：]\s*(.*)$")
_MARKDOWN_NOISE = re.compile(r"<[^>]+>|\*\*")
# 告警类通知的类型 → 标题在告警名之后追加的后缀(scripts/alert_poll.py);资源风险通知不在此列
_ALERT_NOTIFY_KINDS = {
    "告警触发": "",
    "告警转为常驻": " 转为常驻",
    "常驻主机变化": " 新增受影响主机",
    "集群级告警": " 疑似集群级问题",
}
_HOST_LABEL = re.compile(
    r"(?:告警主机|主机|host|instance|节点)"
    r"(?:\s*[】\]])?\s*[:：]\s*([^\s\]】,，]+)",
    re.IGNORECASE,
)
_SERVICE_LABEL = re.compile(
    r"(?:服务|service|job)"
    r"(?:\s*[】\]])?\s*[:：]\s*([^\s\]】,，]+)",
    re.IGNORECASE,
)


# 平台词表取环境包:标签行认全部集群短键,自由文本只认 managed 集群
@lru_cache(maxsize=8)
def _platform_label_re(keys: tuple[str, ...]) -> re.Pattern:
    names = "|".join(re.escape(key) for key in keys) or "(?!)"
    return re.compile(
        r"(?:告警集群|集群|平台|platform)"
        r"(?:\s*[】\]])?\s*[:：]\s*(" + names + r")\b",
        re.IGNORECASE,
    )


@lru_cache(maxsize=8)
def _generic_platform_re(keys: tuple[str, ...]) -> re.Pattern:
    names = "|".join(re.escape(key) for key in keys) or "(?!)"
    return re.compile(r"(?<![A-Za-z0-9_])(" + names + r")(?![A-Za-z0-9_])", re.IGNORECASE)


def _generic_platform_search(text: str):
    return _generic_platform_re(active_env_pack().cluster_keys("managed")).search(text)


_GENERIC_NODE = re.compile(
    r"(?<![A-Za-z0-9_])node[-_]?\d+(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
# 开源组件的通用别名;环境专有服务的别名由环境包 knowledge.service_aliases 追加
_SERVICE_ALIASES = {
    "zookeeper": ("zookeeper", "zoo keeper"),
    "mapreduce": ("mapreduce", "map reduce"),
    "moosefs": ("moosefs",),
}
_COMPONENT_ALIASES = {
    "namenode": ("namenode", "name node", "名称节点"),
    "datanode": ("datanode", "data node", "数据节点"),
    "resourcemanager": ("resourcemanager", "resource manager"),
    "nodemanager": ("nodemanager", "node manager"),
    "regionserver": ("regionserver", "region server"),
    "hiveserver2": ("hiveserver2", "hive server 2"),
    "thriftserver": ("thriftserver", "thrift server"),
}
_COMPONENT_SERVICE = {
    "namenode": "hdfs", "datanode": "hdfs",
    "resourcemanager": "yarn", "nodemanager": "yarn",
    "regionserver": "hbase", "hiveserver2": "hive",
    "thriftserver": "spark",
}
_AMBIGUOUS_SERVICE_TERMS = {"report", "system", "network", "dt", "nfs"}
_ALERT_MARKER = re.compile(r"告警|alert", re.IGNORECASE)
_FIELD = re.compile(
    r"^\s*(?:[-*]\s*)?(?P<key>[A-Za-z][A-Za-z0-9_]*|[^:：\s]+)\s*[:：]\s*(?P<value>.+?)\s*$"
)

_ARTIFACT_PATTERNS = (
    ("script", re.compile(r"脚本|shell|python|监控脚本|采集脚本|代码")),
    ("report", re.compile(r"报告|报表|分析报告")),
    ("document", re.compile(r"文档|SOP|操作手册|方案|说明书", re.IGNORECASE)),
    ("table", re.compile(r"表格|清单|汇总表|导出数据")),
)
_ARTIFACT_INTENT = re.compile(
    r"生成|创建|编写|输出|产出|更新|修改|整理|写一个|写份|导出",
)
_QA_INTENT = re.compile(
    r"^(?:请问|帮我)?\s*(?:有哪些|哪些|是什么|什么是|在哪里|多少|列出|查看|查询|说明|解释)|"
    r"端口|依赖关系|配置项|节点分布|清单|当前|现在|实时|状态|占用|使用率|"
    r"cpu|load|负载|memory|heap|磁盘|disk|资源|内存占用|内存使用|内存情况",
)
_INVESTIGATION_INTENT = re.compile(
    r"排查|调查|诊断|故障|异常|失败|原因|为什么|为何|定位|分析|告警|高负载",
)
_CLUSTER_SCOPE = re.compile(
    r"集群级|集群范围|整集群|整个集群|全集群|全节点|所有节点|跨节点|"
    r"节点分布|整体容量|容量倾斜|节点间",
)


def extract_service_components(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return all explicitly named services and components, without choosing a primary."""
    normalized = str(text or "").lower()
    components = {
        name for name, aliases in _COMPONENT_ALIASES.items()
        if any(_has_term(normalized, alias) for alias in aliases)
    }
    services = {
        name for name in SERVICES - _AMBIGUOUS_SERVICE_TERMS
        if _has_term(normalized, name)
    }
    aliases_by_service = {**_SERVICE_ALIASES, **active_env_pack().service_aliases}
    services.update(
        name for name, aliases in aliases_by_service.items()
        if any(_has_term(normalized, alias) for alias in aliases)
    )
    services.difference_update(components)
    services.update(_COMPONENT_SERVICE[name] for name in components)
    return tuple(sorted(services)), tuple(sorted(components))


def extract_service_component(text: str) -> tuple[Optional[str], Optional[str]]:
    """Return a single target hint only when the text names exactly one."""
    services, components = extract_service_components(text)
    if len(services) > 1:
        return None, None
    service = services[0] if len(services) == 1 else None
    component = components[0] if len(components) == 1 else None
    return service, component


def _has_term(text: str, term: str) -> bool:
    return re.search(rf"(?<![a-z0-9_]){re.escape(term)}(?![a-z0-9_])", text) is not None


class TaskClassifier:
    """Classify with deterministic signals and optional no-tool model fallback."""

    def __init__(
        self,
        *,
        routing_path: Optional[Path] = None,
        route_lookup: Optional[Callable[[str, str], Any]] = None,
        node_resolver: Optional[Callable[[str, str], Optional[str]]] = None,
        model: Optional[ModelClient] = None,
        repo_root: Optional[Path] = None,
    ):
        self.routing_path = Path(routing_path).resolve() if routing_path else None
        self.route_lookup = route_lookup
        self.node_resolver = node_resolver
        self.model = model
        # 代码仓根:节点兜底读入库主机来源;routing_path 在数据根下,不能反推它
        self.repo_root = Path(repo_root).resolve() if repo_root else None

    def classify(
        self,
        message: str,
        *,
        alert_context: Any = None,
        platform: Optional[str] = None,
        node: Optional[str] = None,
        service: Optional[str] = None,
    ) -> TaskTypeDecision:
        text = str(message or "").strip()
        envelope = envelope_from_mapping(alert_context)
        if envelope is None and text:
            envelope = parse_alert_text(text)
        if (
            envelope is not None
            and envelope.host_raw
            and (not envelope.node or envelope.node == envelope.host_raw)
        ):
            resolved_node = self._resolve_node(
                platform or envelope.platform,
                envelope.host_raw,
            )
            if resolved_node:
                envelope = envelope.model_copy(update={"node": resolved_node})

        decision = self._classify_structured(
            text,
            envelope,
            platform=platform,
            node=node,
            service=service,
        )
        if decision.scope is None and decision.task_type in {"qa", "investigation", "alert"}:
            inferred_scope = (
                "cluster" if _CLUSTER_SCOPE.search(text)
                else "node" if decision.node or node or (envelope and (envelope.node or envelope.host_raw))
                else None
            )
            if inferred_scope:
                decision = decision.model_copy(update={"scope": inferred_scope})
        if decision.confidence >= 0.8 or self.model is None:
            return decision
        return self._classify_with_model(
            text,
            base=decision,
            alert_context=envelope,
            platform=platform,
            node=node,
            service=service,
        )

    def classify_and_summarize(
        self,
        message: str,
        *,
        alert_context: Any = None,
        platform: Optional[str] = None,
        node: Optional[str] = None,
        service: Optional[str] = None,
        model: Optional[ModelClient] = None,
    ) -> TaskClassificationOutcome:
        """Run the deterministic pre-check and exactly one no-tool model pass.

        The program result remains visible in the audit payload.  The model may
        enrich missing entities, but a semantic disagreement is deliberately
        left unresolved for an operator instead of silently picking a side.
        """
        text = str(message or "").strip()
        envelope = envelope_from_mapping(alert_context)
        if envelope is None and text:
            envelope = parse_alert_text(text)
        if (
            envelope is not None
            and envelope.host_raw
            and (not envelope.node or envelope.node == envelope.host_raw)
        ):
            resolved_node = self._resolve_node(
                platform or envelope.platform,
                envelope.host_raw,
            )
            if resolved_node:
                envelope = envelope.model_copy(update={"node": resolved_node})
        program = self._classify_structured(
            text,
            envelope,
            platform=platform,
            node=node,
            service=service,
        )
        program = self._complete_entities(
            text, program, envelope=envelope, platform=platform, node=node,
            service=service,
        )
        client = model or self.model
        if client is None:
            return TaskClassificationOutcome(
                program=program,
                final=program,
                display_title=program.title or default_display_title(text),
                intent=program.intent or _intent_for_decision(program),
                classifier_version=CLASSIFICATION_VERSION,
            )

        model_decision, model_title, model_intent = self._classify_model_once(
            text,
            base=program,
            alert_context=envelope,
            platform=platform,
            node=node,
            service=service,
            model=client,
        )
        if model_decision is None:
            return TaskClassificationOutcome(
                program=program,
                final=program,
                display_title=program.title or default_display_title(text),
                intent=program.intent or _intent_for_decision(program),
                conflicts=["model_unavailable_or_invalid"],
                classifier_version=CLASSIFICATION_VERSION,
            )

        model_decision = self._complete_entities(
            text, model_decision, envelope=envelope, platform=platform, node=node,
            service=service,
        )
        conflicts = _classification_conflicts(program, model_decision)
        final = _merge_decisions(program, model_decision)
        return TaskClassificationOutcome(
            program=program,
            model=model_decision,
            final=final,
            display_title=model_title or model_decision.title or program.title or default_display_title(text),
            intent=model_intent or model_decision.intent or program.intent or _intent_for_decision(final),
            conflicts=conflicts,
            needs_review=bool(conflicts),
            classifier_version=CLASSIFICATION_VERSION,
        )

    def _classify_structured(
        self,
        text: str,
        envelope: Optional[AlertEnvelope],
        *,
        platform: Optional[str],
        node: Optional[str],
        service: Optional[str],
    ) -> TaskTypeDecision:
        effective_platform = platform or (envelope.platform if envelope else None)
        effective_node = node or (envelope.node if envelope else None)
        effective_service = service or (envelope.service if envelope else None)
        alertname = envelope.alertname if envelope else None
        if effective_platform is None:
            match = _generic_platform_search(text)
            effective_platform = match.group(1).lower() if match else None
        if effective_node is None and effective_platform is not None:
            match = _GENERIC_NODE.search(text)
            effective_node = match.group(0).lower() if match else None

        # 告警信封(任一已配置告警源)带告警名:有精确路由即告警任务,无路由走 default
        if envelope and alertname:
            if effective_platform and self._route_exists(alertname, effective_platform):
                return self._decision(
                    "alert", 1.0, "structured",
                    [f"{envelope.source}_alert", "exact_alertname"],
                    platform=effective_platform,
                    service=effective_service,
                    node=effective_node,
                    alertname=alertname,
                )
            if effective_platform and self.routing_path is not None:
                return self._decision(
                    "default", 0.45, "rule", ["alert_route_missing"],
                    platform=effective_platform,
                    service=effective_service,
                    node=effective_node,
                    alertname=alertname,
                    needs_clarification=["alertname"],
                )
            return self._decision(
                "alert", 0.9, "structured", ["alert_context", "alertname_platform"],
                platform=effective_platform,
                service=effective_service,
                node=effective_node,
                alertname=alertname,
            )

        if _ARTIFACT_INTENT.search(text):
            artifact_kind = infer_artifact_kind(text)
            if artifact_kind:
                return self._decision(
                    "artifact", 0.94, "rule", ["artifact_intent", "artifact_kind"],
                    platform=effective_platform,
                    service=effective_service,
                    node=effective_node,
                    artifact_kind=artifact_kind,
                )
            return self._decision(
                "artifact", 0.82, "rule", ["artifact_intent"],
                platform=effective_platform,
                service=effective_service,
                node=effective_node,
            )

        if _INVESTIGATION_INTENT.search(text):
            return self._decision(
                "investigation", 0.9, "rule", ["investigation_intent"],
                platform=effective_platform,
                service=effective_service,
                node=effective_node,
            )

        if _QA_INTENT.search(text):
            return self._decision(
                "qa", 0.88, "rule", ["qa_intent"],
                platform=effective_platform,
                service=effective_service,
                node=effective_node,
            )

        return self._decision(
            "default", 0.35, "rule", ["no_high_confidence_signal"],
            platform=effective_platform,
            service=effective_service,
            node=effective_node,
        )

    def _complete_entities(
        self,
        text: str,
        decision: TaskTypeDecision,
        *,
        envelope: Optional[AlertEnvelope],
        platform: Optional[str],
        node: Optional[str],
        service: Optional[str],
    ) -> TaskTypeDecision:
        effective_platform = decision.platform or platform or (
            envelope.platform if envelope else None
        )
        effective_node = decision.node or node or (
            envelope.node if envelope else None
        )
        if effective_node is None:
            match = _GENERIC_NODE.search(text)
            effective_node = match.group(0).lower() if match else None
        inferred_service, inferred_component = extract_service_component(text)
        named_services, named_components = extract_service_components(text)
        effective_service = decision.service or (
            envelope.service if envelope else None
        ) or inferred_service
        effective_component = decision.component or inferred_component
        if len(named_services) > 1 and not service and not (envelope and envelope.service):
            # A model must not turn an explicitly multi-service request into
            # one service just to fit the singular routing hint.
            effective_service = None
            effective_component = None
        elif len(named_components) > 1:
            effective_component = None
        if effective_node and effective_platform:
            resolved = self._resolve_node(effective_platform, effective_node)
            if resolved:
                effective_node = resolved
        scope = decision.scope
        if scope is None and decision.task_type in {"qa", "investigation", "alert"}:
            scope = (
                "cluster" if _CLUSTER_SCOPE.search(text)
                else "node" if effective_node else None
            )
        return decision.model_copy(update={
            "platform": effective_platform,
            "service": effective_service,
            "component": effective_component,
            "node": effective_node,
            "scope": scope,
            "title": decision.title or default_display_title(text),
            "intent": decision.intent or _intent_for_decision(decision),
        })


    def _classify_model_once(
        self,
        text: str,
        *,
        base: TaskTypeDecision,
        alert_context: Optional[AlertEnvelope],
        platform: Optional[str],
        node: Optional[str],
        service: Optional[str],
        model: ModelClient,
    ) -> tuple[Optional[TaskTypeDecision], Optional[str], Optional[str]]:
        prompt = (
            "你是 Ops 任务预分类器。只返回一个严格 JSON object，禁止 Markdown、解释文本和工具调用。"
            "字段必须包含 title(不超过40个中文字符)、task_type、intent、confidence、"
            "reason_codes、platform、service、component、node、alertname、artifact_kind、scope。"
            "task_type 只能是 qa、investigation、alert、artifact、default。"
            "intent 用简短英文 snake_case，例如 realtime_status_query、root_cause_investigation、"
            "alert_triage、artifact_generation、general_request。"
            "如果只是查询当前 CPU/load/内存/磁盘/服务状态，即使需要连接服务器，也必须是 qa；"
            "只有询问原因、故障定位、异常分析、趋势或跨节点根因时才是 investigation。"
            "从用户描述中的明确名称或常见别名提取 service 和 component，"
            "例如 DataNode 对应 component=datanode、service=hdfs。"
            "不能仅凭服务名称猜测所属平台。"
            "不要猜测用户没有提供的告警名、平台或节点；字段缺失返回 null。"
        )
        payload = {
            "message": text,
            "program_preclassification": base.model_dump(mode="json"),
            "alert_context": alert_context.model_dump(mode="json") if alert_context else None,
            "hints": {"platform": platform, "node": node, "service": service},
        }
        try:
            reply = model.complete([
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ], [])
            value = _parse_json_object(reply.content or "")
            value.setdefault("source", "model")
            value.setdefault("classifier_version", CLASSIFICATION_VERSION)
            value.setdefault("reason_codes", ["model_classification"])
            value.setdefault("confidence", base.confidence)
            value = TaskTypeDecision.model_validate(value)
            if value.task_type == "alert" and value.alertname and value.platform:
                if self.routing_path is not None and not self._route_exists(value.alertname, value.platform):
                    return None, None, None
            return value, value.title, value.intent
        except Exception:
            return None, None, None

    def _classify_with_model(
        self,
        text: str,
        *,
        base: TaskTypeDecision,
        alert_context: Optional[AlertEnvelope],
        platform: Optional[str],
        node: Optional[str],
        service: Optional[str],
    ) -> TaskTypeDecision:
        if self.model is None:
            return base
        prompt = (
            "你是任务类型分类器，只能返回严格 JSON，不得调用工具。"
            "task_type 只能是 qa、investigation、alert、artifact、default。"
            "只能根据用户输入和结构化告警字段分类，不能猜测缺失的告警名。"
            "JSON 字段：task_type, confidence, reason_codes, platform, service, "
            "component, node, alertname, artifact_kind, scope。"
        )
        payload = {
            "message": text,
            "alert_context": alert_context.model_dump(mode="json") if alert_context else None,
            "hints": {"platform": platform, "node": node, "service": service},
        }
        try:
            reply = self.model.complete([
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ], [])
            value = _parse_json_object(reply.content or "")
            value.setdefault("source", "model")
            value.setdefault("classifier_version", CLASSIFIER_VERSION)
            value.setdefault("reason_codes", ["model_fallback"])
            value.setdefault("confidence", base.confidence)
            value = TaskTypeDecision.model_validate(value)
            if value.task_type == "alert" and value.alertname and value.platform:
                if self.routing_path is not None and not self._route_exists(value.alertname, value.platform):
                    return base
            return value.model_copy(update={
                "source": "model",
                "classifier_version": CLASSIFIER_VERSION,
            })
        except Exception:
            return base

    def _route_exists(self, alertname: str, platform: str) -> bool:
        if self.route_lookup is not None:
            try:
                return self.route_lookup(alertname, platform) is not None
            except Exception:  # noqa: BLE001
                return False
        if self.routing_path is None or not self.routing_path.is_file():
            return False
        try:
            return match_alert(load_routing(self.routing_path), alertname, platform) is not None
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def _resolve_node(
        self,
        platform: Optional[str],
        host_raw: str,
    ) -> Optional[str]:
        if not platform:
            return None
        if self.node_resolver is not None:
            return self.node_resolver(platform, host_raw)
        if self.repo_root is None:
            return None
        # 兜底与任务路由同口径:经资源清单把节点键/IP/别名/FQDN 归一到登记节点;拓扑不可读即不归一
        try:
            return resolve_registered_node(self.repo_root, platform, host_raw)
        except (OSError, ValueError, KeyError, TypeError):
            return None

    @staticmethod
    def _decision(task_type, confidence, source, reason_codes, **fields):
        return TaskTypeDecision(
            task_type=task_type,
            confidence=confidence,
            source=source,
            reason_codes=reason_codes,
            classifier_version=CLASSIFIER_VERSION,
            **fields,
        )


def parse_alert_text(text: str) -> Optional[AlertEnvelope]:
    """Extract a safe alert envelope from copied Prometheus-style text."""
    if not _ALERT_MARKER.search(text):
        return None
    notification = _parse_notification(text)
    if notification is not None:
        return notification
    alertname = _first_group(_ALERT_LABEL, text)
    platform = _first_group(_platform_label_re(active_env_pack().cluster_keys()), text)
    if platform is None:
        generic = _generic_platform_search(text)
        platform = generic.group(1).lower() if generic else None
    if platform is not None:
        platform = platform.lower()
    host_raw = _first_group(_HOST_LABEL, text)
    service = _first_group(_SERVICE_LABEL, text)
    annotations: Dict[str, str] = {}
    labels: Dict[str, str] = {}
    for line in text.splitlines():
        match = _FIELD.match(line)
        if not match:
            continue
        key = match.group("key").strip()
        value = match.group("value").strip()
        normalized = re.sub(r"[^A-Za-z0-9]+", "", key).lower()
        if normalized in {
            "processid", "taskid", "execdate", "scheduledate", "triggerdate",
            "告警描述",
        }:
            annotations[key] = value[:512]
        elif key in {"severity", "service", "job", "instance", "host", "platform", "cluster"}:
            labels[key] = value[:512]
    if alertname is None and not annotations and not labels:
        return None
    return AlertEnvelope(
        source="prometheus",
        alertname=alertname or "",
        labels=labels,
        annotations=annotations,
        platform=platform if platform in active_env_pack().cluster_keys() else None,
        host_raw=host_raw,
        service=service,
        detail=text[:8000],
    )


def _parse_notification(text: str) -> Optional[AlertEnvelope]:
    """Parse a pasted 中台 alert notification; resource-risk messages are not alerts."""
    title = severity = None
    fields: Dict[str, str] = {}
    for raw_line in text.splitlines():
        line = _MARKDOWN_NOISE.sub("", raw_line).lstrip("#> \t").strip()
        if not line:
            continue
        if title is None:
            match = _NOTIFY_TITLE.match(line)
            if match:
                severity, title = match.group(1).lower(), match.group(2).strip()
                continue
        match = _NOTIFY_FIELD.match(line)
        if match:
            fields.setdefault(match.group(1), match.group(2).strip())
    kind = fields.get("类型")
    if title is None or kind not in _ALERT_NOTIFY_KINDS:
        return None
    suffix = _ALERT_NOTIFY_KINDS[kind]
    alertname = title[:-len(suffix)] if suffix and title.endswith(suffix) else title

    def present(name: str) -> Optional[str]:
        value = fields.get(name) or ""
        return value[:256] if value not in {"", "-"} else None

    platform = (present("平台") or "").lower()
    description = present("说明")
    return AlertEnvelope(
        source="prometheus",
        alertname=alertname[:512],
        annotations={"description": description[:512]} if description else {},
        platform=platform if platform in active_env_pack().cluster_keys() else None,
        host_raw=present("主机"),
        service=present("服务"),
        severity=severity,
        active_at=present("首次发现"),
        detail=text[:8000],
    )


def infer_artifact_kind(text: str) -> Optional[ArtifactKind]:
    for kind, pattern in _ARTIFACT_PATTERNS:
        if pattern.search(text):
            return kind  # type: ignore[return-value]
    return None


def _first_group(pattern: re.Pattern, text: str) -> Optional[str]:
    match = pattern.search(text)
    if not match:
        return None
    value = match.group(1).strip().strip("[]【】")
    return value or None


def _parse_json_object(value: str) -> Dict[str, Any]:
    text = value.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Some OpenAI-compatible gateways prepend a short sentence despite the
        # no-tool JSON contract. Extract one bounded object so a harmless
        # wrapper does not turn a valid classification into default routing.
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(text[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("分类模型必须返回 JSON object")
    return parsed


def default_display_title(message: str) -> str:
    """Deterministic fallback used when the model is unavailable."""
    first_line = next(
        (
            line.strip()
            for line in str(message or "").splitlines()
            if line.strip() and not re.fullmatch(r"=+", line.strip())
        ),
        "未命名任务",
    )
    if len(first_line) <= 40:
        return first_line
    return first_line[:38].rstrip() + "…"


def _intent_for_decision(decision: TaskTypeDecision) -> str:
    if decision.task_type == "qa":
        return "realtime_status_query" if any(
            code in {"qa_intent", "model_classification"}
            for code in decision.reason_codes
        ) else "factual_question"
    return {
        "investigation": "root_cause_investigation",
        "alert": "alert_triage",
        "artifact": "artifact_generation",
        "default": "general_request",
    }[decision.task_type]


def _classification_conflicts(
    program: TaskTypeDecision,
    model: TaskTypeDecision,
) -> list[str]:
    conflicts: list[str] = []
    if program.task_type != model.task_type:
        conflicts.append(f"task_type:{program.task_type}!={model.task_type}")
    for field in ("platform", "node", "scope", "alertname", "artifact_kind"):
        left, right = getattr(program, field), getattr(model, field)
        if left and right and str(left).lower() != str(right).lower():
            conflicts.append(f"{field}:{left}!={right}")
    return conflicts


def _merge_decisions(
    program: TaskTypeDecision,
    model: TaskTypeDecision,
) -> TaskTypeDecision:
    """Use model intent while retaining program-known routing entities."""
    updates = {
        field: getattr(program, field)
        for field in ("platform", "service", "component", "node", "alertname", "artifact_kind", "scope")
        if getattr(program, field) is not None
    }
    return model.model_copy(update={
        **updates,
        "source": "model",
        "classifier_version": CLASSIFICATION_VERSION,
    })
