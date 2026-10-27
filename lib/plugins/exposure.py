"""插件工具交给 Agent 的两端:调用前的参数检查,调用后的结果归一与脱敏。"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

from lib.sanitize import redact_config_value

ERROR_TEXT_LIMIT = 2000
_JSON_TYPES = {
    "string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict,
}


def check_arguments(schema: Dict[str, Any], arguments: Any) -> Optional[str]:
    """轻量预检(必填、未声明的键、顶层类型);深层校验交给插件,出错会作为工具错误回模型。"""
    if not isinstance(arguments, dict):
        return "参数须为对象"
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    missing = [name for name in schema.get("required") or [] if name not in arguments]
    if missing:
        return f"缺少必填参数: {', '.join(missing)}"
    if properties and not schema.get("additionalProperties"):
        unknown = [name for name in arguments if name not in properties]
        if unknown:
            return f"未声明的参数: {', '.join(unknown)};可用: {', '.join(properties)}"
    for name, value in arguments.items():
        allowed = _allowed_types(properties.get(name) or {})
        if allowed and not _matches(value, allowed):
            return f"参数 {name} 类型应为 {'/'.join(allowed)}"
    return None


def apply_scope(spec: Dict[str, Any], arguments: Dict[str, Any], platform: str) -> Tuple[Dict[str, Any], Optional[str]]:
    """全局插件的集群过滤:返回 (实际调用参数, 驳回原因)。

    受约束工具:过滤对象里缺本集群标签就补上;给了别的集群(或含别的集群的列表)即驳回。
    环境级工具与非全局插件原样放行。
    """
    scope = spec.get("scope") or {}
    if scope.get("role") != "cluster":
        return arguments, None
    value = (scope.get("clusters") or {}).get(platform)
    if value is None:
        return arguments, f"插件 {spec['plugin']} 不覆盖集群 {platform},该工具不可用"
    argument, label = scope["argument"], scope["label"]
    filters = arguments.get(argument)
    if filters is None:
        filters = {}
    if not isinstance(filters, dict):
        return arguments, f"{argument} 须为对象"
    given = filters.get(label)
    if given is not None and {str(item) for item in (given if isinstance(given, list) else [given])} != {value}:
        return arguments, f"本任务集群是 {platform}({label}={value}),一个任务只对应一个集群,不能查 {label}={given}"
    return {**arguments, argument: {**filters, label: value}}, None


def normalize_result(raw: Dict[str, Any], *, plugin_id: str, tool: str, trusted: bool) -> Dict[str, Any]:
    """tools/call 结果 → 统一形态并逐值脱敏。ops-mcp 统一信封按其 ok/data/warnings/error 取。"""
    content = [item for item in raw.get("content") or [] if isinstance(item, dict)]
    texts = [item["text"] for item in content if item.get("type") == "text" and isinstance(item.get("text"), str)]
    payload = raw.get("structuredContent")
    if payload is None and texts:
        joined = "\n".join(texts)
        try:
            payload = json.loads(joined)
        except ValueError:
            payload = {"text": joined}
    ok = not raw.get("isError")
    warnings, error, meta = [], None, None
    if isinstance(payload, dict) and {"schema_version", "ok", "data"} <= payload.keys():
        ok = ok and bool(payload.get("ok"))
        data = payload.get("data")
        warnings = [str(item) for item in payload.get("warnings") or []]
        error, meta = payload.get("error"), payload.get("meta")
    else:
        data = payload
        if not ok:
            error = {"code": "PLUGIN_TOOL_ERROR", "message": "\n".join(texts)[:ERROR_TEXT_LIMIT]}
    if len(texts) < len(content):
        warnings.append(f"{len(content) - len(texts)} 项非文本内容(图片等)未传递")
    return redact({
        "plugin": plugin_id, "tool": tool, "trust": "official" if trusted else "untrusted",
        "ok": ok, "data": data, "warnings": warnings, "error": error, "meta": meta,
    })


def redact(value: Any, key: str = "") -> Any:
    """逐键脱敏:敏感键名整值替换,其余字符串按行内规则打码(须在 json.dumps 之前)。"""
    if isinstance(value, dict):
        return {name: redact(item, str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [redact(item, key) for item in value]
    return redact_config_value(key, value) if isinstance(value, str) else value


def _allowed_types(schema: Dict[str, Any]) -> list:
    options = schema.get("anyOf") if isinstance(schema.get("anyOf"), list) else [schema]
    types = [option.get("type") for option in options if isinstance(option, dict)]
    if not types or any(not isinstance(item, str) for item in types):
        return []  # 未声明或复杂组合:不预检
    return [item for item in types if item in _JSON_TYPES or item == "null"]


def _matches(value: Any, allowed: list) -> bool:
    for name in allowed:
        if name == "null" and value is None:
            return True
        expected = _JSON_TYPES.get(name)
        if expected and isinstance(value, expected) and not (isinstance(value, bool) and name in ("integer", "number")):
            return True
    return False
