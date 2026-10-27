"""插件页接口:注册与开关只经 PluginRegistry,凭据只经 CredentialStore,工具缓存只经 CatalogStore。

主动外呼仅有健康探测与连接测试(免登录 /healthz、/manifest 与 MCP 握手 + tools/list)。
凭据与写操作审批密钥只写不读:任何响应都只有「是否已配置」与打码用户名。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field

from lib.credentials import PLUGIN_SECRET_FIELDS, CredentialStoreError
from lib.envpack import EnvPackError, check_plugin_endpoint, load_env_pack
from lib.plugins import Plugin, PluginConfigError, PluginNotFound, PluginService
from lib.plugins.catalog import blocked_reason
from lib.plugins.registry import DEFAULT_TIMEOUT_SECONDS
from lib.triage_agent.models import StrictModel
from web.server.context import RequestContext, request_context

AuthScheme = Literal["none", "basic", "bearer", "header"]


class CustomPluginBody(StrictModel):
    id: str = Field(min_length=1, max_length=24)
    title: str = Field(min_length=1, max_length=64)
    url: str = Field(min_length=1, max_length=512)
    auth: AuthScheme = "none"
    header_name: Optional[str] = Field(default=None, max_length=64)
    cluster: Optional[str] = Field(default=None, max_length=64)
    timeout_seconds: float = Field(default=DEFAULT_TIMEOUT_SECONDS, ge=5, le=300)


class PluginPatchBody(StrictModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=64)
    url: Optional[str] = Field(default=None, min_length=1, max_length=512)
    auth: Optional[AuthScheme] = None
    header_name: Optional[str] = Field(default=None, max_length=64)
    cluster: Optional[str] = Field(default=None, max_length=64)
    timeout_seconds: Optional[float] = Field(default=None, ge=5, le=300)
    enabled: Optional[bool] = None
    tools: Optional[Dict[str, bool]] = None


class CredentialBody(StrictModel):
    user: Optional[str] = Field(default=None, min_length=1, max_length=320)
    password: Optional[str] = Field(default=None, min_length=1, max_length=4096)
    token: Optional[str] = Field(default=None, min_length=1, max_length=8192)
    value: Optional[str] = Field(default=None, min_length=1, max_length=8192)


class ConnectionBody(StrictModel):
    name: str = Field(min_length=1, max_length=32)
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=3306, ge=1, le=65535)
    user: Optional[str] = Field(default=None, max_length=320)  # 留空 = 沿用同名连接原账号
    password: Optional[str] = Field(default=None, max_length=4096)  # 留空 = 沿用同名连接原密码
    database: Optional[str] = Field(default=None, max_length=64)
    platform: str = Field(min_length=1, max_length=64)


class ConnectionsBody(StrictModel):
    connections: List[ConnectionBody] = Field(min_length=1, max_length=32)


class ApprovalSecretBody(StrictModel):
    secret: str = Field(min_length=1, max_length=1024)


class DraftTestBody(StrictModel):
    url: str = Field(min_length=1, max_length=512)
    auth: AuthScheme = "none"
    header_name: Optional[str] = Field(default=None, max_length=64)
    timeout_seconds: float = Field(default=DEFAULT_TIMEOUT_SECONDS, ge=5, le=300)
    credential: Optional[CredentialBody] = None


def router(*, repo_root: Path, state_dir: Path) -> APIRouter:
    routes = APIRouter()
    repo_root, state_dir = Path(repo_root).resolve(), Path(state_dir)

    def service() -> PluginService:
        return PluginService(repo_root, state_dir)

    def find(svc: PluginService, plugin_id: str) -> Plugin:
        try:
            return svc.registry.get(plugin_id)
        except PluginNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PluginConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @routes.get("/api/plugins")
    def list_plugins(_context: RequestContext = Depends(request_context)) -> dict:
        svc = service()
        try:
            plugins = [svc.view(plugin) for plugin in svc.registry.plugins()]
        except PluginConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        clusters = [{"key": item.legacy_key, "name": item.display_name}
                    for item in load_env_pack(repo_root).clusters]
        return {"schema_version": 1, "plugins": plugins, "clusters": clusters}

    @routes.get("/api/plugins/health")
    def plugins_health(_context: RequestContext = Depends(request_context)) -> dict:
        svc = service()
        try:
            plugins = svc.registry.plugins()
        except PluginConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        with ThreadPoolExecutor(max_workers=8) as pool:
            states = list(pool.map(svc.health, plugins))
        return {"schema_version": 1, "plugins": {
            plugin.id: {**state, "enabled": plugin.enabled} for plugin, state in zip(plugins, states)
        }}

    @routes.post("/api/plugins")
    def create_plugin(body: CustomPluginBody, _context: RequestContext = Depends(request_context)) -> dict:
        svc = service()
        try:
            plugin = svc.registry.create_custom(
                body.id, title=body.title, url=body.url, auth=body.auth, header_name=body.header_name or "",
                cluster=body.cluster or None, timeout_seconds=body.timeout_seconds,
            )
        except PluginConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return svc.view(plugin)

    @routes.patch("/api/plugins/{plugin_id}")
    def patch_plugin(plugin_id: str, body: PluginPatchBody,
                     _context: RequestContext = Depends(request_context)) -> dict:
        svc = service()
        plugin = find(svc, plugin_id)
        changes = body.model_dump(exclude_none=True, exclude={"tools"})
        if "cluster" in body.model_fields_set and body.cluster in ("", None):
            changes["cluster"] = ""
        try:
            if changes:
                plugin = svc.registry.update(plugin_id, **changes)
            catalog = {item["name"]: item for item in (svc.catalog.get(plugin_id) or {}).get("tools") or []}
            for name, enabled in (body.tools or {}).items():
                tool = catalog.get(name)
                if tool is None:
                    raise PluginConfigError(f"工具清单里没有 {name},请先连接测试")
                reason = blocked_reason(plugin, tool, approval_secret=svc.approval_ready(plugin))
                if enabled and reason:
                    raise PluginConfigError(f"{name} 不能启用:{reason}")
                plugin = svc.registry.set_tool(plugin_id, name, enabled=enabled, content_hash=tool["hash"])
        except PluginConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return svc.view(plugin)

    @routes.delete("/api/plugins/{plugin_id}")
    def delete_plugin(plugin_id: str, _context: RequestContext = Depends(request_context)) -> dict:
        svc = service()
        find(svc, plugin_id)
        try:
            svc.registry.delete_custom(plugin_id)
        except PluginConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        svc.credentials.delete_plugin(plugin_id)
        svc.catalog.drop(plugin_id)
        return {"deleted": plugin_id}

    @routes.post("/api/plugins/{plugin_id}/reset")
    def reset_plugin(plugin_id: str, _context: RequestContext = Depends(request_context)) -> dict:
        svc = service()
        find(svc, plugin_id)
        try:
            return svc.view(svc.registry.reset_official(plugin_id))
        except PluginConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @routes.put("/api/plugins/{plugin_id}/credential")
    def put_credential(plugin_id: str, body: CredentialBody,
                       _context: RequestContext = Depends(request_context)) -> dict:
        svc = service()
        plugin = find(svc, plugin_id)
        if plugin.auth == "connections":
            raise HTTPException(status_code=422, detail="该插件按连接列表认证,请在连接列表里编辑")
        try:
            svc.credentials.save_plugin(plugin_id, plugin.auth, body.model_dump(exclude_none=True))
        except CredentialStoreError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return svc.view(plugin)

    @routes.put("/api/plugins/{plugin_id}/connections")
    def put_connections(plugin_id: str, body: ConnectionsBody,
                        _context: RequestContext = Depends(request_context)) -> dict:
        """连接列表整表替换;密码只写不回显,留空沿用同名连接原密码。所属集群取环境包集群键。"""
        svc = service()
        plugin = find(svc, plugin_id)
        if plugin.auth != "connections":
            raise HTTPException(status_code=422, detail="该插件不按连接列表认证")
        clusters = set(load_env_pack(repo_root).cluster_keys())
        unknown = sorted({item.platform for item in body.connections} - clusters)
        if unknown:
            raise HTTPException(status_code=422, detail=f"未知集群: {', '.join(unknown)}")
        connections = [{
            **item.model_dump(exclude={"platform"}, exclude_none=True), "labels": {"platform": item.platform},
        } for item in body.connections]
        try:
            svc.credentials.save_plugin_connections(plugin_id, connections)
        except CredentialStoreError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return svc.view(plugin)

    @routes.put("/api/plugins/{plugin_id}/approval-secret")
    def put_approval_secret(plugin_id: str, body: ApprovalSecretBody,
                            _context: RequestContext = Depends(request_context)) -> dict:
        """写操作审批密钥(与插件服务目录的 approval.secret 同一串);只写不回显。"""
        svc = service()
        plugin = find(svc, plugin_id)
        if plugin.write is None:
            raise HTTPException(status_code=422, detail="该插件没有声明写操作,不需要审批密钥")
        try:
            svc.credentials.save_plugin_approval_secret(plugin_id, body.secret)
        except CredentialStoreError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return svc.view(plugin)

    @routes.post("/api/plugins/{plugin_id}/test")
    def test_plugin(plugin_id: str, _context: RequestContext = Depends(request_context)) -> dict:
        svc = service()
        plugin = find(svc, plugin_id)
        result = svc.connection_test(plugin)
        return {**_summary(result), "plugin": svc.view(svc.registry.get(plugin_id))}

    @routes.post("/api/plugins/test")
    def test_draft(body: DraftTestBody, _context: RequestContext = Depends(request_context)) -> dict:
        draft = Plugin(id="draft", title="待添加插件", url=body.url, source="custom", cluster=None,
                       auth=body.auth, header_name=body.header_name or "",
                       timeout_seconds=body.timeout_seconds, enabled=False)
        secret = (body.credential.model_dump(exclude_none=True) if body.credential else {})
        missing = [key for key in PLUGIN_SECRET_FIELDS.get(body.auth, ()) if not secret.get(key)]
        if missing:
            raise HTTPException(status_code=422, detail=f"测试需要填写: {', '.join(missing)}")
        try:
            check_plugin_endpoint(body.url, body.auth, body.header_name or "")
        except EnvPackError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _summary(service().connection_test(draft, secret, save=False))

    return routes


def _summary(result: dict) -> dict:
    """连接测试结果给页面:工具只回名称与读写,完整定义以插件页工具表为准。"""
    return {
        "ok": result["ok"], "message": result["message"], "error_code": result.get("error_code"),
        "health": result.get("health"), "manifest": result.get("manifest"), "version": result.get("version"),
        "tools": [{"name": item["name"], "read_only": item["read_only"]} for item in result.get("tools") or []],
        # 连接列表认证的逐条试连结果(版本、角色、只读、缺的权限或错误码);其他认证为 null
        "probe": result.get("probe"),
    }
