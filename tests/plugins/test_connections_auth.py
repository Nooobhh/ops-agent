"""连接列表认证:插件页逐条编辑连接(中台持有),序列化进请求头传给插件。"""
import base64
import json

import pytest
from fastapi.testclient import TestClient

from lib.credentials import CredentialStoreError
from lib.envpack import EnvPackError, check_plugin_endpoint, parse_env_pack
from lib.plugins import PluginService
from web.server.app import create_app

from .test_plugins import mcp_server

CONN = {"name": "alpha-node1", "host": "192.0.2.11", "port": 3306, "user": "reader", "password": "s3cret",
        "database": "demo_db", "labels": {"platform": "alpha"}}


def _decode(value):
    return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))


def test_connections_are_write_only_and_keep_password_when_left_blank(tmp_path):
    service = PluginService(tmp_path, tmp_path / "state")
    store = service.credentials
    store.save_plugin_connections("mysql", [CONN])
    view = store.plugin_connections_view("mysql")
    assert view == [{"name": "alpha-node1", "host": "192.0.2.11", "port": 3306, "user_hint": "re***r",
                     "database": "demo_db", "labels": {"platform": "alpha"}, "password_set": True}]
    assert "s3cret" not in json.dumps(view)

    store.save_plugin_connections("mysql", [{**CONN, "user": None, "password": None, "port": 3307}])
    saved = store.get_plugin_secret("mysql", "connections")["connections"][0]
    assert (saved["user"], saved["password"], saved["port"]) == ("reader", "s3cret", 3307)
    with pytest.raises(CredentialStoreError, match="需要填写密码"):
        store.save_plugin_connections("mysql", [{**CONN, "name": "beta-node1", "password": None}])


@pytest.mark.parametrize("patch, message", [
    ({"name": "HD_1"}, "连接名"),
    ({"labels": {}}, "所属集群"),
    ({"labels": {"platform": "h d"}}, "所属集群"),
    ({"database": "priest db"}, "库名"),
    ({"port": 70000}, "端口"),
    ({"user": ""}, "需要填写账号"),
    ({"user": "a\nb"}, "账号或密码"),
])
def test_invalid_connections_rejected(tmp_path, patch, message):
    store = PluginService(tmp_path, tmp_path / "state").credentials
    with pytest.raises(CredentialStoreError, match=message):
        store.save_plugin_connections("mysql", [{**CONN, **patch}])


def test_duplicate_names_and_too_many_connections_rejected(tmp_path):
    store = PluginService(tmp_path, tmp_path / "state").credentials
    with pytest.raises(CredentialStoreError, match="不重复"):
        store.save_plugin_connections("mysql", [CONN, CONN])
    many = [{**CONN, "name": f"c{index}"} for index in range(33)]
    with pytest.raises(CredentialStoreError, match="最多 32"):
        store.save_plugin_connections("mysql", many)


def test_header_is_base64url_json_of_the_connection_list(tmp_path):
    service = PluginService(tmp_path, tmp_path / "state")
    service.credentials.save_plugin_connections("mysql", [CONN])
    plugin = service.registry.get("mysql")
    headers = service.headers(plugin)
    assert set(headers) == {"X-Ops-MySQL-Connections"}
    value = headers["X-Ops-MySQL-Connections"]
    assert "=" not in value and "+" not in value and "/" not in value
    assert _decode(value) == {"v": 1, "connections": [CONN]}


def test_manifest_agent_default_and_probe_results(tmp_path):
    tools = [{"name": name, "description": name, "annotations": {"readOnlyHint": True},
              "inputSchema": {"type": "object", "properties": {"filters": {"type": "object"}}}}
             for name in ("list_connections", "ts_list_process_defs")]
    manifest = {"name": "mysql", "version": "0.1.0", "auth": {"scheme": "connections"}, "tools": [
        {"name": "list_connections", "read_only": True},
        {"name": "ts_list_process_defs", "read_only": True, "agent_default": False},
    ]}
    probe = {"schema_version": 1, "ok": False, "data": None, "warnings": [], "error": {
        "code": "UPSTREAM_AUTH_FAILED", "message": "alpha-node1 认证失败",
        "details": {"failed": [{"connection": "alpha-node1", "code": "UPSTREAM_AUTH_FAILED"}]}}}
    transport, seen = mcp_server(tools, {"content": [{"type": "text", "text": json.dumps(probe)}]}, manifest=manifest)
    service = PluginService(tmp_path, tmp_path / "state", transport=transport)
    service.credentials.save_plugin_connections("mysql", [CONN])
    plugin = service.registry.update("mysql", enabled=True)

    result = service.connection_test(plugin)
    assert result["ok"] and result["probe"]["failed"] == [{"connection": "alpha-node1", "code": "UPSTREAM_AUTH_FAILED"}]
    calls = [json.loads(request.content) for request in seen if request.method == "POST"]
    assert {"name": "list_connections", "arguments": {"probe": True}} in [
        call["params"] for call in calls if call.get("method") == "tools/call"]
    assert all(request.headers.get("X-Ops-MySQL-Connections") for request in seen if request.method == "POST")

    enabled = {tool["name"]: tool["enabled"] for tool in service.view(plugin)["tools"]}
    assert enabled == {"list_connections": True, "ts_list_process_defs": False}, "agent_default=false 默认不给 Agent"
    assert [item["tool"] for item in service.snapshot("alpha")["tools"]] == ["list_connections"]


def test_envpack_connections_scheme_rules(tmp_path):
    with pytest.raises(EnvPackError, match="请求头名"):
        check_plugin_endpoint("http://h:1/mcp", "connections", "")
    root = tmp_path / "pack"
    root.mkdir()
    (root / "env.yaml").write_text(
        "version: 1\nid: lab\ndisplay_name: Lab\ntimezone: UTC\nclusters:\n  - {id: a, legacy_key: a, roles: [managed]}\n"
        "plugins:\n  - {id: db, url: 'http://h:1/mcp', auth: basic, connection_probe: list_connections}\n",
        encoding="utf-8")
    with pytest.raises(EnvPackError, match="connection_probe"):
        parse_env_pack(root)


def test_connections_api_validates_cluster_and_never_returns_passwords(tmp_path):
    client = TestClient(create_app(state_dir=tmp_path / "state", repo_root=tmp_path))
    response = client.put("/api/plugins/mysql/connections", json={"connections": [
        {"name": "alpha-node1", "host": "192.0.2.11", "user": "reader", "password": "s3cret",
         "database": "demo_db", "platform": "alpha"}]})
    assert response.status_code == 200
    body = response.json()
    assert body["connections"][0]["labels"] == {"platform": "alpha"} and "s3cret" not in response.text
    assert body["credential"]["configured"] is True

    bad = client.put("/api/plugins/mysql/connections", json={"connections": [
        {"name": "x", "host": "h", "user": "u", "password": "p", "platform": "nowhere"}]})
    assert bad.status_code == 422 and "未知集群" in bad.json()["detail"]
    assert client.put("/api/plugins/mysql/credential", json={"user": "u", "password": "p"}).status_code == 422
    assert client.put("/api/plugins/manager-alpha/connections", json={"connections": [
        {"name": "x", "host": "h", "user": "u", "password": "p", "platform": "alpha"}]}).status_code == 422
    listed = client.get("/api/plugins").json()
    assert "s3cret" not in json.dumps(listed)
