import json

import pytest

from lib.plugins import PluginCallError, PluginService
from lib.triage_agent import actions as actions_module
from lib.triage_agent.actions import ActionDenied, ActionGateway
from lib.triage_agent.diagnostics import DiagnosticRegistry
from lib.triage_agent.paths import AppPaths

SECRET = "plain-db-password"


@pytest.fixture
def gateway(tmp_path):
    gateway = ActionGateway(AppPaths.from_repo_root(tmp_path), DiagnosticRegistry({}, []),
                            pool_factory=lambda _platform: pytest.fail("插件调用不得走 SSH"))
    service = PluginService(tmp_path, gateway.paths.state_dir)
    service.registry.update("manager-alpha", enabled=True)
    service.credentials.save_plugin("manager-alpha", "basic", {"user": "admin", "password": "pw"})
    session = gateway.start_session(platform="alpha", node="node1", trigger="test:plugin")
    return gateway, service, session


def steps(session):
    return json.loads((session.dir / "session.json").read_text(encoding="utf-8"))["steps"]


def run(gateway, session, **overrides):
    return gateway.run_plugin_tool(session.run_id, **{
        "plugin_id": "manager-alpha", "tool": "get_config", "arguments": {"service": "HIVE"},
        "content_hash": "h1", **overrides})


def test_call_is_recorded_as_redacted_step(gateway, monkeypatch):
    gateway, _service, session = gateway
    seen = {}

    def fake_call(url, headers, timeout, name, arguments):
        seen.update(url=url, headers=headers, name=name, arguments=arguments)
        envelope = {"schema_version": 1, "ok": True, "data": {"javax.jdo.option.ConnectionPassword": SECRET},
                    "warnings": [], "error": None, "meta": {}}
        return {"content": [{"type": "text", "text": json.dumps(envelope)}]}

    monkeypatch.setattr(actions_module, "call_plugin_tool", fake_call)
    result = run(gateway, session)

    assert seen["url"] == "http://127.0.0.1:30161/mcp" and seen["headers"]["Authorization"].startswith("Basic ")
    assert result["ok"] and result["trust"] == "official"
    step = steps(session)[-1]
    assert step["cmd"] == '[mcp:manager-alpha] get_config {"service": "HIVE"}'
    assert step["exit_code"] == 0
    assert SECRET not in step["stdout"] and SECRET not in json.dumps(result)


def test_unavailable_plugin_is_an_explicit_failed_step(gateway, monkeypatch):
    gateway, _service, session = gateway

    def down(*_args):
        raise PluginCallError("PLUGIN_UNAVAILABLE", "连接失败")

    monkeypatch.setattr(actions_module, "call_plugin_tool", down)
    result = run(gateway, session)

    assert not result["ok"] and result["error"]["code"] == "PLUGIN_UNAVAILABLE"
    assert steps(session)[-1]["exit_code"] == 1 and "PLUGIN_UNAVAILABLE" in steps(session)[-1]["stderr"]


def test_disabled_tool_or_missing_credentials_is_denied_without_step(gateway, monkeypatch):
    gateway, service, session = gateway
    monkeypatch.setattr(actions_module, "call_plugin_tool", lambda *_a: pytest.fail("不得发出调用"))

    service.registry.set_tool("manager-alpha", "get_config", enabled=False)
    with pytest.raises(ActionDenied, match="PLUGIN_DISABLED"):
        run(gateway, session)
    service.registry.set_tool("manager-alpha", "get_config", enabled=True)
    service.credentials.delete_plugin("manager-alpha")
    with pytest.raises(ActionDenied, match="PLUGIN_NOT_CONFIGURED"):
        run(gateway, session)
    service.registry.update("manager-alpha", enabled=False)
    with pytest.raises(ActionDenied, match="PLUGIN_DISABLED"):
        run(gateway, session)
    assert steps(session) == []
