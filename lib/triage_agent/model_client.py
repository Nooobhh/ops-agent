from __future__ import annotations

import json
import re
import logging
import os
import time
import uuid
from pathlib import Path
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Literal, Mapping, Optional, Protocol
from urllib.parse import urlsplit
from urllib.request import proxy_bypass

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from lib.triage_agent.model_registry import ModelRegistry, ReasoningEffort


MODEL_LOGGER_NAME = "ops_agent.triage.model"
MODEL_LOGGER = logging.getLogger(MODEL_LOGGER_NAME)


def configure_model_logging(state_dir: Path) -> Path:
    """Persist model transport diagnostics without exposing prompts or credentials."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = (state_dir / "triage-model.log").resolve()
    MODEL_LOGGER.setLevel(logging.INFO)
    MODEL_LOGGER.propagate = False
    for handler in MODEL_LOGGER.handlers:
        if isinstance(handler, RotatingFileHandler) and Path(handler.baseFilename).resolve() == path:
            return path
    handler = RotatingFileHandler(
        path,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    MODEL_LOGGER.addHandler(handler)
    return path


class ModelProtocolError(RuntimeError):
    pass


class ModelTransientError(ModelProtocolError):
    """超时、连接被断开/失败、网关 429/502/503/504:请求无副作用,调用方可安全重试。"""


_TRANSIENT_STATUS = {429, 502, 503, 504}


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["openai_compatible"] = "openai_compatible"
    base_url: str
    api_key: SecretStr
    model: str
    reasoning_effort: ReasoningEffort = "none"
    timeout_seconds: float = Field(default=120, gt=0, le=600)
    max_tool_turns: int = Field(default=24, ge=1, le=64)  # 与 ModelRegistry 同步


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str
    name: str
    arguments: Dict[str, Any]
    # 宽松解析下参数不是合法 JSON object 时的原因;此时 arguments 为空
    arguments_error: Optional[str] = None


class ModelReply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: Optional[str]
    tool_calls: List[ToolCall]
    assistant_message: Dict[str, Any]
    # 服务端 usage 原样(prompt/completion tokens、缓存命中、思考 tokens 等),无则 None
    usage: Optional[Dict[str, Any]] = None


class ModelClient(Protocol):
    def complete(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> ModelReply:
        pass


def load_reviewer_config(path: Path) -> Optional[ModelConfig]:
    """审核模型配置。未配置返回 None——调用方据此把未登记命令全部转人工。

    与 load_model_config 的区别是「缺配置不是错误」:审核器是可选加速层,
    没有它系统仍然安全,只是人工裁决更频繁。
    """
    try:
        config = ModelRegistry(path).load()
    except Exception:  # noqa: BLE001
        return None
    workflow = getattr(config.defaults, "reviewer", None) if config.defaults else None
    if workflow is None:
        return None
    profile = next(
        (item for item in config.models if item.id == workflow.model_id), None
    )
    if profile is None:
        return None
    return ModelConfig(
        provider=profile.provider,
        base_url=profile.base_url,
        api_key=profile.api_key,
        model=profile.model,
        reasoning_effort=workflow.reasoning_effort,
        timeout_seconds=workflow.timeout_seconds,
        max_tool_turns=config.max_tool_turns,
    )


def load_model_config(path: Path) -> ModelConfig:
    try:
        config = ModelRegistry(path).load()
        if config.defaults is None:
            raise ValueError("尚未配置手动排查默认模型")
        workflow = config.defaults.manual
        profile = next(
            item for item in config.models if item.id == workflow.model_id
        )
        return ModelConfig(
            provider=profile.provider,
            base_url=profile.base_url,
            api_key=profile.api_key,
            model=profile.model,
            reasoning_effort=workflow.reasoning_effort,
            timeout_seconds=workflow.timeout_seconds,
            max_tool_turns=config.max_tool_turns,
        )
    except Exception as exc:
        raise ValueError(
            f"模型配置无效，仅支持 provider=openai_compatible: {type(exc).__name__}"
        ) from exc


class OpenAICompatibleClient:
    def __init__(
        self,
        config: ModelConfig,
        *,
        transport: Optional[httpx.BaseTransport] = None,
        context: Optional[Mapping[str, str]] = None,
        lenient: bool = False,
    ):
        self.config = config
        self.context = dict(context or {})
        # 新运行时:非法工具参数交回模型重试(连续 3 轮进收口回合)
        self.lenient = lenient
        self._request_sequence = 0
        base_url = config.base_url.rstrip("/") + "/"
        self._client = httpx.Client(
            base_url=base_url,
            timeout=config.timeout_seconds,
            transport=transport,
        )

    def complete(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> ModelReply:
        self._request_sequence += 1
        request_id = uuid.uuid4().hex[:12]
        started = time.monotonic()
        endpoint, host = _safe_endpoint(self.config.base_url)
        route = _network_route(host)
        try:
            payload = {
                "model": self.config.model,
                "messages": messages,
                "tools": tools,
            }
            if self.config.reasoning_effort != "none":
                payload["reasoning_effort"] = self.config.reasoning_effort
            self._log(
                "model_request_start",
                request_id=request_id,
                endpoint=endpoint,
                model=self.config.model,
                timeout_seconds=self.config.timeout_seconds,
                reasoning_effort=self.config.reasoning_effort,
                message_count=len(messages),
                tool_count=len(tools),
                payload_bytes=len(json.dumps(payload, ensure_ascii=False)),
                **route,
            )
            response = self._client.post(
                "chat/completions",
                headers={
                    "Authorization": (
                        f"Bearer {self.config.api_key.get_secret_value()}"
                    ),
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            self._log(
                "model_request_response",
                level=logging.WARNING if response.status_code >= 400 else logging.INFO,
                request_id=request_id,
                endpoint=endpoint,
                status_code=response.status_code,
                elapsed_seconds=round(time.monotonic() - started, 3),
                response_bytes=len(response.content),
                upstream_request_id=(
                    response.headers.get("x-request-id")
                    or response.headers.get("request-id")
                    or response.headers.get("cf-ray")
                ),
            )
            response.raise_for_status()
            return _parse_reply(response.json(), lenient=self.lenient)
        except ModelProtocolError:
            self._log(
                "model_request_protocol_error",
                level=logging.WARNING,
                request_id=request_id,
                endpoint=endpoint,
                elapsed_seconds=round(time.monotonic() - started, 3),
            )
            raise
        except httpx.HTTPStatusError as exc:
            # 只有类型名的话,余额不足、key 失效、限流在任务详情里长得一模一样,
            # 全都是「模型请求失败: HTTPStatusError」,得另开一路探测才知道原因
            error_type = (
                ModelTransientError
                if exc.response.status_code in _TRANSIENT_STATUS
                else ModelProtocolError
            )
            raise error_type(
                f"模型请求失败: HTTP {exc.response.status_code} "
                f"{_error_detail(exc.response)}"
            ) from exc
        except Exception as exc:
            self._log(
                "model_request_error",
                level=logging.WARNING,
                request_id=request_id,
                endpoint=endpoint,
                elapsed_seconds=round(time.monotonic() - started, 3),
                exception_type=type(exc).__name__,
                phase=_exception_phase(exc),
                detail=str(exc)[:240],
                cause_type=(type(exc.__cause__).__name__ if exc.__cause__ else None),
                cause_detail=(str(exc.__cause__)[:160] if exc.__cause__ else None),
            )
            error_type = (
                ModelTransientError
                if isinstance(exc, (
                    httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError,
                ))
                else ModelProtocolError
            )
            raise error_type(f"模型请求失败: {type(exc).__name__}") from exc

    def _log(self, event: str, *, level: int = logging.INFO, **fields: Any) -> None:
        payload = {
            "event": event,
            "request_sequence": self._request_sequence,
            **self.context,
            **fields,
        }
        MODEL_LOGGER.log(level, json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _safe_endpoint(base_url: str) -> tuple[str, str]:
    parsed = urlsplit(base_url)
    host = parsed.hostname or "unknown"
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path.rstrip("/") + "/chat/completions"
    return f"{parsed.scheme}://{host}{port}{path}", host


def _network_route(host: str) -> Dict[str, Any]:
    proxy_configured = any(
        os.environ.get(key)
        for key in (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "all_proxy",
        )
    )
    try:
        bypassed = proxy_bypass(host)
    except (OSError, ValueError):
        bypassed = None
    if not proxy_configured:
        route = "direct"
    elif bypassed is True:
        route = "direct_bypass"
    elif bypassed is False:
        route = "proxy"
    else:
        route = "unknown"
    return {
        "route": route,
        "proxy_configured": bool(proxy_configured),
        "proxy_bypass": bypassed,
    }


def _exception_phase(exc: Exception) -> str:
    if isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError)):
        return "connect"
    if isinstance(exc, httpx.ReadTimeout):
        return "read"
    if isinstance(exc, httpx.WriteTimeout):
        return "write"
    if isinstance(exc, httpx.PoolTimeout):
        return "pool"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    return "request"


def _error_detail(response: httpx.Response, limit: int = 200) -> str:
    """网关的错误体形态各异,取 message/error 字段,取不到就退回截断原文。"""
    try:
        body = response.json()
    except ValueError:
        text = response.text.strip()
        # 网关(nginx)的错误页是整段 HTML,只留 <title>,否则报错铺满任务详情
        title = re.search(r"<title>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
        if title:
            return title.group(1).strip()[:limit]
        return text[:limit]
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            body = error
        for key in ("message", "msg", "detail", "code"):
            value = body.get(key) if isinstance(body, dict) else None
            if isinstance(value, str) and value:
                return value[:limit]
    return str(body)[:limit]


def _parse_reply(payload: Any, *, lenient: bool = False) -> ModelReply:
    if not isinstance(payload, dict):
        raise ModelProtocolError("模型响应必须是 JSON object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ModelProtocolError("模型响应缺少 choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise ModelProtocolError("模型响应缺少 assistant message")

    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise ModelProtocolError("assistant content 必须是字符串或 null")
    raw_calls = message.get("tool_calls") or []
    if not isinstance(raw_calls, list):
        raise ModelProtocolError("assistant tool_calls 必须是列表")

    calls: List[ToolCall] = []
    seen_ids = set()
    for raw in raw_calls:
        if not isinstance(raw, dict) or raw.get("type") != "function":
            raise ModelProtocolError("tool_call 必须是 function")
        call_id = raw.get("id")
        function = raw.get("function")
        if not isinstance(call_id, str) or not call_id or call_id in seen_ids:
            raise ModelProtocolError("tool_call id 缺失或重复")
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            raise ModelProtocolError("tool_call function.name 缺失")
        arguments_error = None
        try:
            arguments = json.loads(function.get("arguments", ""))
        except (TypeError, json.JSONDecodeError) as exc:
            if not lenient:
                raise ModelProtocolError("tool_call arguments 必须是 JSON object") from exc
            arguments, arguments_error = {}, f"参数不是合法 JSON: {exc}"
        if not isinstance(arguments, dict):
            if not lenient:
                raise ModelProtocolError("tool_call arguments 必须是 JSON object")
            arguments, arguments_error = {}, "参数必须是 JSON object"
        seen_ids.add(call_id)
        calls.append(ToolCall(
            call_id=call_id,
            name=function["name"],
            arguments=arguments,
            arguments_error=arguments_error,
        ))

    if content is None and not calls:
        raise ModelProtocolError("assistant 响应必须包含 content 或 tool_calls")
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
    return ModelReply(content=content, tool_calls=calls, assistant_message=message, usage=usage)


def probe_native_tools(client: ModelClient) -> Dict[str, bool]:
    tools = [{
        "type": "function",
        "function": {
            "name": "return_probe",
            "description": "Return a fixed probe token without side effects",
            "parameters": {
                "type": "object",
                "properties": {
                    "token": {"type": "string", "const": "ops-agent-probe"},
                },
                "required": ["token"],
                "additionalProperties": False,
            },
        },
    }]
    user_message = {
        "role": "user",
        "content": "Call return_probe with token ops-agent-probe.",
    }
    first = client.complete([user_message], tools)
    tool_ok = (
        len(first.tool_calls) == 1
        and first.tool_calls[0].name == "return_probe"
        and first.tool_calls[0].arguments == {"token": "ops-agent-probe"}
    )
    if not tool_ok:
        return {"native_tools": False, "tool_result_roundtrip": False}

    second = client.complete([
        user_message,
        first.assistant_message,
        {
            "role": "tool",
            "tool_call_id": first.tool_calls[0].call_id,
            "content": '{"token":"ops-agent-probe","ok":true}',
        },
    ], tools)
    roundtrip = second.content is not None and not second.tool_calls
    return {"native_tools": True, "tool_result_roundtrip": roundtrip}
