import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from lib.triage_agent.model_client import ModelReply
from lib.triage_agent.model_registry import AgentTypePolicy, ModelRegistry, ModelUpsert
from lib.triage_agent.models import TaskStatus
from lib.triage_agent.task_classifier import (
    TaskClassifier, extract_service_components, parse_alert_text,
)
from lib.triage_agent.service import CreateTaskRequest, TriageTaskService
from lib.triage_agent.task_store import TaskStore
from lib.triage_agent.task_types import AlertEnvelope, TaskTypeDecision


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_parse_prometheus_style_alert_text_and_resolve_host():
    text = (
        "========告警触发========\n"
        "【告警项目】：Mtail监控-JobScheduler任务执行失败告警\n"
        "【告警集群】：Alpha\n"
        "【告警主机】：192.0.2.13\n"
        "- processId: 1034\n"
        "- taskId: 14167\n"
        "- execDate: 2026-08-31\n"
    )
    envelope = parse_alert_text(text)

    assert envelope is not None
    assert envelope.alertname == "Mtail监控-JobScheduler任务执行失败告警"
    assert envelope.platform == "alpha"
    assert envelope.host_raw == "192.0.2.13"
    assert envelope.annotations["taskId"] == "14167"

    classifier = TaskClassifier(
        route_lookup=lambda alertname, platform: {
            "doc": "alpha/jobs/diagnosis.md",
        } if alertname and platform == "alpha" else None,
        node_resolver=lambda platform, host: "node13" if host == "192.0.2.13" else None,
    )
    decision = classifier.classify(text)

    assert decision.task_type == "alert"
    assert decision.source == "structured"
    assert decision.node == "node13"


@pytest.mark.parametrize("host", ["192.0.2.13", "node3.example.internal:8042"])
def test_pasted_alert_host_resolves_to_node_with_external_data_root(tmp_path, monkeypatch, host):
    # 安装形态:数据根在仓库外,其 config/ 只放本机私有配置;主机来源仍在代码仓
    data_root = tmp_path / "data"
    monkeypatch.setenv("OPS_AGENT_DATA_ROOT", str(data_root))
    classifier = TaskClassifier(
        routing_path=data_root / "build" / "routing.json", repo_root=REPO_ROOT,
    )

    decision = classifier.classify(
        f"【WARNING】UnroutedAlert\n类型：告警触发\n平台：alpha\n主机：{host}"
    )

    assert decision.platform == "alpha"
    assert decision.node == "node3"


_OWN_NOTIFICATION = (
    "## 【WARNING】Mtail监控-JobScheduler任务执行失败告警  \n"
    "> 类型：告警触发  \n"
    "> 平台：alpha  \n"
    "> 主机：192.0.2.26  \n"
    "> 服务：jobs  \n"
    "> 当前值：**<font color=\"warning\">1</font>**  \n"
    "> 说明：JobScheduler任务执行失败  \n"
    "> 首次发现：2026-09-08 04:39:52"
)


def test_parse_own_markdown_notification():
    envelope = parse_alert_text(_OWN_NOTIFICATION)

    assert envelope is not None
    assert envelope.alertname == "Mtail监控-JobScheduler任务执行失败告警"
    assert (envelope.platform, envelope.host_raw) == ("alpha", "192.0.2.26")
    assert (envelope.service, envelope.severity) == ("jobs", "warning")
    assert envelope.annotations == {"description": "JobScheduler任务执行失败"}


def test_parse_rendered_derived_notification_recovers_the_alertname():
    text = (
        "【CRITICAL】MySQL组件监控-MySQL主从112节点通信延迟时间告警 转为常驻\n"
        "类型：告警转为常驻\n平台：unknown\n主机：192.0.2.112:9104\n服务：-"
    )
    envelope = parse_alert_text(text)

    assert envelope.alertname == "MySQL组件监控-MySQL主从112节点通信延迟时间告警"
    assert envelope.platform is None
    assert envelope.host_raw == "192.0.2.112:9104"
    assert envelope.service is None


def test_resource_risk_notification_is_not_an_alert():
    text = (
        "## 【WARNING】node1 内存使用率预测告警\n"
        "> 类型：资源预测告警\n> 平台：alpha\n> 主机：node1"
    )
    assert parse_alert_text(text) is None


def test_pasted_alert_host_sets_platform_before_classification(tmp_path):
    routing = tmp_path / "routing.json"
    routing.write_text(json.dumps({"alerts": {"alpha": {"RoutedAlert": {
        "doc": "alpha/x.md", "platform": "alpha", "service": "hdfs",
        "playbook": None, "playbooks": [], "diagnostic_paths": [],
    }}}, "docs": []}), encoding="utf-8")
    service = TriageTaskService(
        TaskStore(tmp_path / "tasks"),
        classifier=TaskClassifier(routing_path=routing),
    )
    targets = _registered_targets()

    def paste(alertname):
        return service.create_manual(CreateTaskRequest(message=(
            f"【WARNING】{alertname}\n类型：告警触发\n平台：unknown\n主机：192.0.2.13"
        )), **targets).task

    routed, unrouted = paste("RoutedAlert"), paste("UnroutedAlert")
    assert (routed.task_type, routed.platform, routed.node) == ("alert", "alpha", "node3")
    # 无路由告警不进 AlertAgent:平台已知时与告警页入口同样交给 DefaultAgent
    assert (unrouted.task_type, unrouted.platform, unrouted.node) == ("default", "alpha", "node3")


@pytest.mark.parametrize(
    ("message", "task_type"),
    [
        ("DataNode 使用哪些端口？", "qa"),
        ("排查 DataNode 心跳断开的原因", "investigation"),
        ("生成一个 DataNode 监控脚本", "artifact"),
        ("帮我处理一下这个事情", "default"),
    ],
)
def test_classifier_routes_common_intents(message, task_type):
    decision = TaskClassifier().classify(message, platform="alpha")
    assert decision.task_type == task_type


def test_current_node_resource_status_is_qa_with_entities():
    decision = TaskClassifier().classify("alpha node10现在的cpu占用和load是多少")

    assert decision.task_type == "qa"
    assert decision.platform == "alpha"
    assert decision.node == "node10"
    assert decision.scope == "node"


def test_unverified_alert_does_not_enter_alert_agent(tmp_path):
    routing = tmp_path / "routing.json"
    routing.write_text(json.dumps({"alerts": {}, "docs": []}), encoding="utf-8")
    decision = TaskClassifier(routing_path=routing).classify(
        "告警名：UnknownAlert\n告警集群：Alpha",
    )

    assert decision.task_type == "default"
    assert "alert_route_missing" in decision.reason_codes
    assert decision.needs_clarification == ["alertname"]


def test_model_fallback_is_no_tool_and_validates_enum():
    calls = []

    class FakeModel:
        def complete(self, messages, tools):
            calls.append((messages, tools))
            return ModelReply(
                content=json.dumps({
                    "task_type": "qa",
                    "confidence": 0.91,
                    "reason_codes": ["model_intent"],
                }),
                tool_calls=[],
                assistant_message={"role": "assistant", "content": "{}"},
            )

    classifier = TaskClassifier(model=FakeModel())
    decision = classifier.classify("帮我看看这个东西")

    assert decision.task_type == "qa"
    assert decision.source == "model"
    assert calls and calls[0][1] == []


def test_combined_classifier_persists_model_title_and_audit_fields(tmp_path):
    calls = []

    class FakeModel:
        def complete(self, messages, tools):
            calls.append((messages, tools))
            return ModelReply(
                content=json.dumps({
                    "title": "查询 node10 CPU 与 Load",
                    "task_type": "qa",
                    "intent": "realtime_status_query",
                    "confidence": 0.97,
                    "reason_codes": ["current_status"],
                    "platform": "alpha",
                    "node": "node10",
                    "scope": "node",
                }),
                tool_calls=[],
                assistant_message={"role": "assistant", "content": "{}"},
            )

    service = TriageTaskService(
        TaskStore(tmp_path / "tasks"),
        classifier=TaskClassifier(model=FakeModel()),
    )
    detail = service.create_manual(CreateTaskRequest(
        message="alpha node10现在的cpu占用和load是多少",
    ))

    assert len(calls) == 1 and calls[0][1] == []
    assert detail.task.task_type == "qa"
    assert detail.task.display_title == "查询 node10 CPU 与 Load"
    assert detail.task.intent == "realtime_status_query"
    assert detail.task.classification_audit["model"]["task_type"] == "qa"


def test_missing_platform_asks_before_investigation_and_resumes_with_service(tmp_path):
    class InvalidModel:
        def complete(self, messages, tools):
            raise ValueError("invalid classification response")

    service = TriageTaskService(
        TaskStore(tmp_path / "tasks"),
        classifier=TaskClassifier(model=InvalidModel()),
    )
    detail = service.create_manual(CreateTaskRequest(
        message="排查jobs 1117流程点击运行后没有反应，查看后台接口日志",
    ), known_nodes=lambda platform: ["node1", "node3"])

    assert detail.task.status is TaskStatus.WAITING_INPUT
    assert detail.task.service == "jobs"
    assert detail.task.component is None
    assert detail.task.scope == "cluster"
    assert detail.task.context_missing == ["platform"]
    assert detail.task.classification_audit["model"] is None
    question = next(
        event for event in detail.events
        if event.type == "task_context_clarification_requested"
    )
    assert question.payload["kind"] == "task_context"
    assert "Alpha 集群" in question.payload["options"]

    resumed = service.add_message(
        detail.task.task_id, "Alpha 集群",
        known_nodes=lambda platform: ["node1", "node3"],
    )
    assert resumed.task.status is TaskStatus.QUEUED
    assert resumed.task.platform == "alpha"
    assert resumed.task.node == "node1"
    assert resumed.task.scope == "cluster"
    assert resumed.task.service == "jobs"
    assert resumed.task.context_missing == []


def test_model_service_based_platform_guess_still_requires_confirmation(tmp_path):
    class GuessingModel:
        def complete(self, messages, tools):
            return ModelReply(
                content=json.dumps({
                    "task_type": "investigation", "confidence": 0.9,
                    "reason_codes": ["investigation_intent"],
                    "platform": "alpha", "service": "jobs",
                }),
                tool_calls=[],
                assistant_message={"role": "assistant", "content": "{}"},
            )

    service = TriageTaskService(
        TaskStore(tmp_path / "tasks"),
        classifier=TaskClassifier(model=GuessingModel()),
    )
    detail = service.create_manual(CreateTaskRequest(
        message="排查 JobScheduler 流程点击后没有反应",
    ))
    assert detail.task.status is TaskStatus.WAITING_INPUT
    assert detail.task.platform is None
    assert detail.task.service == "jobs"
    assert detail.task.context_missing == ["platform"]


def test_component_and_node_are_extracted_before_platform_question(tmp_path):
    service = TriageTaskService(TaskStore(tmp_path / "tasks"))
    detail = service.create_manual(CreateTaskRequest(
        message="排查 HDFS DataNode node12 异常",
    ))

    assert detail.task.status is TaskStatus.WAITING_INPUT
    assert detail.task.service == "hdfs"
    assert detail.task.component == "datanode"
    assert detail.task.node == "node12"
    assert detail.task.scope == "node"
    assert detail.task.context_missing == ["platform"]


def _registered_targets():
    """Fake topology: node13 exists on both platforms; identities are key/IP/alias."""
    nodes = {
        "alpha": {"node3": ("192.0.2.13",), "node13": ("192.0.2.23",)},
        "beta": {"node13": ("192.0.2.63",)},
        "shared": {"vm-web01": ("198.51.100.9", "ragflow-host")},
    }

    def resolve(platform, value):
        value = value.lower()
        short = value.split(".", 1)[0]
        for name, identities in nodes.get(platform, {}).items():
            if value in (name, *identities) or (not value[0].isdigit() and short == name):
                return name
        return None

    def infer(value):
        hits = [platform for platform in nodes if resolve(platform, value)]
        return hits[0] if len(hits) == 1 else None

    return {
        "resolve_node": resolve,
        "infer_platform": infer,
        "known_nodes": lambda platform: sorted(nodes[platform]),
    }


def test_initial_message_ip_names_the_node(tmp_path):
    service = TriageTaskService(TaskStore(tmp_path / "tasks"))
    detail = service.create_manual(CreateTaskRequest(
        message="排查 192.0.2.13 上 HDFS DataNode 异常",
    ), **_registered_targets())

    assert detail.task.status is TaskStatus.QUEUED
    assert (detail.task.platform, detail.task.node) == ("alpha", "node3")
    assert detail.task.scope == "node"
    assert detail.task.context_missing == []


@pytest.mark.parametrize(("reply", "platform", "node"), [
    ("192.0.2.13", "alpha", "node3"),
    ("节点 node13.example.internal，Beta 集群", "beta", "node13"),
    ("在 ragflow-host 上", "shared", "vm-web01"),
])
def test_context_reply_resolves_ip_fqdn_and_alias(tmp_path, reply, platform, node):
    targets = _registered_targets()
    service = TriageTaskService(TaskStore(tmp_path / "tasks"))
    detail = service.create_manual(CreateTaskRequest(
        message="排查 HDFS DataNode 进程异常",
    ), **targets)
    assert detail.task.context_missing == ["platform"]

    resumed = service.add_message(detail.task.task_id, reply, **targets)

    assert resumed.task.status is TaskStatus.QUEUED
    assert (resumed.task.platform, resumed.task.node) == (platform, node)
    assert resumed.task.scope == "node"
    assert resumed.task.context_missing == []


def test_context_reply_node_overrides_cluster_entry_node(tmp_path):
    targets = _registered_targets()
    service = TriageTaskService(TaskStore(tmp_path / "tasks"))
    detail = service.create_manual(CreateTaskRequest(
        message="排查这个程序的后台接口错误", platform="alpha",
    ), **targets)
    assert (detail.task.scope, detail.task.node) == ("cluster", "node13")
    assert detail.task.context_missing == ["service"]

    resumed = service.add_message(
        detail.task.task_id, "服务=customapi，在 192.0.2.13 上", **targets,
    )

    assert resumed.task.status is TaskStatus.QUEUED
    assert (resumed.task.scope, resumed.task.node) == ("node", "node3")
    assert resumed.task.service == "customapi"


def test_unknown_named_service_asks_for_service_after_platform(tmp_path):
    service = TriageTaskService(TaskStore(tmp_path / "tasks"))
    detail = service.create_manual(CreateTaskRequest(
        message="排查这个程序的后台接口错误",
    ), known_nodes=lambda platform: ["node1"])

    assert detail.task.context_missing == ["platform", "service"]
    platform_answer = service.add_message(
        detail.task.task_id, "Alpha 集群",
        known_nodes=lambda platform: ["node1"],
    )
    assert platform_answer.task.status is TaskStatus.WAITING_INPUT
    assert platform_answer.task.context_missing == ["service"]
    resumed = service.add_message(
        detail.task.task_id, "服务=customapi",
        known_nodes=lambda platform: ["node1"],
    )
    assert resumed.task.status is TaskStatus.QUEUED
    assert resumed.task.service == "customapi"


def test_static_qa_does_not_require_platform(tmp_path):
    service = TriageTaskService(TaskStore(tmp_path / "tasks"))
    detail = service.create_manual(CreateTaskRequest(message="什么是 HDFS"))
    assert detail.task.status is TaskStatus.QUEUED
    assert detail.task.context_missing == []


def test_mysql_target_is_accepted_during_creation(tmp_path):
    service = TriageTaskService(TaskStore(tmp_path / "tasks"))
    detail = service.create_manual(CreateTaskRequest(
        message="排查 MySQL 连接异常", platform="db", node="db1",
    ))
    assert detail.task.platform == "db"
    assert detail.task.node == "db1"
    assert detail.task.status is TaskStatus.QUEUED


def test_platform_wide_investigation_needs_no_component(tmp_path):
    service = TriageTaskService(TaskStore(tmp_path / "tasks"))
    detail = service.create_manual(CreateTaskRequest(
        message="排查 Alpha 平台所有服务的整体运行情况",
    ), known_nodes=lambda platform: ["node1", "node2"])

    assert detail.task.status is TaskStatus.QUEUED
    assert detail.task.platform == "alpha"
    assert detail.task.scope == "cluster"
    assert detail.task.node == "node1"
    assert detail.task.service is None
    assert detail.task.component is None
    assert detail.task.context_missing == []


def test_multiple_components_remain_multi_target(tmp_path):
    message = "排查 Alpha 集群 HDFS DataNode 和 YARN NodeManager 组件异常"
    assert extract_service_components(message) == (
        ("hdfs", "yarn"), ("datanode", "nodemanager"),
    )
    service = TriageTaskService(TaskStore(tmp_path / "tasks"))
    detail = service.create_manual(CreateTaskRequest(message=message),
                                   known_nodes=lambda platform: ["node1"])

    assert detail.task.status is TaskStatus.QUEUED
    assert detail.task.scope == "cluster"
    assert detail.task.service is None
    assert detail.task.component is None
    assert detail.task.context_missing == []


def test_multi_component_answer_is_kept_for_context_retrieval(tmp_path):
    service = TriageTaskService(TaskStore(tmp_path / "tasks"))
    detail = service.create_manual(CreateTaskRequest(
        message="排查这个程序的后台接口错误", platform="alpha",
    ), known_nodes=lambda platform: ["node1"])
    assert detail.task.context_missing == ["service"]

    resumed = service.add_message(
        detail.task.task_id, "涉及 HDFS DataNode 和 YARN NodeManager",
        known_nodes=lambda platform: ["node1"],
    )
    assert resumed.task.status is TaskStatus.QUEUED
    assert resumed.task.context_missing == []
    assert resumed.task.service is None
    assert resumed.task.component is None
    assert "HDFS DataNode 和 YARN NodeManager" in resumed.task.context_query


def test_task_type_question_requires_two_to_four_options():
    with pytest.raises(ValidationError):
        TaskTypeDecision(
            task_type="default",
            confidence=0.4,
            source="rule",
            question="选择？",
            options=["只有一个"],
        )


def test_alert_envelope_drops_sensitive_metadata():
    envelope = AlertEnvelope(
        alertname="MemoryHigh",
        platform="Alpha",
        labels={"service": "hdfs", "password": "secret"},
        annotations={"description": "password=secret", "token": "secret"},
        detail="Authorization: Bearer secret",
    )

    assert "password" not in envelope.labels
    assert "token" not in envelope.annotations
    assert "secret" not in envelope.detail


def test_service_persists_type_tier_and_alert_context(tmp_path):
    classifier = TaskClassifier(
        route_lookup=lambda _alertname, _platform: {"doc": "alert.md"},
        node_resolver=lambda _platform, _host: "node13",
    )
    service = TriageTaskService(
        TaskStore(tmp_path / "tasks"),
        classifier=classifier,
    )
    detail = service.create_manual(CreateTaskRequest(
        message="告警名：TaskFailure\n告警集群：Alpha\n告警主机：192.0.2.13",
    ))

    task = detail.task
    assert task.task_type == "alert"
    assert task.task_type_source == "structured"
    assert task.harness_tier == "autonomous"
    assert task.agent_id is None  # 五类 Agent 已合并为一个运行时,不再按类型选 Agent
    assert task.platform == "alpha"
    assert task.node == "node13"
    assert task.scope == "node"
    assert task.alert_context is not None
    assert task.alert_context["alertname"] == "TaskFailure"
    assert task.alert_context["platform"] == "alpha"
    assert task.alert_context["host_raw"] == "192.0.2.13"
    assert any(event.type == "task_type_decided" for event in detail.events)
    assert not any(event.type == "agent_selected" for event in detail.events)


def test_alert_follow_up_persists_context_without_reclassifying_task(tmp_path):
    classifier = TaskClassifier(
        route_lookup=lambda _alertname, _platform: {"doc": "alert.md"},
        node_resolver=lambda _platform, _host: "node13",
    )
    store = TaskStore(tmp_path / "tasks")
    service = TriageTaskService(store, classifier=classifier)
    task = store.create_manual(
        "等待补充告警",
        platform="alpha",
        node="node13",
        task_type="alert",
        task_type_confirmed=True,
    )
    store.transition(task.task_id, "queued", "running")
    store.transition(task.task_id, "running", "waiting_input")

    detail = service.add_message(
        task.task_id,
        "告警名：TaskFailure\n告警集群：Alpha\n告警主机：192.0.2.13\n- taskId: 14167",
    )

    assert detail.task.status.value == "queued"
    assert detail.task.task_type == "alert"
    assert detail.task.alert_context is not None
    assert detail.task.alert_context["alertname"] == "TaskFailure"
    assert detail.task.alert_context["node"] == "node13"


def test_service_uses_and_snapshots_task_type_policy(tmp_path):
    identifiers = iter(["model-01", "model-02"])
    registry = ModelRegistry(
        tmp_path / "config" / "model.local.yaml",
        id_factory=lambda: next(identifiers),
    )
    request = ModelUpsert(
        title="Primary",
        base_url="https://primary.test/v1",
        api_key="secret-1",
        model="primary",
        default_reasoning_effort="low",
    )
    registry.create_model(request)
    registry.create_model(request.model_copy(update={
        "title": "Investigator",
        "api_key": "secret-2",
        "model": "investigator",
    }))
    policies = registry.effective_agent_policies()
    policies["investigation"] = AgentTypePolicy(
        model_id="model-02",
        reasoning_effort="high",
        max_tool_turns=40,
        model_timeout_seconds=180,
    )
    registry.update_agent_policies(policies)

    service = TriageTaskService(
        TaskStore(tmp_path / "tasks"),
        model_registry=registry,
    )
    detail = service.create_manual(CreateTaskRequest(
        message="排查 DataNode 心跳断开原因",
        platform="alpha",
        node="node1",
    ))

    assert detail.task.task_type == "investigation"
    assert detail.task.model_profile_id == "model-02"
    assert detail.task.agent_policy_snapshot["max_tool_turns"] == 40
    assert registry.client_material_for_task(detail.task).max_tool_turns == 40


@pytest.mark.parametrize("message,model_type,expected", [
    # 程序没判出类型:取模型判断
    ("Alpha 集群 YARN NodeManager 节点(例如 node5)swap 持续积累、容器调度变慢,请查根因和风险。",
     "investigation", "investigation"),
    # 程序判出了类型:冲突取程序判断
    ("alpha node10现在的cpu占用和load是多少", "investigation", "qa"),
])
def test_classification_conflict_takes_program_unless_program_is_default(tmp_path, message, model_type, expected):
    class FakeModel:
        def complete(self, messages, tools):
            return ModelReply(
                content=json.dumps({"title": "t", "task_type": model_type, "intent": "root_cause_investigation",
                                    "confidence": 0.9, "reason_codes": [], "platform": "alpha"}),
                tool_calls=[],
                assistant_message={"role": "assistant", "content": "{}"},
            )

    registry = ModelRegistry(tmp_path / "config" / "model.local.yaml", id_factory=lambda: "model-01")
    registry.create_model(ModelUpsert(
        title="Primary", base_url="https://primary.test/v1", api_key="secret", model="primary",
        default_reasoning_effort="low",
    ))
    service = TriageTaskService(
        TaskStore(tmp_path / "tasks"), model_registry=registry, classifier=TaskClassifier(model=FakeModel()),
    )
    detail = service.create_manual(CreateTaskRequest(message=message, platform="alpha", node="node5"))

    assert detail.task.task_type == expected and detail.task.task_type_confirmed
    assert detail.task.harness_tier == "autonomous"
    assert not any(event.type == "task_type_clarification_requested" for event in detail.events)
    assert detail.task.classification_audit["model"]["task_type"] == model_type


def test_demo_alert_from_alert_page_is_classified_by_exact_route(tmp_path):
    routing = tmp_path / "routing.json"
    routing.write_text(json.dumps({"alerts": {"alpha": {"datanode_health_summary": {
        "doc": "alpha/hdfs/datanode.md", "playbook": "playbooks/alpha/check.sh", "platform": "alpha", "service": "hdfs",
    }}}, "docs": []}), encoding="utf-8")
    classifier = TaskClassifier(routing_path=routing)
    routed = {"source": "demo", "alertname": "datanode_health_summary", "platform": "alpha",
              "host_raw": "node3", "node": "node3"}
    decision = classifier.classify("排查告警：datanode_health_summary", alert_context=routed)
    assert decision.task_type == "alert" and "demo_alert" in decision.reason_codes
    unrouted = {**routed, "alertname": "SPARK2_THRIFTSERVER_PROCESS"}
    decision = classifier.classify("排查告警：SPARK2_THRIFTSERVER_PROCESS", alert_context=unrouted)
    assert decision.task_type == "default" and "alert_route_missing" in decision.reason_codes
