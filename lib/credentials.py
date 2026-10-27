"""节点 SSH 凭据与本机认证资料。

核心只管通用部分:账号页凭据项的存取(具体有哪些项、怎么校验/测试由环境包 ``credential_profiles``
扩展声明)、节点凭据缓存的读取与展示。节点凭据怎么生成(如堡垒机签发)由环境包
``node_credentials`` 扩展实现;SSHPool 只读缓存,从不触发生成。
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Protocol, Tuple

from lib.credential_cache import CredentialCache
from lib.envpack import EnvPack, EnvPackError, active_env_pack
from lib.envpack.extensions import extension_decl, load_object
from lib.file_lock import FileLock


class CredentialUnavailable(Exception):
    """Base class for a credential that cannot currently be returned."""


class CredentialMissing(CredentialUnavailable):
    def __init__(self, host: str) -> None:
        self.host = host
        super().__init__(f"credentials.json 缺少 host {host} 的凭证")


class CredentialExpiring(CredentialUnavailable):
    def __init__(self, host: str, valid_to: int) -> None:
        self.host = host
        self.valid_to = valid_to
        super().__init__(f"host {host} 的凭证即将过期(valid_to={valid_to})")


class CredentialProviderUnavailable(Exception):
    """节点凭据提供方(如堡垒机)的认证资料或服务不可用;source 为提示用的来源名。"""
    classification: Optional[str] = None

    def __init__(self, source=None, cause=None) -> None:
        self.source = str(source or "节点凭据提供方")
        self.cause = cause
        if isinstance(cause, FileNotFoundError):
            message = f"{self.source} 不存在"
        else:
            detail = str(cause) if cause else "认证配置不可用"
            message = f"{self.source} 不可用: {detail}"
        if self.classification:
            message = f"{message} · 分类={self.classification}"
        super().__init__(message)


class ProviderAuthMissing(CredentialProviderUnavailable):
    classification = "auth-missing"


class ProviderAuthMalformed(CredentialProviderUnavailable):
    classification = "auth-malformed"


class ProviderLoginFailed(CredentialProviderUnavailable):
    classification = "login-failed"


class CredentialGenerateFailed(Exception):
    def __init__(self, host: str, cause) -> None:
        self.host = host
        self.cause = cause
        super().__init__(f"生成 {host} 的节点凭证失败: {cause}")


class CredentialStoreError(RuntimeError):
    """A credential profile cannot be safely read or written."""


class CredentialTestFailed(Exception):
    """账号页登录测试失败;message 给人看(不得含密钥),code 为稳定错误码。"""

    def __init__(self, message: str, code: str = "login_failed") -> None:
        self.code = code
        super().__init__(message)


PROFILE_SCHEMA_VERSION = 1
# 插件(MCP 服务)凭据按认证方式需要的字段;none 不存凭据
PLUGIN_SECRET_FIELDS = {"basic": ("user", "password"), "bearer": ("token",), "header": ("value",)}
APPROVAL_PROFILE_PREFIX = "plugin-approval:"
# 连接列表认证(插件页逐条编辑的连接,中台持有并按插件声明的请求头传给插件)
CONNECTIONS_MAX = 32
CONNECTION_NAME_RE = re.compile(r"^[a-z0-9-]{1,32}$")
CONNECTION_LABEL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
CONNECTION_LABEL_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:/@+-]{1,64}$")
CONNECTION_DATABASE_RE = re.compile(r"^[A-Za-z0-9_$-]{1,64}$")
APPROVAL_SECRET_MIN, APPROVAL_SECRET_MAX = 16, 1024
PROFILE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}(?::[a-z0-9_-]{1,32})?$")
FIELD_KINDS = ("text", "secret", "number")


@dataclass(frozen=True)
class CredentialField:
    """账号页表单的一个字段;secret 字段只写不回显。"""
    name: str
    label: str
    kind: str = "text"
    required: bool = True
    help: str = ""


@dataclass(frozen=True)
class CredentialProfileSpec:
    """环境包声明的一个账号页凭据项。

    validate:合并后的完整值 → 规范化后落盘的值,不合法抛 ValueError/CredentialStoreError;
    test:(规范化值, 代码仓根) → 成功提示语,失败抛 CredentialTestFailed;
    legacy_file:迁移期的旧单文件 JSON(只在新 profile 不存在时读);
    extra_fields:表单不展示、但 API 可写的字段(高级配置)。
    """
    id: str
    title: str
    fields: Tuple[CredentialField, ...]
    validate: Callable[[Dict[str, Any]], Dict[str, Any]]
    description: str = ""
    username_field: str = ""
    test: Optional[Callable[[Dict[str, Any], Path], str]] = None
    legacy_file: Optional[Path] = None
    extra_fields: Tuple[str, ...] = ()


def profile_specs(pack: Optional[EnvPack] = None) -> Tuple[CredentialProfileSpec, ...]:
    """环境包 ``credential_profiles`` 扩展(``(pack) -> 凭据项列表``)声明的凭据项;未声明即无。"""
    pack = pack or active_env_pack()
    ref = extension_decl(pack, "credential_profiles")
    if ref is None:
        return ()
    factory = load_object(pack, ref)
    specs = tuple(factory(pack))
    ids = [spec.id for spec in specs]
    for spec in specs:
        if not isinstance(spec, CredentialProfileSpec) or not PROFILE_ID_RE.match(spec.id) \
                or spec.id.startswith("plugin"):
            raise EnvPackError(f"扩展 {ref} 返回了非法凭据项: {spec!r}")
        if any(item.kind not in FIELD_KINDS for item in spec.fields):
            raise EnvPackError(f"凭据项 {spec.id} 字段类型只能是 {', '.join(FIELD_KINDS)}")
    if len(ids) != len(set(ids)):
        raise EnvPackError(f"扩展 {ref} 返回的凭据项 id 重复")
    return specs


class NodeCredentialProvider(Protocol):
    """节点凭据提供方(环境包 ``node_credentials`` 扩展):只在人工动作或登录初始化时生成凭据。

    initialize:只为本地缓存缺失的节点生成,不替换已有凭据;generate_all:显式替换。
    两者都返回 {节点地址: {"ok": bool, "status"|"error": ...}}。
    """
    title: str
    description: str
    generate_label: str
    generate_confirm: str

    def initialize(self, credentials: "FileCredentials", ip_prefix: Optional[str] = None) -> Dict[str, dict]: ...

    def generate_all(self, credentials: "FileCredentials", ip_prefix: Optional[str] = None,
                     *, replace_existing: bool = True) -> Dict[str, dict]: ...


def node_credential_provider(pack: Optional[EnvPack] = None) -> Optional[NodeCredentialProvider]:
    pack = pack or active_env_pack()
    ref = extension_decl(pack, "node_credentials")
    return load_object(pack, ref) if ref is not None else None


class CredentialStore:
    """Machine-local auth profiles with legacy credential compatibility.

    Auth profiles live in a separate ignored file from the dynamic node cache.
    The storage boundary is deliberately small so a future keychain or secret
    manager backend can replace this file without changing ActionGateway or
    SSHPool.  Secrets are never returned by ``public_status``.
    """

    def __init__(
        self,
        path: Path,
        *,
        legacy_credentials_path: Optional[Path] = None,
        merge_home: bool = True,
        specs: Optional[Tuple[CredentialProfileSpec, ...]] = None,
    ) -> None:
        self.path = Path(path).expanduser()
        self.legacy_credentials_path = (
            Path(legacy_credentials_path).expanduser()
            if legacy_credentials_path is not None
            else self.path.parent / "credentials.json"
        )
        self.merge_home = bool(merge_home)
        self.lock_path = Path(f"{self.path}.lock")
        self._specs = specs

    @property
    def specs(self) -> Tuple[CredentialProfileSpec, ...]:
        if self._specs is None:
            self._specs = profile_specs()
        return self._specs

    def spec(self, profile_id: str) -> CredentialProfileSpec:
        found = next((item for item in self.specs if item.id == profile_id), None)
        if found is None:
            raise CredentialStoreError(f"未知凭证 profile: {profile_id}")
        return found

    # ---- public reads ----

    def get_profile(self, profile_id: str) -> Dict[str, Any]:
        """已保存(或旧格式迁移来)的凭据项,经声明的校验规范化;缺失抛 CredentialMissing。"""
        spec = self.spec(profile_id)
        raw = self._profile(profile_id)
        if raw is None:
            raw = self._legacy(spec)
        if raw is None:
            raise CredentialMissing(profile_id)
        return self._validated(spec, raw)

    def public_status(self) -> Dict[str, Any]:
        profiles: Dict[str, Any] = {}
        for spec in self.specs:
            raw = self._profile(spec.id)
            if raw is None:
                try:
                    raw = self._legacy(spec)
                except CredentialStoreError:
                    raw = None
            try:
                configured = isinstance(raw, dict) and bool(self._validated(spec, raw))
            except CredentialStoreError:
                configured = False
            username = raw.get(spec.username_field) if isinstance(raw, dict) and spec.username_field else None
            profiles[spec.id] = {
                "configured": configured,
                "username_hint": _mask_username(str(username)) if username else None,
                "last_test": self._last_test(spec.id),
            }
        return {"schema_version": 1, "profiles": profiles}

    # ---- writes ----

    def save_profile(self, profile_id: str, updates: Dict[str, Any]) -> None:
        spec = self.spec(profile_id)
        self._save_profile(profile_id, updates, lambda value: self._validated(spec, value), legacy=spec)

    def merged_for_test(self, profile_id: str, overrides: Dict[str, Any]) -> Dict[str, Any]:
        """登录测试用:已保存值叠加本次输入后校验,不落盘。"""
        spec = self.spec(profile_id)
        try:
            current = dict(self._profile(profile_id) or self._legacy(spec) or {})
        except CredentialStoreError:
            current = {}
        current.update({key: value for key, value in overrides.items() if value is not None})
        return self._validated(spec, current)

    def delete_profile(self, profile_id: str) -> None:
        self.spec(profile_id)
        with FileLock(str(self.lock_path)):
            data = self._load_unlocked()
            profiles = data.setdefault("profiles", {})
            profiles.pop(profile_id, None)
            metadata = data.setdefault("metadata", {})
            metadata.pop(profile_id, None)
            self._write_unlocked(data)

    def record_test(self, profile_id: str, *, ok: bool, error_code: Optional[str] = None) -> None:
        if profile_id not in {item.id for item in self.specs}:
            return
        self._record_test(profile_id, ok=ok, error_code=error_code)

    # ---- 插件凭据:plugin:<id>,只经插件页读写,不进账号页的 profile 清单 ----

    def get_plugin_secret(self, plugin_id: str, scheme: str) -> Dict[str, str]:
        profile = self._profile(f"plugin:{plugin_id}")
        if profile is None or profile.get("scheme") != scheme:
            raise CredentialMissing(f"plugin:{plugin_id}")
        return self._validate_plugin(profile, scheme)

    def plugin_status(self, plugin_id: str, scheme: str) -> Dict[str, Any]:
        profile_id = f"plugin:{plugin_id}"
        raw = self._profile(profile_id) or {}
        configured = scheme == "none"
        if not configured and raw.get("scheme") == scheme:
            try:
                self._validate_plugin(raw, scheme)
                configured = True
            except CredentialStoreError:
                configured = False
        user = raw.get("user") if scheme == "basic" and raw.get("scheme") == scheme else None
        return {
            "configured": configured,
            "username_hint": _mask_username(str(user)) if user else None,
            "last_test": self._last_test(profile_id),
        }

    def save_plugin(self, plugin_id: str, scheme: str, updates: Dict[str, Any]) -> None:
        if scheme not in PLUGIN_SECRET_FIELDS:
            raise CredentialStoreError("该插件的认证方式不需要凭据")
        self._save_profile(
            f"plugin:{plugin_id}", {**updates, "scheme": scheme},
            lambda value: self._validate_plugin(value, scheme),
        )

    def plugin_connections_view(self, plugin_id: str) -> List[Dict[str, Any]]:
        """连接列表的可展示部分:不回密码,账号打码。"""
        try:
            connections = self.get_plugin_secret(plugin_id, "connections")["connections"]
        except (CredentialMissing, CredentialStoreError):
            return []
        return [{
            "name": item["name"], "host": item["host"], "port": item["port"],
            "user_hint": _mask_username(item["user"]), "database": item.get("database"),
            "labels": dict(item["labels"]), "password_set": True,
        } for item in connections]

    def save_plugin_connections(self, plugin_id: str, connections: List[Dict[str, Any]]) -> None:
        """整表替换连接列表;某条账号 / 密码留空即沿用同名连接的原值,新连接必须都给。"""
        profile_id = f"plugin:{plugin_id}"
        with FileLock(str(self.lock_path)):
            data = self._load_unlocked()
            current = dict(data.setdefault("profiles", {}).get(profile_id) or {})
            existing = {
                str(item.get("name")): item for item in current.get("connections") or []
                if isinstance(item, dict)
            } if current.get("scheme") == "connections" else {}
            merged = []
            for raw in connections:
                item = dict(raw)
                for key, label in (("user", "账号"), ("password", "密码")):
                    if item.get(key):
                        continue
                    previous = existing.get(str(item.get("name") or ""))
                    if not previous:
                        raise CredentialStoreError(f"连接 {item.get('name') or '(未命名)'} 需要填写{label}")
                    item[key] = previous.get(key)
                merged.append(item)
            data["profiles"][profile_id] = self._validate_connections({"connections": merged})
            self._write_unlocked(data)

    def delete_plugin(self, plugin_id: str) -> None:
        with FileLock(str(self.lock_path)):
            data = self._load_unlocked()
            for profile_id in (f"plugin:{plugin_id}", f"{APPROVAL_PROFILE_PREFIX}{plugin_id}"):
                data.setdefault("profiles", {}).pop(profile_id, None)
                data.setdefault("metadata", {}).pop(profile_id, None)
            self._write_unlocked(data)

    # ---- 写操作审批密钥:plugin-approval:<id>,与登录凭据分开(与认证方式无关),只写不读回 ----

    def plugin_approval_secret(self, plugin_id: str) -> str:
        profile = self._profile(f"{APPROVAL_PROFILE_PREFIX}{plugin_id}")
        if profile is None:
            raise CredentialMissing(f"{APPROVAL_PROFILE_PREFIX}{plugin_id}")
        return self._validate_approval_secret(profile)["secret"]

    def plugin_approval_configured(self, plugin_id: str) -> bool:
        try:
            self.plugin_approval_secret(plugin_id)
        except CredentialUnavailable:
            return False
        except CredentialStoreError:
            return False
        return True

    def save_plugin_approval_secret(self, plugin_id: str, secret: str) -> None:
        self._save_profile(
            f"{APPROVAL_PROFILE_PREFIX}{plugin_id}", {"secret": secret}, self._validate_approval_secret,
        )

    def record_plugin_test(self, plugin_id: str, *, ok: bool, error_code: Optional[str] = None) -> None:
        self._record_test(f"plugin:{plugin_id}", ok=ok, error_code=error_code)

    def _record_test(self, profile_id: str, *, ok: bool, error_code: Optional[str]) -> None:
        with FileLock(str(self.lock_path)):
            data = self._load_unlocked()
            metadata = data.setdefault("metadata", {})
            metadata[profile_id] = {
                "status": "success" if ok else "failure",
                "error_code": error_code if not ok else None,
                "tested_at": datetime.now(timezone.utc).isoformat(),
            }
            self._write_unlocked(data)

    # ---- storage ----

    def _profile(self, profile_id: str) -> Optional[Dict[str, Any]]:
        data = self._load_unlocked()
        profiles = data.get("profiles")
        if not isinstance(profiles, dict):
            return None
        value = profiles.get(profile_id)
        return dict(value) if isinstance(value, dict) else None

    def _last_test(self, profile_id: str) -> Optional[Dict[str, Any]]:
        metadata = self._load_unlocked().get("metadata")
        value = metadata.get(profile_id) if isinstance(metadata, dict) else None
        return dict(value) if isinstance(value, dict) else None

    def _legacy_entries(self) -> Dict[str, Any]:
        merged: Dict[str, Any] = {}
        paths = [self.legacy_credentials_path]
        if self.merge_home:
            paths.append(Path.home() / ".ops-agent" / "credentials.json")
        seen = set()
        for path in paths:
            if path in seen or path == self.path:
                continue
            seen.add(path)
            try:
                raw = path.read_text(encoding="utf-8")
                value = json.loads(raw)
            except (FileNotFoundError, OSError, ValueError):
                continue
            if isinstance(value, dict):
                merged.update(value)
        return merged

    def _legacy(self, spec: CredentialProfileSpec) -> Optional[Dict[str, Any]]:
        """迁移兼容:旧扁平 credentials.json 里同名键优先,其次凭据项声明的旧单文件。"""
        flat = self._legacy_entries().get(spec.id)
        if isinstance(flat, dict):
            return dict(flat)
        if spec.legacy_file is None:
            return None
        path = Path(spec.legacy_file).expanduser()
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise CredentialStoreError(f"旧凭证文件不可读: {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise CredentialStoreError(f"旧凭证文件顶层必须是 JSON object: {path}")
        return dict(value)

    @staticmethod
    def _validated(spec: CredentialProfileSpec, value: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return spec.validate(dict(value))
        except CredentialStoreError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise CredentialStoreError(f"{spec.title} 凭证不完整或格式不正确: {exc}") from exc

    def _load_unlocked(self) -> Dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"schema_version": PROFILE_SCHEMA_VERSION, "profiles": {}, "metadata": {}}
        except (OSError, ValueError) as exc:
            raise CredentialStoreError(f"凭证 profile 文件不可读: {self.path}") from exc
        if not isinstance(value, dict) or value.get("schema_version") != PROFILE_SCHEMA_VERSION:
            raise CredentialStoreError("凭证 profile schema_version 无效")
        if not isinstance(value.get("profiles"), dict) or not isinstance(value.get("metadata"), dict):
            raise CredentialStoreError("凭证 profile 结构无效")
        return value

    def _save_profile(self, profile_id: str, updates: Dict[str, Any], validator,
                      *, legacy: Optional[CredentialProfileSpec] = None) -> None:
        if not isinstance(updates, dict):
            raise CredentialStoreError("凭证更新必须是 object")
        with FileLock(str(self.lock_path)):
            data = self._load_unlocked()
            current = dict(data.setdefault("profiles", {}).get(profile_id) or {})
            if not current and legacy is not None:
                current.update(self._legacy(legacy) or {})
            for key, value in updates.items():
                if value is not None:
                    current[key] = value
            normalized = validator(current)
            data["profiles"][profile_id] = normalized
            self._write_unlocked(data)

    def _write_unlocked(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent),
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.remove(tmp_name)
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _validate_user_password(value: Dict[str, Any], label: str) -> Dict[str, Any]:
        user = str(value.get("user") or "").strip()
        password = str(value.get("password") or "")
        if not user or not password:
            raise CredentialStoreError(f"{label} 凭证必须包含 user 和 password")
        return {"user": user, "password": password}

    @staticmethod
    def _validate_plugin(value: Dict[str, Any], scheme: str) -> Dict[str, Any]:
        if scheme == "connections":
            return CredentialStore._validate_connections(value)
        result = {"scheme": scheme}
        for key in PLUGIN_SECRET_FIELDS.get(scheme, ()):
            text = str(value.get(key) or "")
            text = text.strip() if key == "user" else text
            # 值要进 HTTP 请求头:控制字符会被拒或造成头注入
            if not text or any(ord(char) < 32 for char in text):
                raise CredentialStoreError(f"插件凭据缺少或含非法字符: {key}")
            result[key] = text
        return result

    @staticmethod
    def _validate_connections(value: Dict[str, Any]) -> Dict[str, Any]:
        items = value.get("connections")
        if not isinstance(items, list) or not items:
            raise CredentialStoreError("至少配置一条连接")
        if len(items) > CONNECTIONS_MAX:
            raise CredentialStoreError(f"连接最多 {CONNECTIONS_MAX} 条")
        result, names = [], set()
        for raw in items:
            if not isinstance(raw, dict):
                raise CredentialStoreError("连接必须是对象")
            name = str(raw.get("name") or "").strip()
            if not CONNECTION_NAME_RE.match(name) or name in names:
                raise CredentialStoreError("连接名须为小写字母、数字与连字符,不超过 32 字且不重复")
            names.add(name)
            host = str(raw.get("host") or "").strip()
            if not host or len(host) > 253 or any(char.isspace() or ord(char) < 32 for char in host):
                raise CredentialStoreError(f"连接 {name} 的地址不合法")
            try:
                port = int(raw.get("port") or 3306)
            except (TypeError, ValueError):
                raise CredentialStoreError(f"连接 {name} 的端口不合法") from None
            if not 1 <= port <= 65535:
                raise CredentialStoreError(f"连接 {name} 的端口不合法")
            user, password = str(raw.get("user") or "").strip(), str(raw.get("password") or "")
            if not user or not password or any(ord(char) < 32 for char in user + password):
                raise CredentialStoreError(f"连接 {name} 缺少账号或密码,或含非法字符")
            database = str(raw.get("database") or "").strip() or None
            if database and not CONNECTION_DATABASE_RE.match(database):
                raise CredentialStoreError(f"连接 {name} 的库名不合法")
            labels = raw.get("labels") if isinstance(raw.get("labels"), dict) else {}
            labels = {str(key): str(item) for key, item in labels.items()}
            if not labels.get("platform") or not all(
                CONNECTION_LABEL_RE.match(key) and CONNECTION_LABEL_VALUE_RE.match(item)
                for key, item in labels.items()
            ):
                raise CredentialStoreError(f"连接 {name} 须带所属集群(platform),标签只许字母数字与 _ . : / @ + -")
            result.append({
                "name": name, "host": host, "port": port, "user": user, "password": password,
                **({"database": database} if database else {}), "labels": labels,
            })
        return {"scheme": "connections", "connections": result}

    @staticmethod
    def _validate_approval_secret(value: Dict[str, Any]) -> Dict[str, str]:
        # 插件按「文件内容去首尾空白后的字节」作 HMAC 密钥;中间不允许空白与非 ASCII,免得两边各自理解
        secret = str(value.get("secret") or "").strip()
        if not APPROVAL_SECRET_MIN <= len(secret) <= APPROVAL_SECRET_MAX \
                or any(not (33 <= ord(char) <= 126) for char in secret):
            raise CredentialStoreError(
                f"审批密钥须为 {APPROVAL_SECRET_MIN}~{APPROVAL_SECRET_MAX} 位可见 ASCII 字符(建议 openssl rand -hex 32)"
            )
        return {"secret": secret}


def _mask_username(value: str) -> str:
    if len(value) <= 3:
        return "***"
    return f"{value[:2]}***{value[-1]}"


@dataclass
class Credential:
    user: str
    password: Optional[str] = None
    host: Optional[str] = None
    port: int = 22
    key_file: Optional[str] = None
    pty: bool = False
    mode: Optional[int] = None
    valid_to: int = 0


class FileCredentials:
    """节点凭据:本地缓存读取与展示;生成交给环境包的节点凭据提供方(人工动作或登录初始化)。"""

    def __init__(
        self,
        path: Path,
        merge_home: bool = True,
        *,
        profile_path: Optional[Path] = None,
    ) -> None:
        self.path = Path(path).expanduser()
        self.merge_home = bool(merge_home)
        self._cache = CredentialCache(self.path)
        self.profiles = CredentialStore(
            profile_path or self.path.parent / "auth-profiles.json",
            legacy_credentials_path=self.path,
            merge_home=self.merge_home,
        )

    def get(self, host: str) -> Credential:
        entry = self._load_merged().get(host)
        if entry is None:
            raise CredentialMissing(host)
        # Expiry is display/diagnostic metadata only.  SSH use must not turn
        # a validity check into an implicit token refresh or replacement.
        return self._to_credential(entry)

    @contextmanager
    def host_lock(self, host: str) -> Iterator[None]:
        """同一节点的生成与写缓存互斥(per-IP 锁,保护人工写侧)。"""
        with FileLock(f"{self.path}.{host}.refresh.lock", timeout=30.0):
            yield

    def store(self, host: str, credential: Dict[str, Any]) -> Credential:
        """提供方生成后写入缓存;调用方须持有 ``host_lock``。"""
        self._cache.set(host, credential)
        cached = self._cache.get(host)
        assert cached is not None
        return self._to_credential(cached)

    def cache_status(self) -> list[dict]:
        """Return redacted node credential validity metadata for the UI."""
        now = int(time.time())
        status: list[dict] = []
        for host, record in sorted(self._load_merged().items()):
            if not isinstance(record, dict):
                continue
            try:
                ipaddress.ip_address(str(host))
            except ValueError:
                continue
            if not (
                record.get("target_ip")
                or record.get("uni_id")
                or record.get("acc_id")
                or record.get("pty")
            ):
                continue
            valid_from = int(record.get("valid_from") or 0)
            valid_to = int(record.get("valid_to") or 0)
            mode = record.get("mode")
            mode = int(mode) if mode in {0, 1, "0", "1"} else None
            if valid_to > 0 and valid_to <= now:
                state = "expired"
            elif mode == 0:
                state = "long_lived"
            elif valid_to > 0:
                state = "active"
            else:
                state = "unknown"
            status.append({
                "host": str(host),
                "target": record.get("target", host),
                "cached_at": record.get("cached_at", ""),
                "mode": mode,
                "valid_from": valid_from,
                "valid_to": valid_to,
                "state": state,
            })
        return status

    def delete(self, host: str) -> None:
        self._cache.delete(host)

    def _load_merged(self) -> Dict[str, dict]:
        merged: Dict[str, dict] = {}
        project = self._cache._load()
        merged.update(project)
        home_path = Path.home() / ".ops-agent" / "credentials.json"
        if self.merge_home and home_path != self.path and home_path.exists():
            try:
                home_data = json.loads(home_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                home_data = {}
            if isinstance(home_data, dict):
                merged.update(home_data)
        return merged

    @staticmethod
    def _to_credential(entry: dict) -> Credential:
        key_file = entry.get("key_file")
        return Credential(
            user=entry["user"],
            password=entry.get("password"),
            host=entry.get("host"),
            port=int(entry.get("port", 22)),
            key_file=str(Path(key_file).expanduser()) if key_file else None,
            pty=bool(entry.get("pty", False)),
            mode=(
                int(entry["mode"])
                if entry.get("mode") in {0, 1, "0", "1"}
                else None
            ),
            valid_to=int(entry.get("valid_to") or 0),
        )
