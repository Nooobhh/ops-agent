"""模型 HTTP 层:A 层录下原始响应与 usage,A0 层按顺序重放同一批响应。"""
from __future__ import annotations

import json
import threading
from typing import Any, Dict, List, Optional

import httpx


class ReplayScriptExhausted(RuntimeError):
    """A0 回放时 harness 要的模型回合多于录制:行为已分叉。"""


def _usage(body: Any) -> Dict[str, int]:
    usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        return {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    return {
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "cached_tokens": int(prompt_details.get("cached_tokens") or 0),
        "reasoning_tokens": int(completion_details.get("reasoning_tokens") or 0),
    }


class RecordingTransport(httpx.BaseTransport):
    """透传到真实模型,同时记录每次响应(供 A0 重放)与 token 用量。"""

    def __init__(self, inner: Optional[httpx.BaseTransport] = None):
        self._inner = inner or httpx.HTTPTransport()
        self._lock = threading.Lock()
        self.responses: List[Dict[str, Any]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self._inner.handle_request(request)
        try:
            content = response.read()
        finally:
            response.close()
        try:
            body: Any = json.loads(content)
        except ValueError:
            body = None
        with self._lock:
            self.responses.append({
                "status_code": response.status_code,
                "body": body,
                "usage": _usage(body),
                "model": body.get("model") if isinstance(body, dict) else None,
            })
        # read() 已按 content-encoding 解码,重新包装时去掉编码与长度头,防二次解压
        headers = [
            (key, value) for key, value in response.headers.items()
            if key.lower() not in {"content-encoding", "content-length", "transfer-encoding"}
        ]
        return httpx.Response(
            response.status_code,
            headers=headers,
            content=content,
            request=request,
        )

    def close(self) -> None:
        self._inner.close()


class ScriptedTransport(httpx.BaseTransport):
    """按顺序回放录下的响应;不发任何网络请求。"""

    def __init__(self, responses: List[Dict[str, Any]]):
        self._responses = list(responses)
        self._lock = threading.Lock()
        self.served = 0
        self.exhausted = False

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            if self.served >= len(self._responses):
                self.exhausted = True
                raise ReplayScriptExhausted(
                    f"录制的模型响应已用完(共 {len(self._responses)} 次)"
                )
            item = self._responses[self.served]
            self.served += 1
        if item.get("body") is None:
            return httpx.Response(int(item.get("status_code") or 500), request=request)
        return httpx.Response(
            int(item.get("status_code") or 200),
            json=item["body"],
            request=request,
        )


def usage_totals(responses: List[Dict[str, Any]]) -> Dict[str, Any]:
    totals = {"requests": len(responses), "prompt_tokens": 0, "completion_tokens": 0,
              "cached_tokens": 0, "reasoning_tokens": 0, "http_errors": 0}
    models = set()
    for item in responses:
        for key, value in (item.get("usage") or {}).items():
            totals[key] = totals.get(key, 0) + int(value)
        if int(item.get("status_code") or 0) >= 400:
            totals["http_errors"] += 1
        if item.get("model"):
            models.add(str(item["model"]))
    totals["models"] = sorted(models)
    return totals
