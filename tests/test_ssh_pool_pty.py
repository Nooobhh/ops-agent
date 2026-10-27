import base64
import hashlib
import shlex

import pytest

import lib.ssh_pool as ssh_pool_module
from lib.cmd_filter import CmdFilter, CommandDenied, default_rules_path
from lib.config import NodeInfo, PlatformConfig
from lib.credentials import Credential
from lib.pty_channel import PtyChannelDead
from lib.ssh_pool import (
    PushChannelFailed,
    PushDenied,
    PushIntegrityError,
    PushTimeout,
    SSHNetworkFailed,
    SSHPool,
)


class Creds:
    def __init__(self, cred):
        self.cred = cred

    def get(self, host):
        return self.cred


def _pool(cred):
    platform = PlatformConfig("alpha", {"node12": NodeInfo(host="198.51.100.112")})
    return SSHPool(platform, Creds(cred), CmdFilter.from_yaml(default_rules_path()))


class FakePty:
    created = []
    digest = None

    def __init__(self, transport):
        self.transport = transport
        self.dead = False
        self.commands = []
        FakePty.created.append(self)

    def exec(self, cmd, timeout, max_output_bytes=None):
        self.commands.append((cmd, timeout))
        if cmd.startswith("sha256sum "):
            return 0, f"{self.digest or '0' * 64}  remote\n", False
        return 0, "ok\n", False


class TimeoutPty(FakePty):
    def exec(self, cmd, timeout, max_output_bytes=None):
        self.commands.append((cmd, timeout))
        return None, "partial", False


class ReusedDeadPty(FakePty):
    def exec(self, cmd, timeout, max_output_bytes=None):
        self.commands.append((cmd, timeout))
        if len(FakePty.created) == 1 and len(self.commands) > 1:
            self.dead = True
            raise PtyChannelDead("Socket is closed", retryable=True)
        if len(FakePty.created) > 1:
            return 0, "recovered\n", False
        return 0, "ok\n", False


class NewDeadPty(FakePty):
    def exec(self, cmd, timeout, max_output_bytes=None):
        self.commands.append((cmd, timeout))
        self.dead = True
        raise PtyChannelDead("Socket is closed", retryable=True)


class PartialDeadPty(FakePty):
    def exec(self, cmd, timeout, max_output_bytes=None):
        self.commands.append((cmd, timeout))
        if len(self.commands) > 1:
            self.dead = True
            raise PtyChannelDead("Socket is closed", retryable=False)
        return 0, "ok\n", False


class ReconnectFailurePty(FakePty):
    def exec(self, cmd, timeout, max_output_bytes=None):
        self.commands.append((cmd, timeout))
        if len(FakePty.created) == 1:
            if len(self.commands) == 1:
                return 0, "ok\n", False
            self.dead = True
            raise PtyChannelDead("cached channel closed", retryable=True)
        if len(FakePty.created) == 2:
            self.dead = True
            raise PtyChannelDead("reconnect failed", retryable=True)
        return 0, "recovered\n", False


class FakeTransport:
    def __init__(self, sock=None):
        self.sock = sock
        self.connected = None
        self.keepalive = None

    def connect(self, **kwargs):
        self.connected = kwargs

    def set_keepalive(self, seconds):
        self.keepalive = seconds

    def is_active(self):
        return True

    def open_session(self, timeout=None):
        raise RuntimeError("sftp disabled")


def test_pty_run_reuses_channel_and_rebuilds_dead(monkeypatch):
    FakePty.created = []
    monkeypatch.setattr("lib.ssh_pool.PtyChannel", FakePty)
    pool = _pool(Credential("root", "pw", pty=True))
    pool._transports["node12"] = FakeTransport()

    r1 = pool.run("node12", "echo ok")
    FakePty.created[0].dead = True
    r2 = pool.run("node12", "echo again")

    assert r1.stdout == "ok\n" and r1.stderr == "" and not r1.timed_out
    assert r2.stdout == "ok\n"
    assert len(FakePty.created) == 2


def test_pty_run_retries_reused_channel_after_zero_byte_failure(monkeypatch):
    FakePty.created = []
    monkeypatch.setattr("lib.ssh_pool.PtyChannel", ReusedDeadPty)
    pool = _pool(Credential("root", "pw", pty=True))
    pool._transports["node12"] = FakeTransport()

    first = pool.run("node12", "echo first")
    second = pool.run("node12", "echo second")

    assert first.stdout == "ok\n"
    assert second.stdout == "recovered\n"
    assert len(FakePty.created) == 2


def test_pty_run_does_not_retry_new_channel_failure(monkeypatch):
    FakePty.created = []
    monkeypatch.setattr("lib.ssh_pool.PtyChannel", NewDeadPty)
    pool = _pool(Credential("root", "pw", pty=True))
    pool._transports["node12"] = FakeTransport()

    with pytest.raises(SSHNetworkFailed, match="node12.*PTY 通道已断"):
        pool.run("node12", "echo fail")

    assert len(FakePty.created) == 1


def test_pty_run_does_not_retry_after_partial_send_failure(monkeypatch):
    FakePty.created = []
    monkeypatch.setattr("lib.ssh_pool.PtyChannel", PartialDeadPty)
    pool = _pool(Credential("root", "pw", pty=True))
    pool._transports["node12"] = FakeTransport()

    first = pool.run("node12", "echo first")
    with pytest.raises(SSHNetworkFailed, match="node12.*PTY 通道已断"):
        pool.run("node12", "echo second")

    assert first.stdout == "ok\n"
    assert len(FakePty.created) == 1


def test_pty_retry_uses_remaining_timeout_budget(monkeypatch):
    FakePty.created = []
    monkeypatch.setattr("lib.ssh_pool.PtyChannel", ReusedDeadPty)
    pool = _pool(Credential("root", "pw", pty=True))
    pool._transports["node12"] = FakeTransport()
    cached = ReusedDeadPty(pool._transports["node12"])
    cached.commands.append(("echo first", 10.0))
    pool._pty_channels["node12"] = cached
    ticks = iter([10.0, 13.0, 14.0])
    monkeypatch.setattr("lib.ssh_pool.time.monotonic", lambda: next(ticks))

    result = pool.run("node12", "echo second", timeout=10.0)

    assert result.stdout == "recovered\n"
    assert FakePty.created[1].commands[0][1] == pytest.approx(7.0)


def test_pty_retry_stops_when_timeout_budget_is_exhausted(monkeypatch):
    FakePty.created = []
    monkeypatch.setattr("lib.ssh_pool.PtyChannel", ReusedDeadPty)
    pool = _pool(Credential("root", "pw", pty=True))
    pool._transports["node12"] = FakeTransport()
    cached = ReusedDeadPty(pool._transports["node12"])
    cached.commands.append(("echo first", 10.0))
    pool._pty_channels["node12"] = cached
    ticks = iter([10.0, 20.0])
    monkeypatch.setattr("lib.ssh_pool.time.monotonic", lambda: next(ticks))

    with pytest.raises(SSHNetworkFailed, match="node12.*PTY 通道已断"):
        pool.run("node12", "echo second", timeout=10.0)

    assert len(FakePty.created) == 1


def test_pty_reconnect_failure_cools_down_then_success_clears_memory(monkeypatch):
    FakePty.created = []
    monkeypatch.setattr("lib.ssh_pool.PtyChannel", ReconnectFailurePty)
    now = [100.0]
    monkeypatch.setattr("lib.ssh_pool.time.monotonic", lambda: now[0])
    pool = _pool(Credential("root", "pw", pty=True))
    pool._transports["node12"] = FakeTransport()

    assert pool.run("node12", "echo first").stdout == "ok\n"
    with pytest.raises(SSHNetworkFailed, match="node12.*PTY 通道已断"):
        pool.run("node12", "echo second")
    assert len(FakePty.created) == 2

    with pytest.raises(SSHNetworkFailed, match="node12"):
        pool.run("node12", "echo cooldown")
    assert len(FakePty.created) == 2

    now[0] += ssh_pool_module._PTY_RECONNECT_FAILURE_WINDOW + 1
    assert pool.run("node12", "echo recovered").stdout == "recovered\n"
    assert "node12" not in pool._pty_reconnect_failures


def test_cached_dead_channel_connect_failure_enters_cooldown(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("lib.ssh_pool.time.monotonic", lambda: now[0])
    pool = _pool(Credential("root", "pw", pty=True))
    pool._transports["node12"] = FakeTransport()
    cached = FakePty(pool._transports["node12"])
    cached.dead = True
    pool._pty_channels["node12"] = cached
    attempts = []

    def fail_reconnect(node):
        attempts.append(node)
        raise SSHNetworkFailed("connect failed", OSError("offline"))

    monkeypatch.setattr(pool, "_pty_channel", fail_reconnect)

    with pytest.raises(SSHNetworkFailed, match="connect failed"):
        pool.run("node12", "echo first")
    with pytest.raises(SSHNetworkFailed, match="node12.*重连冷却中"):
        pool.run("node12", "echo second")

    assert attempts == ["node12"]


def test_cold_pool_connect_failure_never_enters_cooldown(monkeypatch):
    """池中从无通道时不武装冷却:否则从未连上的节点会被「冷却中」盖住真实配置错误。"""
    now = [100.0]
    monkeypatch.setattr("lib.ssh_pool.time.monotonic", lambda: now[0])
    pool = _pool(Credential("root", "pw", pty=True))
    pool._transports["node12"] = FakeTransport()
    attempts = []

    def fail_reconnect(node):
        attempts.append(node)
        raise SSHNetworkFailed("connect failed", OSError("offline"))

    monkeypatch.setattr(pool, "_pty_channel", fail_reconnect)

    for _ in range(3):
        with pytest.raises(SSHNetworkFailed, match="connect failed"):
            pool.run("node12", "echo cold")

    assert attempts == ["node12"] * 3
    assert "node12" not in pool._pty_reconnect_failures


def test_push_success_clears_reconnect_cooldown(monkeypatch, tmp_path):
    """push 成功即证明节点可达,须清冷却,否则刚推完文件的节点仍被 run() 拒掉。"""
    payload = b"payload"
    local = tmp_path / "payload.bin"
    local.write_bytes(payload)
    FakePty.created = []
    FakePty.digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr("lib.ssh_pool.PtyChannel", FakePty)
    pool = _pool(Credential("root", "pw", pty=True))
    pool._transports["node12"] = FakeTransport()
    pool._pty_reconnect_failures["node12"] = (0.0, OSError("offline"))

    pool.push("node12", str(local), "/tmp/opsagent/out.bin", timeout=5)

    assert "node12" not in pool._pty_reconnect_failures


def test_non_pty_run_uses_exec_path(monkeypatch):
    from tests.test_ssh_pool import FakeChan, FakeTransport as ExecTransport

    pool = _pool(Credential("root", "pw", pty=False))
    ft = ExecTransport([FakeChan(stdout=b"exec\n", exit_code=0)])
    pool._transports["node12"] = ft

    assert pool.run("node12", "echo exec").stdout == "exec\n"
    assert ft.opened == 1


def test_redline_check_runs_before_pty_dispatch(monkeypatch):
    FakePty.created = []
    monkeypatch.setattr("lib.ssh_pool.PtyChannel", FakePty)
    pool = _pool(Credential("root", "pw", pty=True))
    pool._transports["node12"] = FakeTransport()

    with pytest.raises(CommandDenied):
        pool.run("node12", "rm /tmp/x")

    assert FakePty.created == []


def test_pty_push_base64_fallback_roundtrip(monkeypatch, tmp_path):
    payload = b"\x00hello\xe4\xb8\xad\xe6\x96\x87"
    local = tmp_path / "payload.bin"
    local.write_bytes(payload)
    FakePty.created = []
    FakePty.digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr("lib.ssh_pool.PtyChannel", FakePty)
    pool = _pool(Credential("root", "pw", pty=True))
    pool._transports["node12"] = FakeTransport()

    pool.push("node12", str(local), "/tmp/opsagent/out.bin", timeout=2)

    commands = [cmd for cmd, _ in FakePty.created[0].commands]
    b64 = "".join(
        shlex.split(cmd)[2]
        for cmd in commands
        if cmd.startswith("printf %s ")
    )
    assert base64.b64decode(b64.encode("ascii")) == payload
    assert "base64 -d /tmp/opsagent/out.bin.b64 > /tmp/opsagent/out.bin" in commands
    assert "sha256sum /tmp/opsagent/out.bin | awk '{print $1}'" in commands
    assert all(len(shlex.split(cmd)[2]) <= 32768 for cmd in commands if cmd.startswith("printf %s "))


def test_pty_push_path_guard_and_timeout(monkeypatch, tmp_path):
    local = tmp_path / "payload.bin"
    local.write_bytes(b"x")
    monkeypatch.setattr("lib.ssh_pool.PtyChannel", TimeoutPty)
    pool = _pool(Credential("root", "pw", pty=True))
    pool._transports["node12"] = FakeTransport()

    with pytest.raises(PushDenied):
        pool.push("node12", str(local), "/etc/out")
    with pytest.raises(PushTimeout):
        pool.push("node12", str(local), "/tmp/opsagent/out", timeout=0.1)


def test_pty_push_base64_uses_shared_timeout_budget(monkeypatch, tmp_path):
    payload = b"x" * 30000
    local = tmp_path / "payload.bin"
    local.write_bytes(payload)
    FakePty.created = []
    FakePty.digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr("lib.ssh_pool.PtyChannel", FakePty)
    ticks = iter([0.0, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90])
    monkeypatch.setattr("lib.ssh_pool.time.monotonic", lambda: next(ticks))
    pool = _pool(Credential("root", "pw", pty=True))
    pool._transports["node12"] = FakeTransport()

    pool.push("node12", str(local), "/tmp/opsagent/out.bin", timeout=1.0)

    timeouts = [timeout for _, timeout in FakePty.created[0].commands]
    assert any(t < 1.0 for t in timeouts[1:])


def test_pty_push_digest_mismatch_raises_and_cleans_remote(monkeypatch, tmp_path):
    local = tmp_path / "payload.bin"
    local.write_bytes(b"payload")
    FakePty.created = []
    FakePty.digest = "0" * 64
    monkeypatch.setattr("lib.ssh_pool.PtyChannel", FakePty)
    pool = _pool(Credential("root", "pw", pty=True))
    pool._transports["node12"] = FakeTransport()

    with pytest.raises(PushIntegrityError):
        pool.push("node12", str(local), "/tmp/opsagent/out.bin", timeout=1.0)

    commands = [cmd for cmd, _ in FakePty.created[0].commands]
    assert "rm -f /tmp/opsagent/out.bin /tmp/opsagent/out.bin.b64" in commands


def test_pty_push_channel_failure_is_named_and_not_retried(monkeypatch, tmp_path):
    local = tmp_path / "payload.bin"
    local.write_bytes(b"payload")
    FakePty.created = []
    monkeypatch.setattr("lib.ssh_pool.PtyChannel", PartialDeadPty)
    pool = _pool(Credential("root", "pw", pty=True))
    pool._transports["node12"] = FakeTransport()

    with pytest.raises(PushChannelFailed) as exc_info:
        pool._push_pty_base64(
            "node12", str(local), "/tmp/opsagent/out.bin", timeout=1.0
        )

    assert "node12" in str(exc_info.value)
    assert len(FakePty.created) == 1
    assert FakePty.created[0].commands[-1][0] == (
        "rm -f /tmp/opsagent/out.bin /tmp/opsagent/out.bin.b64"
    )


def test_connection_failure_points_to_manual_credential_generation(monkeypatch):
    class BadTransport(FakeTransport):
        def connect(self, **kwargs):
            raise OSError("auth failed")

    monkeypatch.setattr("lib.ssh_pool.paramiko.Transport", BadTransport)
    monkeypatch.setattr("lib.ssh_pool.time.sleep", lambda _seconds: None)  # 跳过 0/2/5s 重试退避
    pool = _pool(Credential("root", "pw"))

    with pytest.raises(RuntimeError, match="手动生成凭证"):
        pool._transport("node12")


class RefusedPty(FakePty):
    def __init__(self, transport):
        import paramiko
        raise paramiko.ChannelException(1, "Administratively prohibited")


def test_pty_channel_open_refusal_is_network_failure_and_drops_transport(monkeypatch):
    monkeypatch.setattr("lib.ssh_pool.PtyChannel", RefusedPty)
    pool = _pool(Credential("root", "pw", pty=True))
    transport = FakeTransport()
    transport.closed = False
    transport.close = lambda: setattr(transport, "closed", True)
    pool._transports["node12"] = transport

    with pytest.raises(SSHNetworkFailed, match="node12.*PTY 通道打开失败.*Administratively prohibited"):
        pool.run("node12", "echo hi")

    # 弃用被拒的连接,下一条命令重建而不是在同一连接上反复被拒
    assert "node12" not in pool._transports and transport.closed
