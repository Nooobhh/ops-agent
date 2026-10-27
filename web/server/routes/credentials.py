"""Credential profile settings and safe authentication probes.

凭据项(有哪些、字段、校验、登录测试)由环境包 ``credential_profiles`` 扩展声明,节点凭据生成由
``node_credentials`` 扩展实现;这里只做通用的存取、测试与展示,密钥只写不回显。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Union

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import Field

from lib.config import resolve_credentials_path
from lib.data_root import local_config_path
from lib.credential_probe import probe_ssh_connection
from lib.credentials import (
    CredentialMissing,
    CredentialProviderUnavailable,
    CredentialStore,
    CredentialStoreError,
    CredentialTestFailed,
    FileCredentials,
    node_credential_provider,
    profile_specs,
)
from lib.envpack import EnvPackError, load_env_pack
from web.server.context import RequestContext, request_context

from lib.triage_agent.models import StrictModel
from lib.triage_agent.targets import load_target

FIELD_VALUE_MAX = 4096
ProfileValues = Dict[str, Union[str, int, None]]


class SshTestBody(StrictModel):
    platform: str
    node: str = Field(min_length=1, max_length=128)


def router(*, repo_root: Path) -> APIRouter:
    routes = APIRouter()
    repo_root = Path(repo_root).resolve()

    def extensions() -> tuple:
        """凭据项与提供方按请求向环境包取:扩展出错只让账号页报错,不拖垮中台。"""
        try:
            pack = load_env_pack(repo_root)
            store = CredentialStore(
                local_config_path(repo_root, "auth-profiles.json"),
                legacy_credentials_path=local_config_path(repo_root, "credentials.json"),
                specs=profile_specs(pack),
            )
            return pack, store, node_credential_provider(pack)
        except EnvPackError as exc:
            raise HTTPException(status_code=500, detail=f"环境包凭据扩展不可用: {exc}") from exc

    def node_credentials() -> FileCredentials:
        return FileCredentials(
            resolve_credentials_path(repo_root),
            merge_home=True,
            profile_path=local_config_path(repo_root, "auth-profiles.json"),
        )

    def public_status() -> dict:
        pack, store, provider = extensions()
        status = store.public_status()["profiles"]
        result: Dict[str, Any] = {
            "schema_version": 2,
            "profiles": [{
                "id": spec.id,
                "title": spec.title,
                "description": spec.description,
                "fields": [{
                    "name": item.name, "label": item.label, "kind": item.kind,
                    "required": item.required, "help": item.help,
                } for item in spec.fields],
                "testable": spec.test is not None,
                **status[spec.id],
            } for spec in store.specs],
            "node_credentials": {
                "provider": None if provider is None else {
                    "title": provider.title,
                    "description": provider.description,
                    "generate_label": provider.generate_label,
                    "generate_confirm": provider.generate_confirm,
                },
                "nodes": [],
            },
            "ssh_test_platforms": [
                {"key": key, "label": pack.short_name(key)} for key in pack.cluster_keys("managed")
            ],
        }
        try:
            result["node_credentials"]["nodes"] = node_credentials().cache_status()
        except Exception:
            # Credential status is supplementary UI metadata.  A malformed
            # cache must not make the account profile endpoint leak details or
            # become unavailable.
            pass
        return result

    def updates_for(store: CredentialStore, profile_id: str, values: ProfileValues) -> Dict[str, Any]:
        try:
            spec = store.spec(profile_id)
        except CredentialStoreError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        allowed = {item.name for item in spec.fields} | set(spec.extra_fields)
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise HTTPException(status_code=422, detail=f"未知字段: {', '.join(unknown)}")
        if any(isinstance(value, str) and len(value) > FIELD_VALUE_MAX for value in values.values()):
            raise HTTPException(status_code=422, detail="字段值过长")
        return {key: value for key, value in values.items() if value not in (None, "")}

    @routes.get("/api/credentials")
    def get_credentials(
        _context: RequestContext = Depends(request_context),
    ) -> dict:
        try:
            return public_status()
        except CredentialStoreError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @routes.put("/api/credentials/profiles/{profile_id}")
    def put_profile(
        profile_id: str,
        values: ProfileValues = Body(default_factory=dict),
        _context: RequestContext = Depends(request_context),
    ) -> dict:
        _pack, store, _provider = extensions()
        updates = updates_for(store, profile_id, values)
        try:
            store.save_profile(profile_id, updates)
        except (CredentialStoreError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return public_status()

    @routes.delete("/api/credentials/profiles/{profile_id}")
    def delete_profile(
        profile_id: str,
        _context: RequestContext = Depends(request_context),
    ) -> dict:
        _pack, store, _provider = extensions()
        try:
            store.delete_profile(profile_id)
        except CredentialStoreError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return public_status()

    @routes.post("/api/credentials/profiles/{profile_id}/test")
    def test_profile(
        profile_id: str,
        values: ProfileValues = Body(default_factory=dict),
        _context: RequestContext = Depends(request_context),
    ) -> dict:
        _pack, store, _provider = extensions()
        overrides = updates_for(store, profile_id, values)
        spec = store.spec(profile_id)
        if spec.test is None:
            raise HTTPException(status_code=404, detail="该凭据项不支持登录测试")
        try:
            merged = store.merged_for_test(profile_id, overrides)
        except (CredentialStoreError, CredentialMissing):
            store.record_test(profile_id, ok=False, error_code="invalid_config")
            return _test_failure(profile_id, f"{spec.title} 配置不完整或格式不正确", "invalid_config")
        try:
            message = spec.test(merged, repo_root)
        except CredentialTestFailed as exc:
            store.record_test(profile_id, ok=False, error_code=exc.code)
            return _test_failure(profile_id, str(exc), exc.code)
        except Exception:
            store.record_test(profile_id, ok=False, error_code="login_failed")
            return _test_failure(profile_id, f"{spec.title} 登录失败，请检查账号与网络", "login_failed")
        store.record_test(profile_id, ok=True)
        return _test_success(profile_id, message)

    @routes.post("/api/credentials/node-credentials/generate")
    def generate_node_credentials(
        _context: RequestContext = Depends(request_context),
    ) -> dict:
        """Explicitly replace node credentials through the environment's provider."""
        _pack, _store, provider = extensions()
        if provider is None:
            raise HTTPException(status_code=404, detail="环境包未声明节点凭据提供方")
        try:
            result = provider.generate_all(node_credentials(), None, replace_existing=True)
        except CredentialProviderUnavailable as exc:
            raise HTTPException(status_code=422, detail=_safe_provider_error(provider.title, exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"{provider.title}生成失败") from exc

        generated = sorted(
            ip for ip, item in result.items() if item.get("ok") is True
        )
        failed = [
            {"ip": ip, "error": str(item.get("error") or "unknown")}
            for ip, item in sorted(result.items())
            if item.get("ok") is not True
        ]
        return {
            "schema_version": 1,
            "ok": bool(generated),
            "generated": generated,
            "failed": failed,
            "status": public_status()["node_credentials"],
        }

    @routes.post("/api/credentials/test/ssh")
    def test_ssh(
        body: SshTestBody,
        _context: RequestContext = Depends(request_context),
    ) -> dict:
        if body.platform not in load_env_pack(repo_root).cluster_keys("managed"):
            raise HTTPException(status_code=404, detail="SSH 平台不存在")
        try:
            platform = load_target(repo_root, body.platform)
        except (OSError, ValueError, KeyError) as exc:
            raise HTTPException(status_code=422, detail="平台拓扑不可读") from exc
        if body.node not in platform.nodes:
            raise HTTPException(status_code=422, detail="节点未在平台拓扑中登记")
        try:
            result = probe_ssh_connection(repo_root, body.platform, body.node)
        except Exception as exc:
            return {
                "schema_version": 1,
                "ok": False,
                "kind": "ssh",
                "platform": body.platform,
                "node": body.node,
                "error_code": type(exc).__name__,
                "message": "SSH 连接失败，请检查节点凭据、跳板和节点访问权限",
                "tested_at": _now_iso(),
            }
        ok = result.exit_code == 0 and not result.timed_out
        return {
            "schema_version": 1,
            "ok": ok,
            "kind": "ssh",
            "platform": body.platform,
            "node": body.node,
            "error_code": None if ok else "command_failed",
            "message": "SSH 连接成功" if ok else "SSH 连接已建立但探测命令失败",
            "tested_at": _now_iso(),
        }

    return routes


def _safe_provider_error(title: str, exc: BaseException) -> str:
    text = str(exc).lower()
    if "不存在" in str(exc) or "missing" in text:
        return f"{title}的认证配置不存在"
    if "不可用" in str(exc) or "unavailable" in text:
        return f"{title}的认证服务不可用"
    return f"{title}登录失败，请检查认证配置和网络"


def _test_success(profile_id: str, message: str) -> dict:
    return {
        "schema_version": 1,
        "ok": True,
        "kind": "profile",
        "profile_id": profile_id,
        "message": message,
        "tested_at": _now_iso(),
    }


def _test_failure(profile_id: str, message: str, code: str) -> dict:
    return {
        "schema_version": 1,
        "ok": False,
        "kind": "profile",
        "profile_id": profile_id,
        "error_code": code,
        "message": message,
        "tested_at": _now_iso(),
    }


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
