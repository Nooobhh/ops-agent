"""环境包扩展面向的核心 API:扩展只从这里导入核心对象,1.0 起对这一层承诺兼容。

按扩展点分组:
- node_credentials / credential_profiles:节点凭据提供方协议、凭据项声明、凭据缓存与通用异常
- alert_sources:告警事件模型、工厂上下文、源错误与事件辅助(主机归一、截断、源端时间排序键)
- graphs:采集上下文;采集器取数用的节点 SSH 池(唯一出口 SSHPool)与插件服务(feeds)
"""
from __future__ import annotations

from lib.alerts.model import AlertEvent, normalize_host
from lib.alerts.sources import (
    AlertSourceContext,
    SourceError,
    _event_source_timestamp as event_source_timestamp,
    _truncate as truncate_text,
)
from lib.graphs import GraphCollectContext
from lib.credentials import (
    CredentialField,
    CredentialGenerateFailed,
    CredentialMissing,
    CredentialProfileSpec,
    CredentialProviderUnavailable,
    CredentialStoreError,
    CredentialTestFailed,
    FileCredentials,
    NodeCredentialProvider,
    ProviderAuthMalformed,
    ProviderAuthMissing,
    ProviderLoginFailed,
)

def node_pool(repo_root, platform: str):
    """登记集群的 SSHPool(红线与凭据同生产);用完须 close。"""
    from scripts._common import build_pool_for_platform

    return build_pool_for_platform(platform, repo_root)


def plugin_service(repo_root, state_dir):
    """插件服务(后台采集用 feed/feed_call,只认环境包声明的角色)。"""
    from lib.plugins import PluginService

    return PluginService(repo_root, state_dir)


__all__ = [
    "AlertEvent", "AlertSourceContext", "GraphCollectContext", "node_pool", "plugin_service", "SourceError", "event_source_timestamp", "normalize_host", "truncate_text",
    "CredentialField", "CredentialGenerateFailed", "CredentialMissing", "CredentialProfileSpec",
    "CredentialProviderUnavailable", "CredentialStoreError", "CredentialTestFailed", "FileCredentials",
    "NodeCredentialProvider", "ProviderAuthMalformed", "ProviderAuthMissing", "ProviderLoginFailed",
]
