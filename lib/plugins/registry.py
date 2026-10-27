"""插件注册:环境包声明的官方插件 + 本机配置(地址覆盖、开关、自定义插件)。

本机配置 ``<数据根>/config/plugins.local.yaml`` 只经 PluginRegistry 加锁原子写;
凭据不在这里,见 ``lib.credentials`` 的 ``plugin:<id>`` profile。
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

import yaml

from lib.atomic_file import atomic_write_text
from lib.data_root import local_config_path
from lib.envpack import (
    PLUGIN_ID_RE,
    EnvPackError,
    PluginDecl,
    PluginScopeDecl,
    PluginWriteDecl,
    check_plugin_endpoint,
    load_env_pack,
)
from lib.file_lock import FileLock

LOCAL_FILE = "plugins.local.yaml"
DEFAULT_TIMEOUT_SECONDS = 30.0
TIMEOUT_RANGE = (5.0, 300.0)
TITLE_MAX = 64


class PluginConfigError(ValueError):
    pass


class PluginNotFound(PluginConfigError):
    pass


@dataclass(frozen=True)
class ToolSetting:
    enabled: bool
    # 第三方工具启用时钉住的内容哈希(描述 + 参数 + 读写);内容变了即失效待复核
    pinned_hash: str = ""


@dataclass(frozen=True)
class Plugin:
    id: str
    title: str
    url: str
    source: str  # official | custom
    cluster: Optional[str]
    auth: str
    header_name: str
    timeout_seconds: float
    enabled: bool
    tool_settings: Mapping[str, ToolSetting] = field(default_factory=dict)
    default_disabled: Tuple[str, ...] = ()
    default_url: str = ""
    # 两阶段写的工具配对,只有官方插件(环境包声明)才有
    write: Optional[PluginWriteDecl] = None
    # 全局插件按标签区分集群、后台采集角色,同样只有官方插件才有
    scope: Optional[PluginScopeDecl] = None
    feeds: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    connection_probe: str = ""

    @property
    def trusted(self) -> bool:
        return self.source == "official"

    @property
    def origin(self) -> str:
        parts = urlsplit(self.url)
        return f"{parts.scheme}://{parts.netloc}"

    def tool_enabled(self, name: str, content_hash: str, agent_default: bool = True) -> bool:
        setting = self.tool_settings.get(name)
        if self.trusted:
            # 默认开关:环境包 default_disabled 与插件 manifest 的 agent_default=false 都表示默认不给 Agent
            return setting.enabled if setting is not None else (agent_default and name not in self.default_disabled)
        # 第三方:必须人工逐个启用,且内容与启用时一致
        return bool(setting and setting.enabled and setting.pinned_hash == content_hash)


class PluginRegistry:
    def __init__(self, repo_root: Path):
        self.repo_root = Path(repo_root)
        self.path = local_config_path(self.repo_root, LOCAL_FILE)
        self._lock_path = self.path.with_name(f".{self.path.name}.lock")

    # ---- 读 ----

    def plugins(self) -> List[Plugin]:
        local = self._load()
        official = [self._official(decl, local.get(decl.id) or {}) for decl in self._decls()]
        known = {item.id for item in official}
        custom = [
            self._custom(plugin_id, entry) for plugin_id, entry in sorted(local.items())
            if plugin_id not in known and isinstance(entry, dict) and entry.get("custom")
        ]
        return official + custom

    def get(self, plugin_id: str) -> Plugin:
        for plugin in self.plugins():
            if plugin.id == plugin_id:
                return plugin
        raise PluginNotFound(f"插件不存在: {plugin_id}")

    def feed(self, kind: str) -> Plugin:
        """声明了某类后台采集(alerts / resources)的官方插件;不看插件开关(开关只管 Agent 工具)。"""
        found = [plugin for plugin in self.plugins() if kind in plugin.feeds]
        if len(found) != 1:
            raise PluginNotFound(f"环境包须恰有一个插件声明 feeds.{kind},现有 {len(found)} 个")
        return found[0]

    # ---- 写 ----

    def create_custom(self, plugin_id: str, *, title: str, url: str, auth: str, header_name: str = "",
                      cluster: Optional[str] = None,
                      timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> Plugin:
        if not PLUGIN_ID_RE.match(plugin_id or ""):
            raise PluginConfigError("插件 id 须为小写字母开头、仅含小写字母数字与连字符,最长 24")
        with self._locked() as local:
            if plugin_id in local or plugin_id in {decl.id for decl in self._decls()}:
                raise PluginConfigError(f"插件 id 已存在: {plugin_id}")
            entry = {"custom": True, "enabled": False, "tools": {}}
            local[plugin_id] = self._apply(entry, custom=True, title=title, url=url, auth=auth,
                                           header_name=header_name, cluster=cluster,
                                           timeout_seconds=timeout_seconds)
        return self.get(plugin_id)

    def update(self, plugin_id: str, **changes: Any) -> Plugin:
        plugin = self.get(plugin_id)
        allowed = {"url", "timeout_seconds", "enabled"} | (
            set() if plugin.trusted else {"title", "auth", "header_name", "cluster"}
        )
        unknown = set(changes) - allowed
        if unknown:
            raise PluginConfigError(f"{'官方' if plugin.trusted else '自定义'}插件不可修改: {', '.join(sorted(unknown))}")
        with self._locked() as local:
            entry = dict(local.get(plugin_id) or {})
            local[plugin_id] = self._apply(entry, custom=not plugin.trusted, **changes)
        return self.get(plugin_id)

    def set_tool(self, plugin_id: str, tool: str, *, enabled: bool, content_hash: str = "") -> Plugin:
        plugin = self.get(plugin_id)
        with self._locked() as local:
            entry = dict(local.get(plugin_id) or {})
            tools = dict(entry.get("tools") or {})
            setting: Dict[str, Any] = {"enabled": bool(enabled)}
            if not plugin.trusted and enabled:
                setting["pinned_hash"] = content_hash
            tools[tool] = setting
            entry["tools"] = tools
            local[plugin_id] = entry
        return self.get(plugin_id)

    def reset_official(self, plugin_id: str) -> Plugin:
        if not self.get(plugin_id).trusted:
            raise PluginConfigError("只有官方插件可恢复默认")
        with self._locked() as local:
            local.pop(plugin_id, None)
        return self.get(plugin_id)

    def delete_custom(self, plugin_id: str) -> None:
        if self.get(plugin_id).trusted:
            raise PluginConfigError("官方插件不可删除,可停用或恢复默认")
        with self._locked() as local:
            local.pop(plugin_id, None)

    # ---- 内部 ----

    def _decls(self) -> Tuple[PluginDecl, ...]:
        return load_env_pack(self.repo_root).plugins

    def _cluster_keys(self) -> set:
        return set(load_env_pack(self.repo_root).cluster_keys())

    def _official(self, decl: PluginDecl, entry: Mapping[str, Any]) -> Plugin:
        return Plugin(
            id=decl.id,
            title=decl.title or decl.id,
            url=str(entry.get("url") or decl.url),
            source="official",
            cluster=decl.cluster,
            auth=decl.auth,
            header_name=decl.header_name,
            timeout_seconds=float(entry.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS),
            enabled=bool(entry.get("enabled")),
            tool_settings=_tool_settings(entry),
            default_disabled=decl.default_disabled,
            default_url=decl.url,
            write=decl.write,
            scope=decl.scope,
            feeds=decl.feeds,
            connection_probe=decl.connection_probe,
        )

    @staticmethod
    def _custom(plugin_id: str, entry: Mapping[str, Any]) -> Plugin:
        return Plugin(
            id=plugin_id,
            title=str(entry.get("title") or plugin_id),
            url=str(entry.get("url") or ""),
            source="custom",
            cluster=entry.get("cluster") or None,
            auth=str(entry.get("auth") or "none"),
            header_name=str(entry.get("header_name") or ""),
            timeout_seconds=float(entry.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS),
            enabled=bool(entry.get("enabled")),
            tool_settings=_tool_settings(entry),
        )

    def _apply(self, entry: Dict[str, Any], *, custom: bool, **changes: Any) -> Dict[str, Any]:
        merged = {**entry, **{key: value for key, value in changes.items() if value is not None}}
        if "cluster" in changes and changes["cluster"] in ("", None):
            merged.pop("cluster", None)
        if "timeout_seconds" in merged:
            timeout = float(merged["timeout_seconds"])
            if not TIMEOUT_RANGE[0] <= timeout <= TIMEOUT_RANGE[1]:
                raise PluginConfigError(f"超时须在 {TIMEOUT_RANGE[0]:.0f}~{TIMEOUT_RANGE[1]:.0f} 秒")
            merged["timeout_seconds"] = timeout
        if custom:
            title = str(merged.get("title") or "").strip()
            if not title or len(title) > TITLE_MAX:
                raise PluginConfigError(f"插件名称必填,最长 {TITLE_MAX} 字")
            merged["title"] = title
            if merged.get("cluster") and merged["cluster"] not in self._cluster_keys():
                raise PluginConfigError(f"未知集群: {merged['cluster']}")
            auth, header_name = str(merged.get("auth") or "none"), str(merged.get("header_name") or "")
            if auth != "header":
                header_name = ""
                merged.pop("header_name", None)
            self._check_endpoint(str(merged.get("url") or ""), auth, header_name)
        elif "url" in changes and changes["url"]:
            self._check_endpoint(str(changes["url"]), "none", "")
        return merged

    @staticmethod
    def _check_endpoint(url: str, auth: str, header_name: str) -> None:
        try:
            check_plugin_endpoint(url, auth, header_name)
        except EnvPackError as exc:
            raise PluginConfigError(str(exc)) from None

    def _load(self) -> Dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise PluginConfigError(f"{self.path.name} 不可读: {exc}") from exc
        plugins = data.get("plugins") if isinstance(data, dict) else None
        if plugins is None:
            return {}
        if not isinstance(plugins, dict):
            raise PluginConfigError(f"{self.path.name}: plugins 必须是映射")
        return dict(plugins)

    @contextmanager
    def _locked(self) -> Iterator[Dict[str, Any]]:
        """加锁读出本机配置,块内原地修改,正常退出才原子写回。"""
        with FileLock(str(self._lock_path)):
            local = self._load()
            yield local
            atomic_write_text(self.path, yaml.safe_dump(
                {"version": 1, "plugins": local}, allow_unicode=True, sort_keys=True,
            ))


def _tool_settings(entry: Mapping[str, Any]) -> Dict[str, ToolSetting]:
    raw = entry.get("tools") or {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(name): ToolSetting(enabled=bool(value.get("enabled")), pinned_hash=str(value.get("pinned_hash") or ""))
        for name, value in raw.items() if isinstance(value, dict)
    }
