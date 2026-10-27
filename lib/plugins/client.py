"""MCP 客户端(Streamable HTTP,initialize 握手)与插件免登录探测。

只实现中台用到的 tools/list、tools/call:每次调用一个短会话
(initialize → notifications/initialized → 请求 → DELETE 会话),断线后下次调用自然重连。
同步实现(worker 与路由层都是同步代码);httpx ``trust_env=False`` 直连不经代理。
只有建连失败重试一次,请求发出后不重试(与 ops-mcp 约定一致)。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List, Optional, Tuple

import httpx

PROTOCOL_VERSION = "2025-11-25"
CONNECT_TIMEOUT = 3.0
PROBE_TIMEOUT = 3.0
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_TOOL_PAGES = 20
CLIENT_INFO = {"name": "ops-agent", "version": "1"}


class PluginCallError(Exception):
    """code:PLUGIN_UNAVAILABLE(连不上/超时/5xx)、PLUGIN_AUTH_FAILED(401/403)、
    PLUGIN_PROTOCOL_ERROR(响应不合 MCP)、PLUGIN_TOOL_ERROR(JSON-RPC 错误,如参数不合法)。

    maybe_delivered:请求可能已到达插件并执行(发出后超时/断开/5xx/响应坏了)。
    写操作据此区分「确定没发出」与「结果未知须对账」;握手阶段的失败一律为 False。"""

    def __init__(self, code: str, message: str, *, maybe_delivered: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.maybe_delivered = maybe_delivered


class McpSession:
    def __init__(self, url: str, headers: Dict[str, str], timeout: float,
                 transport: Optional[httpx.BaseTransport] = None):
        self.url = url
        self.headers = dict(headers)
        self.client = httpx.Client(
            trust_env=False, follow_redirects=False, transport=transport,
            timeout=httpx.Timeout(timeout, connect=CONNECT_TIMEOUT),
        )
        self.session_id: Optional[str] = None
        self.protocol: Optional[str] = None
        self.server_info: Dict[str, Any] = {}
        self._next_id = 0

    def __enter__(self) -> "McpSession":
        try:
            result = self.request("initialize", {
                "protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO,
            }, retry_connect=True)
            self.protocol = str(result.get("protocolVersion") or PROTOCOL_VERSION)
            self.server_info = dict(result.get("serverInfo") or {})
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_id=None)
        except PluginCallError as exc:
            self.client.close()
            exc.maybe_delivered = False  # 握手没完成,工具调用不可能发出
            raise
        except BaseException:
            self.client.close()
            raise
        return self

    def __exit__(self, *_exc) -> None:
        try:
            if self.session_id:
                self.client.delete(self.url, headers=self._headers())
        except httpx.HTTPError:
            pass  # 会话由服务端超时回收
        finally:
            self.client.close()

    def request(self, method: str, params: Dict[str, Any], *, retry_connect: bool = False) -> Dict[str, Any]:
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        attempts = 2 if retry_connect else 1
        for attempt in range(attempts):
            try:
                message = self._post(payload, expect_id=self._next_id)
                break
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if attempt + 1 == attempts:
                    raise PluginCallError("PLUGIN_UNAVAILABLE", f"连接失败: {exc}") from None
        if message is None:
            raise PluginCallError("PLUGIN_PROTOCOL_ERROR", f"{method} 没有返回响应")
        if "error" in message:
            error = message.get("error") or {}
            raise PluginCallError("PLUGIN_TOOL_ERROR", f"{method} 出错: {error.get('message') or error}")
        result = message.get("result")
        if not isinstance(result, dict):
            raise PluginCallError("PLUGIN_PROTOCOL_ERROR", f"{method} 的响应缺少 result")
        return result

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json, text/event-stream", **self.headers}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol:
            headers["MCP-Protocol-Version"] = self.protocol
        return headers

    def _post(self, payload: Dict[str, Any], *, expect_id: Optional[int]) -> Optional[Dict[str, Any]]:
        try:
            with self.client.stream("POST", self.url, json=payload, headers=self._headers()) as response:
                if response.status_code in (401, 403):
                    raise PluginCallError("PLUGIN_AUTH_FAILED", f"认证失败(HTTP {response.status_code}),请在插件页检查凭据")
                if response.status_code >= 400:
                    server_error = response.status_code >= 500
                    raise PluginCallError("PLUGIN_UNAVAILABLE" if server_error else "PLUGIN_PROTOCOL_ERROR",
                                          f"HTTP {response.status_code}", maybe_delivered=server_error)
                self.session_id = response.headers.get("mcp-session-id") or self.session_id
                if expect_id is None:
                    return None
                try:
                    if "text/event-stream" in response.headers.get("content-type", ""):
                        return _first_matching(_sse_messages(response), expect_id)
                    return _first_matching(_json_messages(response), expect_id)
                except PluginCallError as exc:
                    exc.maybe_delivered = True  # 请求已被接收,只是响应读不出来
                    raise
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise
        except httpx.TimeoutException:
            raise PluginCallError("PLUGIN_UNAVAILABLE", "请求超时", maybe_delivered=True) from None
        except httpx.HTTPError as exc:
            raise PluginCallError("PLUGIN_UNAVAILABLE", f"连接中断: {type(exc).__name__}",
                                  maybe_delivered=True) from None


def list_tools(url: str, headers: Dict[str, str], timeout: float, *,
               transport: Optional[httpx.BaseTransport] = None) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    with McpSession(url, headers, timeout, transport) as session:
        tools: List[Dict[str, Any]] = []
        cursor = None
        for _ in range(MAX_TOOL_PAGES):
            result = session.request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(item for item in result.get("tools") or [] if isinstance(item, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return session.server_info, tools


def call_tool(url: str, headers: Dict[str, str], timeout: float, name: str, arguments: Dict[str, Any], *,
              transport: Optional[httpx.BaseTransport] = None) -> Dict[str, Any]:
    with McpSession(url, headers, timeout, transport) as session:
        return session.request("tools/call", {"name": name, "arguments": arguments})


def probe(origin: str, path: str, *, transport: Optional[httpx.BaseTransport] = None
          ) -> Tuple[Optional[int], Any, Optional[str]]:
    """免登录 GET(/healthz、/manifest):返回 (状态码, JSON 或 None, 连不上时的原因)。"""
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, transport=transport,
                          timeout=httpx.Timeout(PROBE_TIMEOUT)) as client:
            response = client.get(origin.rstrip("/") + path)
    except httpx.TimeoutException:
        return None, None, "超时"
    except httpx.HTTPError as exc:
        return None, None, f"连接失败: {type(exc).__name__}"
    try:
        body = response.json() if len(response.content) <= MAX_RESPONSE_BYTES else None
    except ValueError:
        body = None
    return response.status_code, body, None


def _first_matching(messages: Iterator[Any], expect_id: int) -> Optional[Dict[str, Any]]:
    for message in messages:
        if isinstance(message, dict) and message.get("id") == expect_id:
            return message
    raise PluginCallError("PLUGIN_PROTOCOL_ERROR", "响应里没有对应请求的结果")


def _json_messages(response: httpx.Response) -> Iterator[Any]:
    body = _bounded(response.iter_bytes())
    try:
        value = json.loads(body)
    except ValueError:
        raise PluginCallError("PLUGIN_PROTOCOL_ERROR", "响应不是 JSON") from None
    yield from value if isinstance(value, list) else [value]


def _sse_messages(response: httpx.Response) -> Iterator[Any]:
    size, data = 0, []
    for line in response.iter_lines():
        size += len(line) + 1
        if size > MAX_RESPONSE_BYTES:
            raise PluginCallError("PLUGIN_PROTOCOL_ERROR", "响应超过 4MB 上限")
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        elif not line and data:
            try:
                yield json.loads("\n".join(data))
            except ValueError:
                pass  # 非 JSON 事件(如心跳)跳过
            data = []
    if data:
        try:
            yield json.loads("\n".join(data))
        except ValueError:
            pass


def _bounded(chunks: Iterator[bytes]) -> bytes:
    body = bytearray()
    for chunk in chunks:
        body.extend(chunk)
        if len(body) > MAX_RESPONSE_BYTES:
            raise PluginCallError("PLUGIN_PROTOCOL_ERROR", "响应超过 4MB 上限")
    return bytes(body)
