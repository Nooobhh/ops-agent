"""插件服务:注册表 + 工具缓存 + 凭据 + MCP 客户端,供插件页、任务快照与 Agent 调用共用。"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from lib.credentials import CredentialMissing, CredentialStore, CredentialStoreError
from lib.data_root import local_config_path

from . import client
from .catalog import CatalogStore, blocked_reason, model_definition, normalize_tool, scope_role, write_role
from .exposure import normalize_result
from .registry import Plugin, PluginRegistry

# 健康状态封闭集;凭据是否配置另列,不混进健康
HEALTH_STATES = ("ok", "unreachable", "auth_failed", "protocol_error", "not_configured")
CONNECTIONS_HEADER_MAX = 16 * 1024


class PluginService:
    def __init__(self, repo_root: Path, state_dir: Path, *, transport: Optional[httpx.BaseTransport] = None):
        self.registry = PluginRegistry(repo_root)
        self.catalog = CatalogStore(state_dir)
        self.credentials = CredentialStore(
            local_config_path(repo_root, "auth-profiles.json"),
            legacy_credentials_path=local_config_path(repo_root, "credentials.json"),
        )
        self.transport = transport

    # ---- 认证 ----

    def headers(self, plugin: Plugin, secret: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        if plugin.auth == "none":
            return {}
        if secret is None:
            try:
                secret = self.credentials.get_plugin_secret(plugin.id, plugin.auth)
            except (CredentialMissing, CredentialStoreError):
                raise client.PluginCallError(
                    "PLUGIN_NOT_CONFIGURED", f"插件 {plugin.title} 未配置凭据,请在插件页填写",
                ) from None
        if plugin.auth == "basic":
            token = base64.b64encode(f"{secret['user']}:{secret['password']}".encode()).decode()
            return {"Authorization": f"Basic {token}"}
        if plugin.auth == "connections":
            return {plugin.header_name: _connections_header(secret["connections"])}
        if plugin.auth == "bearer":
            return {"Authorization": f"Bearer {secret['token']}"}
        return {plugin.header_name: secret["value"]}

    # ---- 健康与连接测试 ----

    def health(self, plugin: Plugin) -> Dict[str, Any]:
        """有 /healthz 的(ops-mcp)免登录判定;没有的用一次 MCP 握手判定。"""
        status, body, error = client.probe(plugin.origin, "/healthz", transport=self.transport)
        if error:
            return _health("unreachable", error)
        if status == 200 and isinstance(body, dict) and body.get("ok") is True:
            return _health("ok", version=str(body.get("version") or ""))
        try:
            with client.McpSession(plugin.url, self.headers(plugin), plugin.timeout_seconds, self.transport) as session:
                return _health("ok", version=str(session.server_info.get("version") or ""))
        except client.PluginCallError as exc:
            return _health(_state_of(exc.code), exc.message)

    def connection_test(self, plugin: Plugin, secret: Optional[Dict[str, str]] = None, *,
                        save: bool = True) -> Dict[str, Any]:
        """healthz → manifest → 握手 + tools/list;save 时写工具缓存并记最近测试。"""
        health = self.health(plugin) if secret is None else None
        status, manifest, _ = client.probe(plugin.origin, "/manifest", transport=self.transport)
        manifest = manifest if status == 200 and _valid_manifest(manifest) else None
        read_only = {item["name"]: bool(item.get("read_only")) for item in (manifest or {}).get("tools", [])}
        agent_default = {item["name"]: item.get("agent_default") is not False
                         for item in (manifest or {}).get("tools", [])}
        try:
            server, raw_tools = client.list_tools(
                plugin.url, self.headers(plugin, secret), plugin.timeout_seconds, transport=self.transport,
            )
        except client.PluginCallError as exc:
            if save:
                self.credentials.record_plugin_test(plugin.id, ok=False, error_code=exc.code)
            return {"ok": False, "error_code": exc.code, "message": exc.message, "health": health,
                    "manifest": manifest is not None, "tools": []}
        tools = [normalize_tool(item, read_only.get(str(item.get("name"))), agent_default.get(str(item.get("name")), True))
                 for item in raw_tools]
        version = str((manifest or {}).get("version") or server.get("version") or "")
        if save:
            self.catalog.put(plugin.id, {"url": plugin.url, "server": server, "manifest": manifest,
                                         "version": version, "tools": tools})
            self.credentials.record_plugin_test(plugin.id, ok=True)
        writes = sum(1 for item in tools if not item["read_only"])
        return {"ok": True, "error_code": None, "health": health, "manifest": manifest is not None,
                "version": version, "tools": tools, "probe": self._probe_connections(plugin, secret),
                "message": f"连接成功:{len(tools)} 个工具(写操作 {writes} 个)"}

    def _probe_connections(self, plugin: Plugin, secret: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """连接列表认证:调环境包声明的试连工具(probe=true),逐条给出可达、版本、角色、缺的权限。"""
        if plugin.auth != "connections" or not plugin.connection_probe:
            return None
        try:
            raw = self.call_with(plugin, self.headers(plugin, secret), plugin.connection_probe, {"probe": True})
        except client.PluginCallError as exc:
            return {"ok": False, "connections": [], "failed": [], "warnings": [],
                    "error": {"code": exc.code, "message": exc.message}}
        result = normalize_result(raw, plugin_id=plugin.id, tool=plugin.connection_probe, trusted=plugin.trusted)
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        error = result.get("error") if isinstance(result.get("error"), dict) else None
        details = (error or {}).get("details") if isinstance((error or {}).get("details"), dict) else {}
        return {"ok": result["ok"], "connections": list(data.get("connections") or []),
                "failed": list(details.get("failed") or []), "warnings": result.get("warnings") or [],
                "error": None if error is None else {"code": error.get("code"), "message": error.get("message")}}

    # ---- 插件页视图 ----

    def approval_ready(self, plugin: Plugin) -> bool:
        """插件声明了两阶段写且已配审批密钥:plan 工具才可能交给模型。"""
        return plugin.write is not None and self.credentials.plugin_approval_configured(plugin.id)

    def view(self, plugin: Plugin) -> Dict[str, Any]:
        entry = self.catalog.get(plugin.id) or {}
        approval = self.approval_ready(plugin)
        tools = []
        for tool in entry.get("tools") or []:
            setting = plugin.tool_settings.get(tool["name"])
            blocked = blocked_reason(plugin, tool, approval_secret=approval)
            tools.append({
                "name": tool["name"],
                "read_only": tool["read_only"],
                "write_role": write_role(plugin, tool["name"]),
                "hash": tool["hash"],
                "enabled": blocked is None
                and plugin.tool_enabled(tool["name"], tool["hash"], tool.get("agent_default", True)),
                "agent_default": tool.get("agent_default", True),
                "blocked_reason": blocked,
                # 第三方工具启用后内容变了:自动停用,待人工复核
                "needs_review": bool(not plugin.trusted and setting and setting.enabled
                                     and setting.pinned_hash != tool["hash"]),
                "model_description": model_definition(plugin, tool)["function"]["description"],
            })
        manifest = entry.get("manifest") or {}
        return {
            "id": plugin.id,
            "title": plugin.title,
            "source": plugin.source,
            "url": plugin.url,
            "default_url": plugin.default_url or None,
            "cluster": plugin.cluster,
            "auth": plugin.auth,
            "header_name": plugin.header_name or None,
            "auth_account": (manifest.get("auth") or {}).get("account"),
            "timeout_seconds": plugin.timeout_seconds,
            "enabled": plugin.enabled,
            "credential": self.credentials.plugin_status(plugin.id, plugin.auth),
            # 连接列表认证:展示部分(无密码、账号打码)
            "connections": self.credentials.plugin_connections_view(plugin.id)
            if plugin.auth == "connections" else None,
            "version": entry.get("version") or None,
            "manifest": bool(manifest),
            "fetched_at": entry.get("fetched_at"),
            "tools": tools,
            # 两阶段写:只回角色与密钥是否配置,密钥本身永不回传
            "write": None if plugin.write is None else {
                "plan": plugin.write.plan, "execute": plugin.write.execute,
                "approval_secret_configured": approval,
            },
            # 全局插件:按哪个标签区分集群、哪些工具强制带本集群过滤;后台采集用到哪几类
            "scope": None if plugin.scope is None else {
                "label": plugin.scope.label, "clusters": dict(plugin.scope.clusters),
                "tools": list(plugin.scope.tools),
            },
            "feeds": sorted(plugin.feeds),
        }

    # ---- 任务快照与调用 ----

    def snapshot(self, platform: Optional[str], *, writable: bool = False) -> Dict[str, Any]:
        """新任务首次执行时冻结的插件工具;已定集群只收该集群与不绑定集群的插件。不联网。

        writable:任务有人工审批通道(人工任务)。否则写操作计划工具一律不进快照。
        """
        tools: List[Dict[str, Any]] = []
        plugins: List[Dict[str, Any]] = []
        notices: List[str] = []
        catalog = self.catalog.load()
        for plugin in self.registry.plugins():
            if not plugin.enabled or (platform and plugin.cluster and plugin.cluster != platform):
                continue
            entry = catalog.get(plugin.id)
            if not entry:
                notices.append(f"插件 {plugin.title} 已启用但还没有工具清单,请在插件页做连接测试")
                continue
            approval = writable and self.approval_ready(plugin)
            exposed = [
                tool for tool in entry.get("tools") or []
                if blocked_reason(plugin, tool, approval_secret=approval) is None
                and plugin.tool_enabled(tool["name"], tool["hash"], tool.get("agent_default", True))
            ]
            if not exposed:
                continue
            plugins.append({"id": plugin.id, "title": plugin.title, "cluster": plugin.cluster,
                            "trusted": plugin.trusted, "version": entry.get("version") or ""})
            tools.extend({
                "name": model_definition(plugin, tool)["function"]["name"],
                "plugin": plugin.id,
                "tool": tool["name"],
                "trusted": plugin.trusted,
                "cluster": plugin.cluster,
                "hash": tool["hash"],
                "schema": model_definition(plugin, tool),
                **_scope_spec(plugin, tool["name"]),
                # 写操作计划工具:配对的执行/跟踪/对账工具随任务冻结,执行时网关再按插件页现状复核
                **({"role": "write_plan", "write": {
                    "execute": plugin.write.execute, "track": plugin.write.track,
                    "reconcile": plugin.write.reconcile, "impact": plugin.write.impact,
                }} if write_role(plugin, tool["name"]) == "plan" else {}),
            } for tool in exposed)
        return {"version": 1, "plugins": plugins, "tools": tools, "notices": notices}

    def call(self, plugin: Plugin, tool: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return self.call_with(plugin, self.headers(plugin), tool, arguments)

    def call_with(self, plugin: Plugin, headers: Dict[str, str], tool: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return client.call_tool(plugin.url, headers, plugin.timeout_seconds, tool, arguments, transport=self.transport)

    # ---- 后台采集(告警源、资源采集) ----

    def feed_call(self, kind: str, role: str, arguments: Dict[str, Any], *,
                  plugin: Optional[Plugin] = None) -> Dict[str, Any]:
        """按环境包 feeds 声明调插件取数,返回归一后的 {data, warnings, meta}。

        plugin 缺省取唯一声明了该类 feeds 的插件。不看插件页开关(开关只管 Agent 工具),
        地址与凭据取插件页。插件不可达、``ok=false``(如上游 ``UPSTREAM_UNAVAILABLE``)
        一律抛 PluginCallError,调用方按源失败处理,不当空结果。
        """
        plugin = plugin or self.registry.feed(kind)
        tool = (plugin.feeds.get(kind) or {}).get(role)
        if not tool:
            raise client.PluginCallError("PLUGIN_FEED_UNDECLARED", f"插件 {plugin.title} 未声明 feeds.{kind}.{role}")
        result = normalize_result(self.call(plugin, tool, arguments),
                                  plugin_id=plugin.id, tool=tool, trusted=plugin.trusted)
        if not result["ok"]:
            error = result.get("error") if isinstance(result.get("error"), dict) else {}
            source = (error.get("details") or {}).get("source") if isinstance(error.get("details"), dict) else None
            raise client.PluginCallError(
                str(error.get("code") or "PLUGIN_TOOL_ERROR"),
                f"{tool}: {error.get('message') or '插件返回失败'}" + (f"(数据源 {source})" if source else ""),
            )
        return result


def _scope_spec(plugin: Plugin, tool: str) -> Dict[str, Any]:
    """冻结进任务快照的集群作用域;Harness 调用时据此补 / 校验集群过滤。"""
    role = scope_role(plugin, tool)
    if role is None:
        return {}
    return {"scope": {"role": role, "argument": plugin.scope.argument, "label": plugin.scope.label,
                      "clusters": dict(plugin.scope.clusters)}}


def _connections_header(connections: List[Dict[str, Any]]) -> str:
    """连接列表 → 请求头值:{"v":1,"connections":[…]} 的 JSON 做 base64url(去填充)。"""
    payload = json.dumps({"v": 1, "connections": connections}, ensure_ascii=False, separators=(",", ":"))
    value = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    if len(value) > CONNECTIONS_HEADER_MAX:
        raise client.PluginCallError("PLUGIN_NOT_CONFIGURED", "连接列表超过请求头 16KB 上限,请减少连接")
    return value


def _health(state: str, detail: str = "", *, version: str = "") -> Dict[str, Any]:
    return {"state": state, "detail": detail or None, "version": version or None,
            "checked_at": datetime.now(timezone.utc).isoformat()}


def _state_of(code: str) -> str:
    return {
        "PLUGIN_UNAVAILABLE": "unreachable",
        "PLUGIN_AUTH_FAILED": "auth_failed",
        "PLUGIN_NOT_CONFIGURED": "not_configured",
    }.get(code, "protocol_error")


def _valid_manifest(value: Any) -> bool:
    return (
        isinstance(value, dict) and isinstance(value.get("name"), str) and isinstance(value.get("tools"), list)
        and all(isinstance(item, dict) and isinstance(item.get("name"), str) for item in value["tools"])
    )
