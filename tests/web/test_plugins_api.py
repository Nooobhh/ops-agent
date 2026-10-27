import json

import pytest
from fastapi.testclient import TestClient

from lib.plugins import client as plugin_client
from web.server.app import create_app

TOOLS = [
    {"name": "search", "description": "检索", "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}},
    {"name": "delete_doc", "description": "删除", "annotations": {"readOnlyHint": False},
     "inputSchema": {"type": "object", "properties": {}}},
]


@pytest.fixture
def api(tmp_path, monkeypatch):
    calls = []

    def probe(origin, path, transport=None):
        return (200, {"ok": True, "version": "1.2"}, None) if path == "/healthz" else (404, None, None)

    def list_tools(url, headers, timeout, transport=None):
        calls.append(headers)
        return {"name": "kb", "version": "1.2"}, TOOLS

    monkeypatch.setattr(plugin_client, "probe", probe)
    monkeypatch.setattr(plugin_client, "list_tools", list_tools)
    return TestClient(create_app(state_dir=tmp_path / "state", repo_root=tmp_path)), calls


def test_official_plugins_listed_disabled_without_credentials(api):
    client, _ = api
    body = client.get("/api/plugins").json()
    plugins = {item["id"]: item for item in body["plugins"]}
    assert plugins["manager-alpha"]["source"] == "official" and not plugins["manager-alpha"]["enabled"]
    assert not plugins["manager-alpha"]["credential"]["configured"]
    assert {item["key"] for item in body["clusters"]} >= {"alpha", "beta"}
    health = client.get("/api/plugins/health").json()["plugins"]
    assert health["manager-alpha"]["state"] == "ok" and health["manager-alpha"]["enabled"] is False


def test_custom_plugin_lifecycle_and_tool_admission(api):
    client, calls = api
    assert client.post("/api/plugins", json={"id": "Bad Id", "title": "x", "url": "http://h/mcp"}).status_code == 422
    created = client.post("/api/plugins", json={"id": "kb", "title": "知识检索", "url": "http://h:1/mcp",
                                                "auth": "bearer"})
    assert created.status_code == 200 and created.json()["source"] == "custom"

    saved = client.put("/api/plugins/kb/credential", json={"token": "secret-token-1"})
    assert saved.json()["credential"]["configured"] and "secret-token-1" not in saved.text

    tested = client.post("/api/plugins/kb/test").json()
    assert tested["ok"] and calls[-1] == {"Authorization": "Bearer secret-token-1"}
    tools = {item["name"]: item for item in tested["plugin"]["tools"]}
    assert not tools["search"]["enabled"], "第三方工具默认停用"
    assert tools["delete_doc"]["blocked_reason"]
    assert tools["search"]["model_description"].startswith("[第三方插件 知识检索")

    assert client.patch("/api/plugins/kb", json={"tools": {"delete_doc": True}}).status_code == 422
    enabled = client.patch("/api/plugins/kb", json={"enabled": True, "tools": {"search": True}}).json()
    assert enabled["enabled"] and next(t for t in enabled["tools"] if t["name"] == "search")["enabled"]
    names = {item["name"]: item for item in client.get("/api/triage-tools").json()["items"]}
    assert names["mcp__kb__search"]["plugin"] == "kb" and "mcp__kb__delete_doc" not in names

    assert client.delete("/api/plugins/manager-alpha").status_code == 422
    assert client.delete("/api/plugins/kb").status_code == 200
    assert "kb" not in {item["id"] for item in client.get("/api/plugins").json()["plugins"]}
    assert "mcp__kb__search" not in json.dumps(client.get("/api/triage-tools").json())


def test_official_url_override_and_draft_test(api):
    client, calls = api
    patched = client.patch("/api/plugins/manager-alpha", json={"url": "http://198.51.100.109:8761/mcp", "enabled": True})
    assert patched.json()["url"] == "http://198.51.100.109:8761/mcp" and patched.json()["default_url"]
    assert client.patch("/api/plugins/manager-alpha", json={"auth": "none"}).status_code == 422
    assert client.post("/api/plugins/manager-alpha/reset").json()["url"] == patched.json()["default_url"]

    missing = client.post("/api/plugins/test", json={"url": "http://h/mcp", "auth": "basic"})
    assert missing.status_code == 422
    draft = client.post("/api/plugins/test", json={"url": "http://h/mcp", "auth": "basic",
                                                   "credential": {"user": "u", "password": "p"}}).json()
    assert draft["ok"] and [item["name"] for item in draft["tools"]] == ["search", "delete_doc"]
    assert calls[-1]["Authorization"].startswith("Basic ")


def test_approval_secret_gates_the_write_plan_tool_and_is_never_returned(api, tmp_path):
    from lib.plugins import PluginService
    from tests.plugins.fake_ops_mcp import install_plugin

    client, _ = api
    install_plugin(PluginService(tmp_path, tmp_path / "state"), plan_enabled=False)
    blocked = client.patch("/api/plugins/manager-alpha", json={"tools": {"plan_operation": True}})
    assert blocked.status_code == 422 and "审批密钥" in blocked.json()["detail"]
    assert client.patch("/api/plugins/manager-alpha", json={"tools": {"execute_operation": True}}).status_code == 422

    secret = "e5" * 32
    assert client.put("/api/plugins/manager-alpha/approval-secret", json={"secret": "short"}).status_code == 422
    saved = client.put("/api/plugins/manager-alpha/approval-secret", json={"secret": secret})
    assert saved.status_code == 200 and secret not in saved.text
    assert saved.json()["write"]["approval_secret_configured"] is True
    enabled = client.patch("/api/plugins/manager-alpha", json={"tools": {"plan_operation": True}}).json()
    tools = {item["name"]: item for item in enabled["tools"]}
    assert tools["plan_operation"]["enabled"] and tools["plan_operation"]["write_role"] == "plan"
    assert not tools["execute_operation"]["enabled"] and tools["execute_operation"]["blocked_reason"]
    assert secret not in client.get("/api/plugins").text

    client.post("/api/plugins", json={"id": "kb", "title": "知识检索", "url": "http://h:1/mcp"})
    assert client.put("/api/plugins/kb/approval-secret", json={"secret": secret}).status_code == 422
