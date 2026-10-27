import socket
import time

import paramiko
import pytest

import lib.ssh_pool as ssh_pool_mod
from lib.cmd_filter import CmdFilter, default_rules_path
from lib.config import NodeInfo, PlatformConfig
from lib.credentials import Credential
from lib.ssh_pool import PushTimeout, SSHPool


class Creds:
    def get(self, host):
        return Credential("root", "pw")


class PtyCreds:
    def get(self, host):
        return Credential("root", "pw", pty=True)


class FakeTransport:
    def __init__(self, chan):
        self.chan = chan
        self.opened_with = None

    def is_active(self):
        return True

    def open_session(self, timeout=None):
        self.opened_with = timeout
        if isinstance(self.chan, BaseException):
            raise self.chan
        return self.chan


class FakeChannel:
    def __init__(self, *, invoke_error=None):
        self.timeout = None
        self.invoke_error = invoke_error
        self.closed = False

    def settimeout(self, timeout):
        self.timeout = timeout

    def invoke_subsystem(self, name):
        assert name == "sftp"
        if self.invoke_error:
            raise self.invoke_error

    def close(self):
        self.closed = True


class BlockingInvokeChannel(FakeChannel):
    def invoke_subsystem(self, name):
        while not self.closed:
            time.sleep(0.01)


@pytest.fixture
def pool():
    platform = PlatformConfig("alpha", {"node12": NodeInfo(host="198.51.100.112")})
    return SSHPool(platform, Creds(), CmdFilter.from_yaml(default_rules_path()))


def test_push_put_timeout_keeps_transport_active(pool, monkeypatch, tmp_path):
    local = tmp_path / "payload.bin"
    local.write_bytes(b"payload")
    chan = FakeChannel()
    transport = FakeTransport(chan)
    pool._transports["node12"] = transport

    class FakeSFTP:
        def __init__(self, channel):
            assert channel is chan
            self.closed = False

        def put(self, local_path, remote_path):
            raise socket.timeout("put timed out")

        def close(self):
            self.closed = True

    monkeypatch.setattr("lib.ssh_pool.paramiko.SFTPClient", FakeSFTP)

    with pytest.raises(PushTimeout):
        pool.push("node12", str(local), "/tmp/opsagent/payload.bin", timeout=0.01)

    assert transport.is_active()
    assert transport.opened_with == 0.01
    assert chan.timeout == 0.01
    assert chan.closed
    assert pool._transports["node12"] is transport


@pytest.mark.parametrize(
    "chan",
    [
        socket.timeout("open timed out"),
        paramiko.SSHException("Timeout opening channel."),
        FakeChannel(invoke_error=socket.timeout("invoke timed out")),
    ],
)
def test_push_setup_timeout_keeps_transport_active(pool, monkeypatch, tmp_path, chan):
    local = tmp_path / "payload.bin"
    local.write_bytes(b"payload")
    transport = FakeTransport(chan)
    pool._transports["node12"] = transport

    class FakeSFTP:
        def __init__(self, channel):
            raise AssertionError("SFTPClient must not be constructed after setup timeout")

    monkeypatch.setattr("lib.ssh_pool.paramiko.SFTPClient", FakeSFTP)

    with pytest.raises(PushTimeout):
        pool.push("node12", str(local), "/tmp/opsagent/payload.bin", timeout=0.01)

    assert transport.is_active()
    if not isinstance(chan, BaseException):
        assert chan.closed


def test_pty_push_open_session_timeout_does_not_fallback(monkeypatch, tmp_path):
    local = tmp_path / "payload.bin"
    local.write_bytes(b"payload")
    platform = PlatformConfig("alpha", {"node12": NodeInfo(host="198.51.100.112")})
    pool = SSHPool(platform, PtyCreds(), CmdFilter.from_yaml(default_rules_path()))
    transport = FakeTransport(paramiko.SSHException("Timeout opening channel."))
    pool._transports["node12"] = transport

    class FakePty:
        def __init__(self, transport):
            raise AssertionError("open_session timeout must not fall back to PTY base64 push")

    monkeypatch.setattr("lib.ssh_pool.PtyChannel", FakePty)

    with pytest.raises(PushTimeout):
        pool.push("node12", str(local), "/tmp/opsagent/payload.bin", timeout=0.01)


def test_push_invoke_subsystem_hang_is_bounded(pool, monkeypatch, tmp_path):
    local = tmp_path / "payload.bin"
    local.write_bytes(b"payload")
    chan = BlockingInvokeChannel()
    transport = FakeTransport(chan)
    pool._transports["node12"] = transport

    class FakeSFTP:
        def __init__(self, channel):
            raise AssertionError("SFTPClient must not be constructed after subsystem timeout")

    monkeypatch.setattr("lib.ssh_pool.paramiko.SFTPClient", FakeSFTP)

    started = time.monotonic()
    with pytest.raises(PushTimeout):
        pool.push("node12", str(local), "/tmp/opsagent/payload.bin", timeout=0.05)

    assert time.monotonic() - started < 1.0
    assert transport.is_active()
    assert chan.closed


# ---- SFTP 瞬时故障重试(node16 真机偶发 SFTP glitch 触发降级卡死的教训)----

class RetryTransport:
    """按 script 顺序回放每次 open_session 的行为:异常实例=raise,FakeChannel=return。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def is_active(self):
        return True

    def open_session(self, timeout=None):
        idx = min(self.calls, len(self.script) - 1)
        item = self.script[idx]
        self.calls += 1
        if isinstance(item, BaseException):
            raise item
        return item


def _no_sleep(monkeypatch):
    monkeypatch.setattr(ssh_pool_mod.time, "sleep", lambda s: None)


def test_push_transient_sftp_error_retries_and_succeeds(pool, monkeypatch, tmp_path):
    _no_sleep(monkeypatch)
    chan = FakeChannel()
    transport = RetryTransport([
        paramiko.SSHException("EOF during negotiation"),
        paramiko.SSHException("EOF during negotiation"),
        chan,
    ])
    pool._transports["node12"] = transport
    puts = []

    class FakeSFTP:
        def __init__(self, channel):
            assert channel is chan
            self.closed = False

        def put(self, local_path, remote_path):
            puts.append((local_path, remote_path))

        def close(self):
            self.closed = True

    monkeypatch.setattr("lib.ssh_pool.paramiko.SFTPClient", FakeSFTP)

    local = tmp_path / "payload.bin"
    local.write_bytes(b"payload")
    pool.push("node12", str(local), "/tmp/opsagent/payload.bin", timeout=1)

    assert transport.calls == 3
    assert puts == [(str(local), "/tmp/opsagent/payload.bin")]


def test_push_persistent_sftp_error_falls_back_to_base64_after_retries(monkeypatch, tmp_path):
    _no_sleep(monkeypatch)
    platform = PlatformConfig("alpha", {"node12": NodeInfo(host="198.51.100.112")})

    class PtyCreds:
        def get(self, host):
            return Credential("root", "pw", pty=True)

    pool = SSHPool(platform, PtyCreds(), CmdFilter.from_yaml(default_rules_path()))
    transport = RetryTransport([paramiko.SSHException("EOF")] * 5)
    pool._transports["node12"] = transport

    b64_called = []

    def fake_base64(node, local, norm, timeout):
        b64_called.append((node, norm))

    monkeypatch.setattr(pool, "_push_pty_base64", fake_base64)

    local = tmp_path / "payload.bin"
    local.write_bytes(b"payload")
    pool.push("node12", str(local), "/tmp/opsagent/payload.bin", timeout=1)

    assert transport.calls == 3
    assert b64_called == [("node12", "/tmp/opsagent/payload.bin")]


def test_push_non_pty_persistent_failure_raises_after_retries(pool, monkeypatch, tmp_path):
    _no_sleep(monkeypatch)
    transport = RetryTransport([paramiko.SSHException("EOF")] * 5)
    pool._transports["node12"] = transport

    local = tmp_path / "payload.bin"
    local.write_bytes(b"payload")
    with pytest.raises(paramiko.SSHException):
        pool.push("node12", str(local), "/tmp/opsagent/payload.bin", timeout=1)

    assert transport.calls == 3


def test_push_timeout_does_not_trigger_retry(pool, monkeypatch, tmp_path):
    slept = []
    monkeypatch.setattr(ssh_pool_mod.time, "sleep", lambda s: slept.append(s))
    transport = RetryTransport([socket.timeout("open timed out")] * 5)
    pool._transports["node12"] = transport

    local = tmp_path / "payload.bin"
    local.write_bytes(b"payload")
    with pytest.raises(PushTimeout):
        pool.push("node12", str(local), "/tmp/opsagent/payload.bin", timeout=0.01)

    assert transport.calls == 1
    assert slept == []
