"""模型可见工具清单(只读):新运行时实际交给模型的工具定义,不含渲染命令与凭据。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from lib.agent_runtime.tools import KB_DRAFT_SCHEMA, TOOL_SCHEMAS
from lib.plugins import PluginConfigError, PluginService


def router(*, repo_root: Path, state_dir: Path) -> APIRouter:
    routes = APIRouter()

    @routes.get("/api/triage-tools")
    def triage_tools():
        # 常规轮次的工具 + 「生成知识草稿」那一轮专用的 create_kb_draft
        schemas = [*TOOL_SCHEMAS, KB_DRAFT_SCHEMA]
        items = [schema["function"] for schema in schemas]
        # 插件工具按当前插件页开关给出(未定集群的新任务会拿到这一份);任务内以冻结快照为准
        try:
            plugin_tools = PluginService(repo_root, state_dir).snapshot(None)["tools"]
        except PluginConfigError:
            plugin_tools = []
        items.extend({**tool["schema"]["function"], "plugin": tool["plugin"]} for tool in plugin_tools)
        return {"schema_version": 1, "items": items}

    return routes
