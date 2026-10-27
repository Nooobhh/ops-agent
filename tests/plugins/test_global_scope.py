"""全局插件(不绑集群、按标签区分集群)与后台采集声明。"""
import pytest

from lib.envpack import EnvPackError, parse_env_pack
from lib.plugins import PluginCallError, PluginRegistry, PluginService
from lib.plugins.catalog import SCOPE_FILTER_MISSING, blocked_reason, model_description, normalize_tool
from lib.plugins.exposure import apply_scope
from lib.plugins.registry import Plugin

from .test_plugins import mcp_server

PACK = """\
version: 1
id: lab
display_name: Lab
timezone: UTC
clusters:
  - id: alpha
    legacy_key: a
    roles: [managed]
  - id: beta
    legacy_key: b
    roles: [managed]
plugins:
  - id: metrics
    url: http://127.0.0.1:9100/mcp
    scope:
      label: platform
      clusters: {a: alpha-label, b: beta-label}
      tools: [query]
    feeds:
      resources: {snapshot: snap, targets: targets, query: query}
"""

FILTERS = {"anyOf": [{"type": "object"}, {"type": "null"}]}
QUERY = {"name": "query", "description": "具名查询", "annotations": {"readOnlyHint": True},
         "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "filters": FILTERS}}}
OVERVIEW = {"name": "overview", "description": "概览", "annotations": {"readOnlyHint": True},
            "inputSchema": {"type": "object", "properties": {}}}


def _pack(tmp_path, body=PACK):
    root = tmp_path / "pack"
    root.mkdir(parents=True, exist_ok=True)
    (root / "env.yaml").write_text(body, encoding="utf-8")
    return parse_env_pack(root)


def test_scope_and_feeds_are_parsed(tmp_path):
    decl = _pack(tmp_path).plugins[0]
    assert decl.cluster is None
    assert decl.scope.argument == "filters" and decl.scope.cluster_of("beta-label") == "b"
    assert decl.scope.cluster_of("gamma") is None
    assert decl.feeds == {"resources": {"snapshot": "snap", "targets": "targets", "query": "query"}}


@pytest.mark.parametrize("patch, message", [
    (("    scope:", "    cluster: a\n    scope:"), "二选一"),
    (("{a: alpha-label, b: beta-label}", "{a: alpha-label, c: x}"), "未声明的集群"),
    (("{a: alpha-label, b: beta-label}", "{a: same, b: same}"), "不能重复"),
    (("{a: alpha-label, b: beta-label}", '{a: "x y"}'), "标签值不合法"),
    (("tools: [query]", "tools: []"), "scope.tools"),
    (("resources: {snapshot: snap, targets: targets, query: query}", "resources: {snapshot: snap}"), "须且只须"),
    (("resources:", "logs:"), "feeds 只能是"),
])
def test_invalid_scope_or_feeds_rejected(tmp_path, patch, message):
    with pytest.raises(EnvPackError, match=message):
        _pack(tmp_path, PACK.replace(*patch))


def _plugin(tmp_path):
    decl = _pack(tmp_path).plugins[0]
    return Plugin(id=decl.id, title="指标", url=decl.url, source="official", cluster=None, auth="none",
                  header_name="", timeout_seconds=10, enabled=True, scope=decl.scope, feeds=decl.feeds)


def test_scoped_tools_need_filter_argument_and_are_annotated(tmp_path):
    plugin = _plugin(tmp_path)
    query, overview = normalize_tool(QUERY, True), normalize_tool(OVERVIEW, True)
    assert blocked_reason(plugin, query) is None
    assert blocked_reason(plugin, overview) is None, "环境级工具不要求过滤参数"
    no_filter = normalize_tool({**QUERY, "inputSchema": {"type": "object", "properties": {}}}, True)
    assert blocked_reason(plugin, no_filter) == SCOPE_FILTER_MISSING
    assert "【集群过滤】" in model_description(plugin, query)
    assert "【环境级】" in model_description(plugin, overview)


def _spec(role="cluster"):
    return {"plugin": "metrics", "scope": {"role": role, "argument": "filters", "label": "platform",
                                           "clusters": {"a": "alpha-label", "b": "beta-label"}}}


def test_apply_scope_fills_accepts_and_rejects():
    assert apply_scope(_spec(), {"name": "up"}, "a") == (
        {"name": "up", "filters": {"platform": "alpha-label"}}, None)
    assert apply_scope(_spec(), {"filters": {"node": "n1"}}, "b")[0]["filters"] == {
        "node": "n1", "platform": "beta-label"}
    assert apply_scope(_spec(), {"filters": {"platform": ["alpha-label"]}}, "a")[1] is None
    for given in ("beta-label", ["alpha-label", "beta-label"]):
        _, problem = apply_scope(_spec(), {"filters": {"platform": given}}, "a")
        assert "一个任务只对应一个集群" in problem
    assert "不覆盖集群" in apply_scope(_spec(), {}, "c")[1]
    assert "须为对象" in apply_scope(_spec(), {"filters": "platform=a"}, "a")[1]
    environment = {"filters": {"platform": "beta-label"}}
    assert apply_scope(_spec("environment"), environment, "a") == (environment, None)
    assert apply_scope({"plugin": "x"}, environment, "a") == (environment, None)


def test_real_pack_declares_one_global_prometheus_feed(tmp_path):
    registry = PluginRegistry(tmp_path)
    for kind in ("alerts", "resources"):
        plugin = registry.feed(kind)
        assert plugin.scope is not None and plugin.cluster is None and plugin.auth == "none"
        assert not plugin.enabled, "后台采集不依赖插件页开关;官方插件仍默认停用"


def test_snapshot_freezes_scope_and_feed_call_reports_upstream_failure(tmp_path):
    envelope = {"schema_version": 1, "ok": False, "data": None, "warnings": [],
                "error": {"code": "UPSTREAM_UNAVAILABLE", "message": "连不上 8428",
                          "details": {"source": "victoriametrics"}}}
    import json
    tools = [{"name": "list_targets", "description": "目标", "annotations": {"readOnlyHint": True},
              "inputSchema": QUERY["inputSchema"]},
             {"name": "get_overview", "description": "概览", "annotations": {"readOnlyHint": True},
              "inputSchema": {"type": "object", "properties": {}}}]
    transport, _ = mcp_server(tools, {"content": [{"type": "text", "text": json.dumps(envelope)}]})
    service = PluginService(tmp_path, tmp_path / "state", transport=transport)
    plugin = service.registry.update(service.registry.feed("alerts").id, enabled=True)
    assert service.connection_test(plugin)["ok"]
    snapshot = {item["tool"]: item for item in service.snapshot("alpha")["tools"]}
    assert snapshot["list_targets"]["scope"]["role"] == "cluster"
    assert snapshot["list_targets"]["scope"]["clusters"]["alpha"] == "alpha"
    assert snapshot["get_overview"]["scope"]["role"] == "environment"
    assert service.view(plugin)["scope"]["label"] == "platform"

    with pytest.raises(PluginCallError) as caught:
        service.feed_call("alerts", "history", {})
    assert caught.value.code == "UPSTREAM_UNAVAILABLE" and "victoriametrics" in caught.value.message
