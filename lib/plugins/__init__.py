"""插件(MCP 服务)接入:注册、凭据、工具清单、准入、调用。

官方插件由环境包声明,自定义插件在插件页添加;只读工具按开关在任务首次执行时冻结进任务,
写工具一律不暴露(审批链路接入前)。
"""
from .client import PluginCallError
from .registry import Plugin, PluginConfigError, PluginNotFound, PluginRegistry
from .service import HEALTH_STATES, PluginService

__all__ = [
    "HEALTH_STATES", "Plugin", "PluginCallError", "PluginConfigError", "PluginNotFound", "PluginRegistry",
    "PluginService",
]
