"""Task-scoped remote connection ownership.

This module deliberately does not execute SSH commands itself.  SSHPool remains
the only command/file-transfer gateway; TaskRemoteSession only owns pools for a
single task execution and attaches safe connection metadata to results.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple


@dataclass(frozen=True)
class ConnectionInfo:
    status: str
    route: str
    channel: str
    reused: bool
    attempts: int = 0
    credential_status: str = "unknown"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "route": self.route,
            "channel": self.channel,
            "reused": self.reused,
            "attempts": self.attempts,
            "credential_status": self.credential_status,
        }


class RemoteExecutionError(RuntimeError):
    """A remote connection/action failed with a safe structured result."""

    def __init__(self, result: Dict[str, Any], cause: BaseException):
        self.result = result
        self.cause = cause
        super().__init__(str(result.get("error", {}).get("message", "远程执行失败")))


class TaskRemoteSession:
    """Own one or more platform SSHPools for one execution batch."""

    def __init__(self, execution_id: str, pool_factory: Callable[[str], Any]) -> None:
        if not execution_id:
            raise ValueError("execution_id 不能为空")
        self.execution_id = execution_id
        self._pool_factory = pool_factory
        self._pools: Dict[str, Any] = {}
        self._used: set[Tuple[str, str]] = set()
        self._lock = threading.RLock()
        self._closed = False

    def pool(self, platform: str) -> Any:
        with self._lock:
            if self._closed:
                raise RuntimeError(f"RemoteSession 已关闭: {self.execution_id}")
            pool = self._pools.get(platform)
            if pool is None:
                pool = self._pool_factory(platform)
                self._pools[platform] = pool
            return pool

    def run(self, platform: str, node: str, command: str, **kwargs):
        pool = self.pool(platform)
        key = (platform, node)
        was_used = key in self._used
        was_connected = _transport_is_active(pool, node)
        try:
            result = pool.run(node, command, **kwargs)
        except Exception:
            self._used.add(key)
            raise
        self._used.add(key)
        return result, self.connection_info(
            platform,
            node,
            was_connected=was_connected,
            was_used=was_used,
        )

    def push(self, platform: str, node: str, local: str, remote: str, **kwargs):
        pool = self.pool(platform)
        key = (platform, node)
        was_used = key in self._used
        was_connected = _transport_is_active(pool, node)
        pool.push(node, local, remote, **kwargs)
        self._used.add(key)
        return self.connection_info(
            platform,
            node,
            was_connected=was_connected,
            was_used=was_used,
            channel="pty" if _last_channel(pool, node) == "pty" else "sftp",
        )

    def is_used(self, platform: str, node: str) -> bool:
        with self._lock:
            return (platform, node) in self._used

    def mark_used(self, platform: str, node: str) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError(f"RemoteSession 已关闭: {self.execution_id}")
            self._used.add((platform, node))

    def connection_info(
        self,
        platform: str,
        node: str,
        *,
        was_connected: Optional[bool] = None,
        was_used: Optional[bool] = None,
        channel: Optional[str] = None,
    ) -> ConnectionInfo:
        pool = self.pool(platform)
        if was_connected is None:
            was_connected = (platform, node) in self._used
        if was_used is None:
            was_used = (platform, node) in self._used
        channel_name = channel or _last_channel(pool, node) or "exec"
        attempts = _last_attempts(pool, node)
        status = (
            "reused" if was_connected else
            "reconnected" if was_used else
            "opened"
        )
        return ConnectionInfo(
            status=status,
            route=_route(pool),
            channel=channel_name,
            reused=bool(was_connected),
            attempts=attempts if not was_connected else 0,
            credential_status=_credential_status(pool, node),
        )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pools = list(self._pools.values())
            self._pools.clear()
            self._used.clear()
        for pool in pools:
            try:
                pool.close()
            except Exception:
                # Closing is best effort; one broken transport must not prevent
                # the remaining task-scoped pools from being released.
                pass


def _transport_is_active(pool: Any, node: str) -> bool:
    method = getattr(pool, "transport_is_active", None)
    if callable(method):
        return bool(method(node))
    transports = getattr(pool, "_transports", {})
    transport = transports.get(node) if isinstance(transports, dict) else None
    try:
        return bool(transport is not None and transport.is_active())
    except Exception:
        return False


def _route(pool: Any) -> str:
    method = getattr(pool, "route_mode", None)
    if callable(method):
        return str(method())
    return "jump" if getattr(pool, "_jump", None) is not None else "direct"


def _last_channel(pool: Any, node: str) -> Optional[str]:
    method = getattr(pool, "last_channel_mode", None)
    if callable(method):
        return method(node)
    return None


def _last_attempts(pool: Any, node: str) -> int:
    method = getattr(pool, "last_connection_attempts", None)
    if callable(method):
        return int(method(node) or 0)
    return 0


def _credential_status(pool: Any, node: str) -> str:
    method = getattr(pool, "last_credential_status", None)
    if callable(method):
        return str(method(node) or "unknown")
    return "unknown"


def remote_error_result(
    operation: str,
    platform: str,
    node: str,
    exc: BaseException,
) -> Dict[str, Any]:
    """Map infrastructure exceptions to a redacted, stable ToolResult."""
    from lib.ssh_pool import (
        PushChannelFailed,
        PushTimeout,
        SSHAuthFailed,
        SSHNetworkFailed,
        SSHProtocolFailed,
    )

    if isinstance(exc, SSHAuthFailed):
        phase, kind, retryable, message = (
            "authentication", "SSHAuthFailed", True,
            "节点认证失败，请检查账号页的节点凭据与认证配置",
        )
    elif isinstance(exc, SSHNetworkFailed):
        phase, kind, retryable, message = (
            "tcp", "SSHNetworkFailed", True,
            "节点网络连接失败，请检查节点或跳板可达性",
        )
    elif isinstance(exc, SSHProtocolFailed):
        phase, kind, retryable, message = (
            "handshake", "SSHProtocolFailed", True,
            "SSH 协议握手失败",
        )
    elif isinstance(exc, (PushChannelFailed, PushTimeout)):
        phase, kind, retryable, message = (
            "channel", type(exc).__name__, isinstance(exc, PushTimeout),
            "远程文件传输通道失败",
        )
    else:
        phase, kind, retryable, message = (
            "channel", type(exc).__name__, False, "远程通道执行失败",
        )
    return {
        "schema_version": 1,
        "ok": False,
        "operation": operation,
        "target": {"platform": platform, "node": node},
        "error": {
            "phase": phase,
            "kind": kind,
            "retryable": retryable,
            "message": message,
        },
    }
