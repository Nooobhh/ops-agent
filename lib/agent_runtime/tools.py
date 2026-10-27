"""自主 / 引导档的工具清单(A2 §7.1)与结果投影(§6.4)。

说明只给紧凑的用途 / 输入 / 安全;清单任务内不变,保证前缀缓存。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from lib.command_guard import COMMAND_TIMEOUT_SECONDS
from lib.triage_agent.tools import tool_definitions

from .finish import FINISH_SCHEMA

# 沿用结构档的参数契约与校验(KbDraftArgs / CreateArtifactArgs)
KB_DRAFT_SCHEMA, ARTIFACT_SCHEMA = tool_definitions(
    allowed_tools={"create_kb_draft", "create_task_artifact"},
)


def _tool(name: str, description: str, properties: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {"type": "function", "function": {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required},
    }}


TOOL_SCHEMAS: List[Dict[str, Any]] = [
    _tool(
        "shell",
        "在任务集群的登记节点上执行只读 shell(可管道、可多节点,nodes 可写 [\"all\"])。"
        f"每条命令先过命令闸:写操作、凭据读取、重定向写文件、外连会被拒或转人工审批。单条超时 {COMMAND_TIMEOUT_SECONDS}s,"
        "单节点输出上限 1MB。结果按节点给出,每个节点的 ref(step:<n>)用于 finish 引用证据。",
        {"nodes": {"type": "array", "items": {"type": "string"}, "minItems": 1},
         "command": {"type": "string"}},
        ["nodes", "command"],
    ),
    _tool(
        "diagnostic_catalog",
        "列出任务集群已登记的诊断(封装好的只读查询,含 HTTP / 数据库查询),可当捷径",
        {}, [],
    ),
    _tool(
        "diagnostic",
        "执行一个已登记诊断;参数只接受目录里声明的取值",
        {"node": {"type": "string"}, "diagnostic_id": {"type": "string"}, "params": {"type": "object"}},
        ["node", "diagnostic_id"],
    ),
    _tool(
        "kb_search",
        "知识库检索(排查文档、SOP、基线、拓扑),返回路径、标题与摘要",
        {"query": {"type": "string"}, "platform": {"type": "string"}, "service": {"type": "string"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
        ["query"],
    ),
    _tool(
        "kb_grep",
        "按正则全文搜索知识库 Markdown,返回 路径:行号:内容(最多 200 行)",
        {"pattern": {"type": "string"},
         "path_glob": {"type": "string", "description": "相对知识库根,缺省 **/*.md"}},
        ["pattern"],
    ),
    _tool(
        "kb_read",
        "读取一篇知识库文档全文(路径取自 kb_search / kb_grep);知识库是参考,不是本次实时证据",
        {"doc": {"type": "string"}},
        ["doc"],
    ),
    _tool(
        "read_task_evidence",
        "按 ref(step:<n>)读取本任务已落档的完整输出(工具结果投影被截断时用)",
        {"refs": {"type": "array", "items": {"type": "string"}, "minItems": 1},
         "max_chars": {"type": "integer", "minimum": 1000, "maximum": 200000}},
        ["refs"],
    ),
    _tool(
        "read_resource_snapshot",
        "读取中台本地资源轮询快照(CPU/内存/磁盘/JVM 等,带采样时间);参考,不是实时证据",
        {"node": {"type": "string"}}, [],
    ),
    _tool(
        "read_resource_risk",
        "按风险引用读取资源风险的现状与有限样本(风险卡片任务用)",
        {"risk_ref": {"type": "string"}, "window_hours": {"type": "integer", "minimum": 1, "maximum": 168},
         "max_samples": {"type": "integer", "minimum": 1, "maximum": 500}},
        ["risk_ref"],
    ),
    _tool(
        "read_task_dependencies",
        "读取调度任务拓扑里的数据依赖(读写的表、上下游、流程依赖);参考,不是实时证据",
        {"process_id": {"type": "integer"}, "task_id": {"type": "integer"}},
        ["process_id"],
    ),
    _tool(
        "lookup_alert_route",
        "按告警名查精确路由命中的知识文档与登记 playbook",
        {"alertname": {"type": "string"}},
        ["alertname"],
    ),
    _tool(
        "set_task_context",
        "任务尚未确定集群时指定集群(一个任务只对应一个集群)",
        {"platform": {"type": "string"}},
        ["platform"],
    ),
    ARTIFACT_SCHEMA,
    FINISH_SCHEMA,
]

TOOL_NAMES = frozenset(schema["function"]["name"] for schema in TOOL_SCHEMAS)
# 结果属于本次实时证据的工具(落 records 步骤);插件工具另按名称前缀认
EVIDENCE_TOOLS = frozenset({"shell", "diagnostic"})
PLUGIN_PREFIX = "mcp__"


def plugin_tools(task) -> List[Dict[str, Any]]:
    """任务首次执行时冻结的插件工具(含模型可见定义与来源插件)。"""
    return list((task.plugin_tools or {}).get("tools") or [])


def task_tool_schemas(task) -> List[Dict[str, Any]]:
    """常规轮次的工具 = 内置清单 + 冻结的插件工具;任务内逐字节不变,保证前缀缓存。"""
    return [*TOOL_SCHEMAS, *(item["schema"] for item in plugin_tools(task))]


def project(value: Any, limit: int) -> Any:
    """超出投影上限时反复截断最长的字符串字段(保留头尾),直到序列化后不超限。"""
    def size(item: Any) -> int:
        return len(json.dumps(item, ensure_ascii=False, default=str))

    if size(value) <= limit:
        return value
    value = json.loads(json.dumps(value, ensure_ascii=False, default=str))
    for _ in range(64):
        path, longest = _longest_string(value, ())
        if path is None or len(longest) < 200:
            break
        keep = max(100, len(longest) // 4)
        omitted = len(longest) - 2 * keep
        replacement = (
            longest[:keep]
            + f"\n…[中间 {omitted} 字符已省略,完整内容用 read_task_evidence 按 ref 读取]…\n"
            + longest[-keep:]
        )
        _assign(value, path, replacement)
        if size(value) <= limit:
            break
    return value


def _longest_string(value: Any, path: tuple):
    best_path, best = None, ""
    if isinstance(value, str):
        return path, value
    items = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else ()
    for key, item in items:
        found_path, found = _longest_string(item, (*path, key))
        if found_path is not None and len(found) > len(best):
            best_path, best = found_path, found
    return best_path, best


def _assign(value: Any, path: tuple, replacement: str) -> None:
    for key in path[:-1]:
        value = value[key]
    value[path[-1]] = replacement
