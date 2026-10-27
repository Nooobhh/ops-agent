"""持久 SSH 池:每节点一条持久 Transport,每条命令一个独立 exec channel。

到集群的唯一出口。红线(CmdFilter)在 run() 入口;push() 限定 /tmp/opsagent/ 工作区。
超时只杀当条 channel,Transport 存活(结构性消灭 v2/v3 的哨兵/毒化/吞输出问题)。
跳板(direct-tcpip)仅由 local-dev 配置注入,生产直连。
"""
from __future__ import annotations

import posixpath
import socket
import time
import base64
import hashlib
import shlex
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import paramiko

from .cmd_filter import CmdFilter
from .config import JumpConfig, PlatformConfig
from .credentials import (
    CredentialUnavailable,
    CredentialGenerateFailed,
    CredentialProviderUnavailable,
)
from .pty_channel import PtyChannel, PtyChannelDead

WORKDIR_PREFIX = "/tmp/opsagent/"
# 避免堡垒机/凭据提供方故障期重复支付重连成本;5 秒足够覆盖同批连续诊断。
_PTY_RECONNECT_FAILURE_WINDOW = 5.0


@dataclass
class CmdResult:
    node: str
    cmd: str
    exit_code: Optional[int]
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False
    truncated: bool = False


class UnknownNode(ValueError):
    pass


class PushDenied(Exception):
    pass


class PushTimeout(Exception):
    pass


class PushIntegrityError(Exception):
    pass


class PushChannelFailed(RuntimeError):
    def __init__(self, message: str, last_error: BaseException):
        self.last_error = last_error
        super().__init__(message)


class SSHConnectFailed(RuntimeError):
    def __init__(self, message: str, last_error: BaseException):
        self.last_error = last_error
        super().__init__(message)


class SSHAuthFailed(SSHConnectFailed):
    pass


class SSHNetworkFailed(SSHConnectFailed):
    pass


class SSHProtocolFailed(SSHConnectFailed):
    pass


def _open_socket(host: str, port: int, timeout: float) -> socket.socket:
    """直连建 TCP 并设上限。paramiko 收到 (host, port) 元组时自建 socket 且不设
    超时,主机不可达会卡到 OS 超时(数十秒起)再乘以重试次数。"""
    return socket.create_connection((host, port), timeout=timeout)


class SSHPool:
    _CONNECT_MAX_ATTEMPTS = 3
    _CONNECT_BACKOFFS = (0.0, 2.0, 5.0)
    _CONNECT_TIMEOUT_SECONDS = 10.0

    def __init__(self, platform: PlatformConfig, credentials, cmd_filter: CmdFilter,
                 jump: Optional[JumpConfig] = None):
        self._platform = platform
        self._creds = credentials
        self._filter = cmd_filter
        self._jump = jump
        self._jump_client: Optional[paramiko.SSHClient] = None
        self._transports: Dict[str, paramiko.Transport] = {}
        self._pty_channels: Dict[str, PtyChannel] = {}
        self._pty_reconnect_failures: Dict[str, tuple[float, BaseException]] = {}
        self._last_connection_attempts: Dict[str, int] = {}
        self._last_credential_status: Dict[str, str] = {}
        self._last_channel_modes: Dict[str, str] = {}

    # ---- 拓扑 ----

    def components(self, node: str) -> List[str]:
        """节点部署的组件清单;playbook 的「若有」类检查据此判断该组件是否本该在此。"""
        info = self._platform.nodes.get(node)
        return list(info.components) if info else []

    def _node_info(self, node: str):
        """未登记节点抛具名异常;裸 nodes[node] 的 KeyError 上层读不懂也判不了责。"""
        info = self._platform.nodes.get(node)
        if info is None:
            raise UnknownNode(
                f"节点未在平台 {self._platform.platform} 登记: {node}"
            )
        return info

    # ---- 执行 ----

    def run(
        self,
        node: str,
        cmd: str,
        timeout: float = 30.0,
        approved: bool = False,
        max_output_bytes: Optional[int] = None,
    ) -> CmdResult:
        if max_output_bytes is not None and (
            type(max_output_bytes) is not int or max_output_bytes <= 0
        ):
            raise ValueError("max_output_bytes must be a positive integer or None")
        self._filter.check(cmd, approved=approved)   # 红线:唯一出口上的闸门
        credential = self._credential(node)
        self._last_channel_modes[node] = "pty" if credential.pty else "exec"
        if credential.pty:
            result = self._run_pty(node, cmd, timeout, max_output_bytes)
            if max_output_bytes is None:
                return result
            stdout, truncated = self._truncate_output(
                result.stdout.encode("utf-8"), max_output_bytes
            )
            return CmdResult(
                result.node,
                result.cmd,
                result.exit_code,
                stdout,
                result.stderr,
                result.duration,
                timed_out=result.timed_out,
                # 流式截断按通道原始字节计,事后这层按 UTF-8 编码字节计,
                # 两者可能差一点;已发生的截断不能被事后判定抹掉
                truncated=truncated or result.truncated,
            )
        t0 = time.monotonic()
        chan = self._transport(node).open_session(timeout=10)
        chan.settimeout(5)
        try:
            chan.exec_command(cmd)
            out, err, timed_out, truncated = self._drain(
                chan,
                deadline=t0 + timeout,
                max_output_bytes=max_output_bytes,
            )
            exit_code = (
                None
                if timed_out or truncated
                else chan.recv_exit_status()
            )
            return CmdResult(node, cmd, exit_code, out, err,
                             time.monotonic() - t0, timed_out=timed_out,
                             truncated=truncated)
        finally:
            chan.close()

    def _run_pty(
        self,
        node: str,
        cmd: str,
        timeout: float,
        max_output_bytes: Optional[int] = None,
    ) -> CmdResult:
        t0 = time.monotonic()
        recent_failure = self._pty_reconnect_failures.get(node)
        if (
            recent_failure is not None
            and t0 - recent_failure[0] < _PTY_RECONNECT_FAILURE_WINDOW
        ):
            last_error = recent_failure[1]
            raise SSHNetworkFailed(
                f"SSH 连接失败: {node}; PTY 通道重连冷却中", last_error
            ) from last_error
        deadline = t0 + timeout
        cached = self._pty_channels.get(node)
        reused = False
        try:
            chan = self._pty_channel(node)
            reused = chan is cached
            exit_code, out, capped = chan.exec(
                cmd, timeout=timeout, max_output_bytes=max_output_bytes,
            )
        except PtyChannelDead as exc:
            if not (reused and exc.retryable):
                if cached is not None and not reused:
                    self._pty_reconnect_failures[node] = (
                        time.monotonic(), exc
                    )
                raise SSHNetworkFailed(
                    f"SSH 连接失败: {node}; PTY 通道已断", exc
                ) from exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SSHNetworkFailed(
                    f"SSH 连接失败: {node}; PTY 通道已断", exc
                ) from exc
            try:
                exit_code, out, capped = self._pty_channel(node).exec(
                    cmd, timeout=remaining, max_output_bytes=max_output_bytes,
                )
            except PtyChannelDead as retry_exc:
                self._pty_reconnect_failures[node] = (
                    time.monotonic(), retry_exc
                )
                raise SSHNetworkFailed(
                    f"SSH 连接失败: {node}; PTY 通道已断", retry_exc
                ) from retry_exc
            except SSHConnectFailed as retry_exc:
                self._pty_reconnect_failures[node] = (
                    time.monotonic(), retry_exc
                )
                raise
        except SSHConnectFailed as exc:
            # 冷却只在「池中已有通道、重建时才连不上」时武装:首次失败必须是真实
            # 重连,否则拓扑/凭证配错会被「冷却中」的提示盖住。此处 reused 必为
            # False(赋值在 _pty_channel 返回之后),故只判 cached。
            if cached is not None:
                self._pty_reconnect_failures[node] = (
                    time.monotonic(), exc
                )
            raise
        self._pty_reconnect_failures.pop(node, None)
        return CmdResult(
            node, cmd, exit_code, out, "",
            time.monotonic() - t0,
            # 撞上限提前收也拿不到 exit_code,但它不是超时,别混成同一个信号
            timed_out=exit_code is None and not capped,
            truncated=capped,
        )

    @staticmethod
    def _drain(
        chan,
        deadline: float,
        max_output_bytes: Optional[int] = None,
    ):
        if max_output_bytes is not None and (
            type(max_output_bytes) is not int or max_output_bytes <= 0
        ):
            raise ValueError("max_output_bytes must be a positive integer or None")
        out, err = bytearray(), bytearray()
        # 上限对 stdout+stderr 合计生效:只限 stdout 时 `yes >&2` 可无限占内存。
        # deadline 也必须在内层循环里查——远端持续输出时 recv_ready() 恒为真,
        # 外层的检查永远轮不到,超时形同虚设。
        notice = f"\n[output truncated after {max_output_bytes} bytes]\n"

        def _budget_left() -> int:
            if max_output_bytes is None:
                return 65536
            return max_output_bytes - len(out) - len(err)

        def _plain():
            return out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

        def _capped(side: str):
            stdout, stderr = _plain()
            if side == "err":
                return stdout, stderr + notice, False, True
            return stdout + notice, stderr, False, True

        while True:
            got = False
            while chan.recv_ready():
                left = _budget_left()
                if left <= 0:
                    return _capped("out")
                out += chan.recv(min(65536, left))
                got = True
                if _budget_left() <= 0:
                    return _capped("out")
                if time.monotonic() > deadline:
                    stdout, stderr = _plain()
                    return stdout, stderr, True, False
            while chan.recv_stderr_ready():
                left = _budget_left()
                if left <= 0:
                    return _capped("err")
                err += chan.recv_stderr(min(65536, left))
                got = True
                if _budget_left() <= 0:
                    return _capped("err")
                if time.monotonic() > deadline:
                    stdout, stderr = _plain()
                    return stdout, stderr, True, False
            eof_received = getattr(chan, "eof_received", None)
            buffers_empty = not chan.recv_ready() and not chan.recv_stderr_ready()
            if (
                buffers_empty
                and (
                    bool(eof_received)
                    or (
                        chan.exit_status_ready()
                        and (eof_received is None or bool(getattr(chan, "closed", False)))
                    )
                )
            ):
                return (
                    out.decode("utf-8", "replace"),
                    err.decode("utf-8", "replace"),
                    False,
                    False,
                )
            if time.monotonic() > deadline:
                return (
                    out.decode("utf-8", "replace"),
                    err.decode("utf-8", "replace"),
                    True,
                    False,
                )
            if not got:
                time.sleep(0.02)

    @staticmethod
    def _truncate_output(data: bytes, max_output_bytes: int):
        truncated = len(data) >= max_output_bytes
        text = data[:max_output_bytes].decode("utf-8", "replace")
        if truncated:
            text += (
                f"\n[output truncated after {max_output_bytes} bytes]\n"
            )
        return text, truncated

    # ---- 文件推送 ----

    # SFTP 瞬时故障重试:每次尝试独立超时预算,只对非 PushTimeout 异常重试。
    # 退避总耗时 400ms,3 次尝试完仍失败才走降级/抛。
    _SFTP_RETRY_BACKOFFS = (0.1, 0.3)

    def push(self, node: str, local: str, remote: str, timeout: float = 30.0) -> None:
        norm = posixpath.normpath(remote)
        if not (norm == WORKDIR_PREFIX.rstrip("/") or norm.startswith(WORKDIR_PREFIX)):
            raise PushDenied(f"push 目标必须在 {WORKDIR_PREFIX} 下(normalize 后): {remote}")

        credential = self._credential(node)
        self._last_channel_modes[node] = "pty" if credential.pty else "sftp"
        last_err: Optional[BaseException] = None
        for attempt in range(len(self._SFTP_RETRY_BACKOFFS) + 1):
            try:
                self._push_sftp(node, local, norm, timeout=timeout, credential=credential)
                return
            except PushTimeout:
                raise
            except Exception as exc:
                last_err = exc
                if attempt < len(self._SFTP_RETRY_BACKOFFS):
                    time.sleep(self._SFTP_RETRY_BACKOFFS[attempt])

        if credential.pty:
            self._push_pty_base64(node, local, norm, timeout=timeout)
            return
        assert last_err is not None
        raise last_err

    def _push_sftp(
        self,
        node: str,
        local: str,
        norm: str,
        timeout: float,
        credential=None,
    ) -> None:
        chan = None
        sftp = None
        deadline = time.monotonic() + timeout
        try:
            try:
                chan = self._transport(node, credential=credential).open_session(timeout=timeout)
            except (socket.timeout, TimeoutError) as exc:
                raise PushTimeout(f"push 超时: {node} -> {norm}") from exc
            except paramiko.SSHException as exc:
                if "Timeout opening channel" in str(exc):
                    raise PushTimeout(f"push 超时: {node} -> {norm}") from exc
                raise
            chan.settimeout(timeout)
            self._call_with_timeout(
                lambda: chan.invoke_subsystem("sftp"),
                deadline - time.monotonic(),
                lambda: chan.close(),
                node,
                norm,
            )
            sftp = self._call_with_timeout(
                lambda: self._new_sftp_client(chan),
                deadline - time.monotonic(),
                lambda: chan.close(),
                node,
                norm,
            )
            sftp.put(str(Path(local)), norm)   # 用 normalize 后的路径,防服务端二次解释
        except (socket.timeout, TimeoutError) as exc:
            raise PushTimeout(f"push 超时: {node} -> {norm}") from exc
        finally:
            if sftp is not None:
                sftp.close()
            if chan is not None:
                chan.close()

    @staticmethod
    def _new_sftp_client(chan):
        return paramiko.SFTPClient(chan)

    @staticmethod
    def _call_with_timeout(func, timeout: float, on_timeout, node: str, norm: str):
        if timeout <= 0:
            on_timeout()
            raise PushTimeout(f"push 超时: {node} -> {norm}")
        box = {}

        def runner():
            try:
                box["value"] = func()
            except BaseException as exc:
                box["error"] = exc

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            on_timeout()
            raise PushTimeout(f"push 超时: {node} -> {norm}")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def _push_pty_base64(self, node: str, local: str, norm: str, timeout: float) -> None:
        try:
            pty = self._pty_channel(node)
        except PtyChannelDead as exc:
            raise PushChannelFailed(
                f"push PTY 通道已断: {node} -> {norm}", exc
            ) from exc
        b64_path = f"{norm}.b64"
        deadline = time.monotonic() + timeout
        local_digest = hashlib.sha256(Path(local).read_bytes()).hexdigest()
        commands = [f": > {shlex.quote(b64_path)}"]
        with Path(local).open("rb") as fh:
            while True:
                chunk = fh.read(24576)
                if not chunk:
                    break
                encoded = base64.b64encode(chunk).decode("ascii")
                commands.append(
                    f"printf %s {shlex.quote(encoded)} >> {shlex.quote(b64_path)}"
                )
        commands.append(
            f"base64 -d {shlex.quote(b64_path)} > {shlex.quote(norm)}"
        )
        digest_cmd = f"sha256sum {shlex.quote(norm)} | awk '{{print $1}}'"
        cleanup_bad = f"rm -f {shlex.quote(norm)} {shlex.quote(b64_path)}"
        cleanup_tmp = f"rm -f {shlex.quote(b64_path)}"
        try:
            for cmd in commands:
                self._pty_exec_with_deadline(pty, cmd, deadline, node, norm)
            remote_digest = self._pty_exec_with_deadline(pty, digest_cmd, deadline, node, norm).strip().splitlines()[-1].split()[0]
            if remote_digest != local_digest:
                self._best_effort_pty(pty, cleanup_bad)
                raise PushIntegrityError(f"push digest mismatch: {node} -> {norm}")
            self._pty_exec_with_deadline(pty, cleanup_tmp, deadline, node, norm)
        except PushTimeout:
            self._best_effort_pty(pty, cleanup_bad)
            raise
        except PtyChannelDead as exc:
            self._best_effort_pty(pty, cleanup_bad)
            raise PushChannelFailed(
                f"push PTY 通道已断: {node} -> {norm}", exc
            ) from exc
        # push 成功即证明该节点可达,清掉冷却记录;否则刚推完文件的节点
        # 仍会被 _run_pty 的冷却拒掉。只清不设,push 不参与冷却判定。
        self._pty_reconnect_failures.pop(node, None)

    @staticmethod
    def _pty_exec_with_deadline(pty, cmd: str, deadline: float, node: str, norm: str) -> str:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PushTimeout(f"push PTY 降级超时: {node} -> {norm}")
        exit_code, out, _capped = pty.exec(cmd, timeout=remaining)
        if exit_code is None:
            raise PushTimeout(f"push PTY 降级超时: {node} -> {norm}")
        if exit_code != 0:
            raise RuntimeError(f"push PTY 降级失败({exit_code}): {out}")
        return out

    @staticmethod
    def _best_effort_pty(pty, cmd: str) -> None:
        try:
            pty.exec(cmd, timeout=1.0)
        except Exception:
            pass

    # ---- 连接管理 ----

    def _transport(self, node: str, credential=None) -> paramiko.Transport:
        t = self._transports.get(node)
        if t is not None and t.is_active():
            self._last_connection_attempts[node] = 0
            return t
        platform_host = self._node_info(node).host
        last_error: Optional[BaseException] = None
        network_errors = (
            socket.timeout,
            ConnectionRefusedError,
            ConnectionResetError,
            TimeoutError,
            OSError,
        )

        for attempt in range(self._CONNECT_MAX_ATTEMPTS):
            time.sleep(self._CONNECT_BACKOFFS[attempt])
            transport = None
            try:
                if credential is not None:
                    cred = credential
                    credential = None
                else:
                    cred = self._credential(node)

                if not cred.key_file and not cred.password:
                    raise ValueError(
                        f"credentials for {platform_host} require password or key_file"
                    )
                actual_host = cred.host if cred.host else platform_host
                actual_port = cred.port
                sock = (
                    _open_socket(actual_host, actual_port, self._CONNECT_TIMEOUT_SECONDS)
                    if self._jump is None
                    else self._jump_channel(actual_host, actual_port)
                )
                transport = paramiko.Transport(sock)
                if cred.key_file:
                    transport.connect(
                        username=cred.user,
                        pkey=self._load_private_key(cred.key_file),
                    )
                else:
                    transport.connect(username=cred.user, password=cred.password)
                transport.set_keepalive(30)
                self._transports[node] = transport
                self._last_connection_attempts[node] = attempt + 1
                return transport
            except ValueError:
                raise
            except paramiko.AuthenticationException as exc:
                last_error = exc
                self._close_failed_transport(transport)
                # Credential rotation is an explicit account/manual action.
                # Do not delete or regenerate a token because one SSH attempt
                # failed; other consumers may still be using the same token.
                break
            except (CredentialProviderUnavailable, CredentialGenerateFailed) as exc:
                last_error = exc
                self._close_failed_transport(transport)
            except paramiko.SSHException as exc:
                last_error = exc
                self._close_failed_transport(transport)
                # Protocol retries reuse the same cached credential.  They are
                # transport retries only, never a trigger for token refresh.
            except network_errors as exc:
                last_error = exc
                self._close_failed_transport(transport)
            except SSHConnectFailed:
                # Missing local credentials are already classified as an
                # authentication failure; do not retry or refresh them here.
                raise
            except Exception as exc:
                last_error = exc
                self._close_failed_transport(transport)

        assert last_error is not None
        self._last_connection_attempts[node] = self._CONNECT_MAX_ATTEMPTS
        self._dispatch_ssh_failure(platform_host, last_error)
        raise AssertionError("unreachable")

    @staticmethod
    def _close_failed_transport(transport) -> None:
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass

    @staticmethod
    def _dispatch_ssh_failure(platform_host: str, last_error: BaseException) -> None:
        base = (
            f"SSH 连接失败: {platform_host}; "
            "凭证可能过期或认证失败，请在设置页手动生成凭证"
        )
        if isinstance(last_error, (
            paramiko.AuthenticationException,
            CredentialProviderUnavailable,
            CredentialGenerateFailed,
            CredentialUnavailable,
        )):
            raise SSHAuthFailed(f"{base} · 分类=认证", last_error) from last_error
        if isinstance(last_error, (
            socket.timeout,
            ConnectionRefusedError,
            ConnectionResetError,
            TimeoutError,
            OSError,
        )):
            raise SSHNetworkFailed(f"{base} · 分类=网络", last_error) from last_error
        if isinstance(last_error, paramiko.SSHException):
            raise SSHProtocolFailed(f"{base} · 分类=SSH协议", last_error) from last_error
        raise SSHConnectFailed(f"{base} · 分类=未知", last_error) from last_error

    def _credential(self, node: str):
        host = self._node_info(node).host
        try:
            credential = self._creds.get(host)
            self._last_credential_status[node] = (
                "cache" if credential.valid_to > 0 else "configured"
            )
            return credential
        except CredentialUnavailable as exc:
            self._last_credential_status[node] = "missing"
            raise SSHAuthFailed(
                f"SSH 连接失败: {host}; 本地凭证不存在，请先在设置页手动生成凭证 · 分类=认证",
                exc,
            ) from exc

    def transport_is_active(self, node: str) -> bool:
        transport = self._transports.get(node)
        try:
            return bool(transport is not None and transport.is_active())
        except Exception:
            return False

    def route_mode(self) -> str:
        return "jump" if self._jump is not None else "direct"

    def last_channel_mode(self, node: str) -> Optional[str]:
        return self._last_channel_modes.get(node)

    def last_connection_attempts(self, node: str) -> int:
        return int(self._last_connection_attempts.get(node, 0))

    def last_credential_status(self, node: str) -> str:
        return self._last_credential_status.get(node, "unknown")

    def _pty_channel(self, node: str) -> PtyChannel:
        chan = self._pty_channels.get(node)
        if chan is None or chan.dead:
            transport = self._transport(node)
            try:
                chan = PtyChannel(transport)
            except paramiko.SSHException as exc:
                # 服务端拒开通道(如堡垒机会话上限 "Administratively prohibited"):弃用该连接,
                # 下一条命令重建;本条按网络类失败交回调用方,不能当未知异常把任务打挂
                self._transports.pop(node, None)
                self._close_failed_transport(transport)
                raise SSHNetworkFailed(f"SSH 连接失败: {node}; PTY 通道打开失败({exc})", exc) from exc
            self._pty_channels[node] = chan
        return chan

    @staticmethod
    def _load_private_key(path: str):
        last_error = None
        for key_cls in (paramiko.Ed25519Key, paramiko.RSAKey):
            try:
                return key_cls.from_private_key_file(path)
            except Exception as exc:
                last_error = exc
        raise last_error

    def _jump_channel(self, target_host: str, target_port: int = 22):
        if self._jump_client is None or self._jump_client.get_transport() is None \
                or not self._jump_client.get_transport().is_active():
            c = paramiko.SSHClient()
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            c.connect(self._jump.host, port=self._jump.port, username=self._jump.user,
                      key_filename=self._jump.key_file, look_for_keys=True, timeout=10)
            self._jump_client = c
        jt = self._jump_client.get_transport()
        return jt.open_channel("direct-tcpip", (target_host, target_port), ("127.0.0.1", 0))

    def close(self) -> None:
        for t in self._transports.values():
            t.close()
        self._transports.clear()
        self._pty_channels.clear()
        self._pty_reconnect_failures.clear()
        self._last_connection_attempts.clear()
        self._last_credential_status.clear()
        self._last_channel_modes.clear()
        if self._jump_client is not None:
            self._jump_client.close()
            self._jump_client = None
