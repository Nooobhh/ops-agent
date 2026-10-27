"""两阶段写在插件层的部分:环境包声明、准入(plan 要审批密钥、execute 永不暴露)、快照、密钥存取、令牌兼容。"""
import json
import os
import re
from pathlib import Path

import pytest

from lib.credentials import CredentialMissing, CredentialStoreError
from lib.envpack import EnvPackError, PluginWriteDecl
from lib.envpack import _parse_write as parse_write
from lib.plugins import PluginService, approval_token
from lib.plugins.catalog import EXECUTE_ONLY, NEEDS_APPROVAL_SECRET, WRITE_PLAN_NOTE, blocked_reason, write_role

from .fake_ops_mcp import PLUGIN, Clock, install_plugin, verify

SECRET = "0f" * 32
OPS_MCP_SOURCE = os.environ.get("OPS_MCP_SOURCE", "")  # ops-mcp 源码目录;未设则跳过交叉校验


def test_env_pack_declares_write_roles_for_official_demo_plugins(tmp_path):
    plugin = PluginService(tmp_path, tmp_path / "state").registry.get(PLUGIN)
    assert plugin.write == PluginWriteDecl(plan="plan_operation", execute="execute_operation", track="get_operation",
                                           reconcile="list_operations", impact="get_topology")
    assert write_role(plugin, "execute_operation") == "execute" and write_role(plugin, "get_config") is None


@pytest.mark.parametrize("raw, message", [
    ("x", "必须是映射"),
    ({"plan": "a", "execute": "b", "track": "c"}, "缺少 reconcile"),
    ({"plan": "a", "execute": "a", "track": "c", "reconcile": "d"}, "不能重名"),
    ({"plan": "a b", "execute": "b", "track": "c", "reconcile": "d"}, "不是合法工具名"),
    ({"plan": "a", "execute": "b", "track": "c", "reconcile": "d", "extra": 1}, "未知字段"),
])
def test_write_declaration_is_validated(raw, message):
    with pytest.raises(EnvPackError, match=message):
        parse_write(raw, "plugins[0]")


def test_admission_plan_needs_secret_and_execute_is_never_exposed(tmp_path):
    service = PluginService(tmp_path, tmp_path / "state")
    tools = install_plugin(service)
    plugin = service.registry.get(PLUGIN)
    assert blocked_reason(plugin, tools["execute_operation"], approval_secret=True) == EXECUTE_ONLY
    assert blocked_reason(plugin, tools["plan_operation"]) == NEEDS_APPROVAL_SECRET
    assert blocked_reason(plugin, tools["plan_operation"], approval_secret=True) is None
    assert blocked_reason(plugin, tools["get_operation"]) is None, "跟踪工具是普通只读工具"

    view = {item["name"]: item for item in service.view(plugin)["tools"]}
    assert view["plan_operation"]["blocked_reason"] == NEEDS_APPROVAL_SECRET and not view["plan_operation"]["enabled"]
    assert view["execute_operation"]["write_role"] == "execute" and not view["execute_operation"]["enabled"]
    assert service.view(plugin)["write"] == {"plan": "plan_operation", "execute": "execute_operation",
                                              "approval_secret_configured": False}


def test_snapshot_offers_plan_only_with_secret_and_human_channel(tmp_path):
    service = PluginService(tmp_path, tmp_path / "state")
    install_plugin(service)
    names = lambda snapshot: {item["tool"] for item in snapshot["tools"]}  # noqa: E731
    assert "plan_operation" not in names(service.snapshot("alpha", writable=True)), "没配密钥不暴露"

    service.credentials.save_plugin_approval_secret(PLUGIN, SECRET)
    manual = service.snapshot("alpha", writable=True)
    assert "plan_operation" in names(manual) and "execute_operation" not in names(manual)
    assert "plan_operation" not in names(service.snapshot("alpha", writable=False)), "自动任务没有人工通道"
    plan = next(item for item in manual["tools"] if item["tool"] == "plan_operation")
    assert plan["role"] == "write_plan"
    assert plan["write"] == {"execute": "execute_operation", "track": "get_operation",
                             "reconcile": "list_operations", "impact": "get_topology"}
    assert WRITE_PLAN_NOTE in plan["schema"]["function"]["description"]
    assert SECRET not in json.dumps(manual) and SECRET not in json.dumps(service.view(service.registry.get(PLUGIN)))

    service.registry.set_tool(PLUGIN, "plan_operation", enabled=False)
    assert "plan_operation" not in names(service.snapshot("alpha", writable=True)), "插件页关掉即不暴露"


def test_approval_secret_store_validates_and_is_removed_with_plugin(tmp_path):
    store = PluginService(tmp_path, tmp_path / "state").credentials
    with pytest.raises(CredentialMissing):
        store.plugin_approval_secret(PLUGIN)
    for bad in ("short", "has space inside 1234567890", "密钥" * 10):
        with pytest.raises(CredentialStoreError):
            store.save_plugin_approval_secret(PLUGIN, bad)
    store.save_plugin_approval_secret(PLUGIN, f"  {SECRET}\n")
    assert store.plugin_approval_secret(PLUGIN) == SECRET and store.plugin_approval_configured(PLUGIN)
    store.delete_plugin(PLUGIN)
    assert not store.plugin_approval_configured(PLUGIN)


def test_token_round_trip_and_rejections():
    clock = Clock()
    token = approval_token.sign(SECRET, "p-1", "h1", now=clock())
    assert verify(SECRET.encode(), token, "p-1", "h1", clock()) == ""
    assert verify(b"other-secret-0000", token, "p-1", "h1", clock()) == "签名不符"
    assert verify(SECRET.encode(), token, "p-1", "h2", clock()) == "令牌与该计划不匹配"
    assert verify(SECRET.encode(), token, "p-2", "h1", clock()) == "令牌与该计划不匹配"
    assert verify(SECRET.encode(), token, "p-1", "h1", clock() + approval_token.TOKEN_TTL_SECONDS + 1) == "令牌已过期"
    head, sig = token.split(".")
    assert verify(SECRET.encode(), f"{head}x.{sig}", "p-1", "h1", clock()) != ""
    assert approval_token.digest(token) == approval_token.digest(token) and len(approval_token.digest(token)) == 12


@pytest.mark.skipif(not OPS_MCP_SOURCE, reason="未设 OPS_MCP_SOURCE(ops-mcp 源码目录)")
def test_token_is_accepted_by_real_ops_mcp_verifier():
    """只读加载 ops-mcp 的 core/approval.py 交叉校验字节兼容(不 import 其包、不写其仓)。"""
    approval = Path(OPS_MCP_SOURCE).expanduser() / "src/ops_mcp/core/approval.py"
    source = approval.read_text(encoding="utf-8")
    source = re.sub(r"^from \.errors import OpsError$", "", source, flags=re.M)

    class OpsError(Exception):
        def __init__(self, code, message):
            super().__init__(message)
            self.code = code

    namespace = {"OpsError": OpsError, "__name__": "ops_mcp_approval_copy"}
    exec(compile(source, str(approval), "exec"), namespace)  # noqa: S102 — 本机受信源码,只读
    token = approval_token.sign(f"{SECRET}\n", "p-abc", "hash-1")
    namespace["verify"](SECRET.encode(), token, "p-abc", "hash-1")
    assert namespace["digest"](token) == approval_token.digest(token)
    with pytest.raises(OpsError):
        namespace["verify"](SECRET.encode(), token, "p-abc", "hash-2")
