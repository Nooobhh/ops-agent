"""网关的写操作出口:执行前复核、签令牌、落档(令牌不落档)、送达未知的判定、Harness 侧只读调用。"""
import json

import pytest

from lib.plugins import PluginService
from lib.triage_agent import actions as actions_module
from lib.triage_agent.actions import ActionDenied, ActionGateway
from lib.triage_agent.diagnostics import DiagnosticRegistry
from lib.triage_agent.paths import AppPaths
from tests.plugins.fake_ops_mcp import PLUGIN, Clock, FakeOpsMcp, install_plugin, transport_timeout

SECRET = "a1" * 32


@pytest.fixture
def setup(tmp_path, monkeypatch):
    gateway = ActionGateway(AppPaths.from_repo_root(tmp_path), DiagnosticRegistry({}, []),
                            pool_factory=lambda _platform: pytest.fail("插件调用不得走 SSH"))
    service = PluginService(tmp_path, gateway.paths.state_dir)
    tools = install_plugin(service, secret=SECRET)
    mcp = FakeOpsMcp(SECRET, Clock())
    monkeypatch.setattr(actions_module, "call_plugin_tool", mcp)
    session = gateway.start_session(platform="alpha", node="node1", trigger="test:write")
    return gateway, service, mcp, session, tools["plan_operation"]["hash"]


def plan(gateway, session, plan_hash):
    result = gateway.run_plugin_tool(session.run_id, plugin_id=PLUGIN, tool="plan_operation", content_hash=plan_hash,
                                     arguments={"action": "restart", "scope": "component",
                                                "component": "LOGSEARCH_LOGFEEDER", "host": "node11"})
    return result["data"]


def execute(gateway, session, plan_hash, data, **overrides):
    return gateway.execute_plugin_operation(session.run_id, **{
        "plugin_id": PLUGIN, "plan_tool": "plan_operation", "content_hash": plan_hash,
        "plan_id": data["plan_id"], "plan_hash": data["plan_hash"], "approver": "alice", **overrides})


def session_text(session):
    return (session.dir / "session.json").read_text(encoding="utf-8") + (session.dir / "report.md").read_text()


def test_execute_signs_calls_and_records_approved_step_without_token(setup):
    gateway, _service, mcp, session, plan_hash = setup
    data = plan(gateway, session, plan_hash)
    result = execute(gateway, session, plan_hash, data)

    assert result["ok"] and result["data"]["request_id"] == 3301 and not result["maybe_delivered"]
    step = json.loads((session.dir / "session.json").read_text())["steps"][-1]
    assert step["cmd"] == f'[mcp:{PLUGIN}] execute_operation {{"plan_id": "{data["plan_id"]}"}}'
    assert step["approved"] is True
    assert step["stdout"].startswith(f"审批人: alice · 令牌摘要: {result['token_digest']}")
    token = mcp.tokens[-1]
    assert token not in session_text(session) and SECRET not in session_text(session)
    assert token not in json.dumps(result)
    assert mcp.executed() == [{"plan_id": data["plan_id"]}]


@pytest.mark.parametrize("mutate, code", [
    (lambda service: service.registry.set_tool(PLUGIN, "plan_operation", enabled=False), "PLUGIN_DISABLED"),
    (lambda service: service.registry.update(PLUGIN, enabled=False), "PLUGIN_DISABLED"),
    (lambda service: service.credentials.delete_plugin(PLUGIN) or service.credentials.save_plugin(
        PLUGIN, "basic", {"user": "admin", "password": "pw"}), "APPROVAL_SECRET_MISSING"),
])
def test_execute_rechecks_plugin_page_state_before_signing(setup, mutate, code):
    gateway, service, mcp, session, plan_hash = setup
    data = plan(gateway, session, plan_hash)
    mutate(service)
    with pytest.raises(ActionDenied, match=code):
        execute(gateway, session, plan_hash, data)
    assert mcp.executed() == []


def test_rejected_token_is_not_an_approved_step(setup):
    gateway, service, mcp, session, plan_hash = setup
    data = plan(gateway, session, plan_hash)
    service.credentials.save_plugin_approval_secret(PLUGIN, "b2" * 32)  # 中台与插件密钥不一致
    result = execute(gateway, session, plan_hash, data)
    assert result["error"]["code"] == "APPROVAL_INVALID" and not result["maybe_delivered"]
    assert json.loads((session.dir / "session.json").read_text())["steps"][-1]["approved"] is False
    assert mcp.requests == {}


def test_tampered_plan_hash_is_rejected_by_plugin(setup):
    gateway, _service, mcp, session, plan_hash = setup
    data = plan(gateway, session, plan_hash)
    result = execute(gateway, session, plan_hash, {**data, "plan_hash": "0" * 64})
    assert result["error"]["code"] == "APPROVAL_INVALID" and mcp.requests == {}


def test_timeout_after_send_is_maybe_delivered_and_recorded_as_approved(setup):
    gateway, _service, mcp, session, plan_hash = setup
    data = plan(gateway, session, plan_hash)
    mcp.execute_raises, mcp.submit_first = transport_timeout(), True
    result = execute(gateway, session, plan_hash, data)
    assert not result["ok"] and result["maybe_delivered"]
    assert json.loads((session.dir / "session.json").read_text())["steps"][-1]["approved"] is True


def test_harness_side_calls_use_declared_roles_and_survive_plugin_disable(setup):
    gateway, service, _mcp, session, plan_hash = setup
    data = plan(gateway, session, plan_hash)
    request_id = execute(gateway, session, plan_hash, data)["data"]["request_id"]
    service.registry.update(PLUGIN, enabled=False)
    before = len(json.loads((session.dir / "session.json").read_text())["steps"])
    tracked = gateway.run_plugin_write_tool(session.run_id, plugin_id=PLUGIN, role="track",
                                            arguments={"request_id": request_id})
    assert tracked["ok"] and tracked["data"]["status"] == "IN_PROGRESS"
    assert len(json.loads((session.dir / "session.json").read_text())["steps"]) == before, "轮询不落步骤"
    gateway.run_plugin_write_tool(session.run_id, plugin_id=PLUGIN, role="reconcile", arguments={}, record=True)
    assert len(json.loads((session.dir / "session.json").read_text())["steps"]) == before + 1
    with pytest.raises(ActionDenied, match="不支持的写协议角色"):
        gateway.run_plugin_write_tool(session.run_id, plugin_id=PLUGIN, role="execute", arguments={})
