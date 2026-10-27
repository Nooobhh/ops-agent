import socket
from unittest.mock import Mock, call

import paramiko
import pytest

from lib.cmd_filter import CmdFilter, default_rules_path
from lib.config import NodeInfo, PlatformConfig
from lib.credentials import (
    Credential,
    CredentialMissing,
    CredentialProviderUnavailable,
)
from lib.ssh_pool import _open_socket as _real_open_socket
from lib.ssh_pool import (
    SSHAuthFailed,
    SSHNetworkFailed,
    SSHProtocolFailed,
    SSHPool,
)


def make_pool(creds):
    platform = PlatformConfig("alpha", {"node": NodeInfo(host="198.51.100.101")})
    return SSHPool(platform, creds, CmdFilter.from_yaml(default_rules_path()))


class FakeTransport:
    connect_effects = []
    instances = []

    def __init__(self, sock):
        self.sock = sock
        self.keepalive = None
        self.closed = False
        self.__class__.instances.append(self)

    def connect(self, **kwargs):
        self.connect_kwargs = kwargs
        effect = self.__class__.connect_effects.pop(0) if self.__class__.connect_effects else None
        if isinstance(effect, BaseException):
            raise effect

    def set_keepalive(self, seconds):
        self.keepalive = seconds

    def is_active(self):
        return True

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def reset_transport():
    FakeTransport.connect_effects = []
    FakeTransport.instances = []


def test_missing_credential_is_auth_failure_without_refresh(monkeypatch):
    creds = Mock()
    creds.get.side_effect = CredentialMissing("198.51.100.101")
    monkeypatch.setattr("lib.ssh_pool.time.sleep", Mock())
    with pytest.raises(SSHAuthFailed, match="手动生成凭证"):
        make_pool(creds)._transport("node")
    creds.refresh.assert_not_called()
    assert FakeTransport.instances == []


def test_public_run_uses_cached_credential_without_expiry_refresh(monkeypatch):
    creds = Mock()
    creds.get.return_value = Credential("u", "p", pty=True, valid_to=1)
    pool = make_pool(creds)
    expected = Mock()
    monkeypatch.setattr(pool, "_run_pty", Mock(return_value=expected))

    result = pool.run("node", "uptime")

    assert result is expected
    creds.refresh.assert_not_called()
    pool._run_pty.assert_called_once_with("node", "uptime", 30.0, None)


def test_auth_failure_does_not_force_refresh_or_replace_token(monkeypatch):
    creds = Mock()
    creds.get.return_value = Credential("u", "p")
    FakeTransport.connect_effects = [
        paramiko.AuthenticationException("bad1"),
        paramiko.AuthenticationException("bad2"),
        None,
    ]
    monkeypatch.setattr("lib.ssh_pool.paramiko.Transport", FakeTransport)
    monkeypatch.setattr("lib.ssh_pool.time.sleep", Mock())
    with pytest.raises(SSHAuthFailed, match="手动生成凭证"):
        make_pool(creds)._transport("node")
    creds.delete.assert_not_called()
    creds.refresh.assert_not_called()
    assert len(FakeTransport.instances) == 1


def test_all_auth_failures_are_classified(monkeypatch):
    creds = Mock()
    creds.get.return_value = Credential("u", "p")
    FakeTransport.connect_effects = [paramiko.AuthenticationException("bad") for _ in range(3)]
    monkeypatch.setattr("lib.ssh_pool.paramiko.Transport", FakeTransport)
    monkeypatch.setattr("lib.ssh_pool.time.sleep", Mock())
    with pytest.raises(SSHAuthFailed, match="分类=认证") as exc:
        make_pool(creds)._transport("node")
    assert isinstance(exc.value.__cause__, paramiko.AuthenticationException)


@pytest.mark.parametrize("error,expected,classification", [
    (socket.timeout("slow"), SSHNetworkFailed, "分类=网络"),
    (paramiko.SSHException("protocol"), SSHProtocolFailed, "分类=SSH协议"),
])
def test_network_and_protocol_failures_keep_cache(monkeypatch, error, expected, classification):
    creds = Mock()
    creds.get.return_value = Credential("u", "p")
    FakeTransport.connect_effects = [error, error, error]
    monkeypatch.setattr("lib.ssh_pool.paramiko.Transport", FakeTransport)
    monkeypatch.setattr("lib.ssh_pool.time.sleep", Mock())
    with pytest.raises(expected, match=classification):
        make_pool(creds)._transport("node")
    creds.delete.assert_not_called()


def test_protocol_error_retries_transport_without_refresh(monkeypatch):
    """协议重试复用同一凭证，不触发 token 替换。"""
    creds = Mock()
    creds.get.return_value = Credential("u", "p")
    FakeTransport.connect_effects = [paramiko.SSHException("banner reset"), None]
    monkeypatch.setattr("lib.ssh_pool.paramiko.Transport", FakeTransport)
    monkeypatch.setattr("lib.ssh_pool.time.sleep", Mock())

    result = make_pool(creds)._transport("node")

    assert result is FakeTransport.instances[1]
    creds.refresh.assert_not_called()
    assert creds.get.call_count == 2
    creds.delete.assert_not_called()


def test_protocol_error_never_refreshes_credential(monkeypatch):
    """真协议故障时也不反复刷新 token。"""
    creds = Mock()
    creds.get.return_value = Credential("u", "p")
    FakeTransport.connect_effects = [paramiko.SSHException("protocol") for _ in range(3)]
    monkeypatch.setattr("lib.ssh_pool.paramiko.Transport", FakeTransport)
    monkeypatch.setattr("lib.ssh_pool.time.sleep", Mock())

    with pytest.raises(SSHProtocolFailed, match="分类=SSH协议"):
        make_pool(creds)._transport("node")

    creds.refresh.assert_not_called()
    assert creds.get.call_count == 3


def test_missing_credential_three_times_is_auth_failure(monkeypatch):
    creds = Mock()
    creds.get.side_effect = CredentialMissing("198.51.100.101")
    monkeypatch.setattr("lib.ssh_pool.time.sleep", Mock())
    with pytest.raises(SSHAuthFailed, match="分类=认证"):
        make_pool(creds)._transport("node")


def test_unavailable_credential_provider_is_auth_failure(monkeypatch):
    creds = Mock()
    creds.get.side_effect = CredentialProviderUnavailable("网关", RuntimeError("down"))
    monkeypatch.setattr("lib.ssh_pool.time.sleep", Mock())
    with pytest.raises(SSHAuthFailed, match="分类=认证"):
        make_pool(creds)._transport("node")
    assert creds.get.call_count == 3


def test_auth_failure_preserves_old_cache_and_does_not_retry_token(monkeypatch):
    creds = Mock()
    creds.get.return_value = Credential("u", "old-password")
    FakeTransport.connect_effects = [
        paramiko.AuthenticationException("bad1"),
        paramiko.AuthenticationException("bad2"),
        paramiko.AuthenticationException("bad3"),
    ]
    sleep = Mock()
    monkeypatch.setattr("lib.ssh_pool.paramiko.Transport", FakeTransport)
    monkeypatch.setattr("lib.ssh_pool.time.sleep", sleep)
    with pytest.raises(SSHAuthFailed, match="分类=认证"):
        make_pool(creds)._transport("node")
    creds.delete.assert_not_called()
    assert creds.get.call_count == 1
    creds.refresh.assert_not_called()
    assert [item.connect_kwargs["password"] for item in FakeTransport.instances] == [
        "old-password",
    ]
    assert sleep.call_args_list == [call(0.0)]


def test_success_caches_transport_keepalive_and_backoffs(monkeypatch):
    creds = Mock()
    creds.get.return_value = Credential("u", "p")
    sleep = Mock()
    FakeTransport.connect_effects = [socket.timeout("one"), paramiko.SSHException("two"), None]
    monkeypatch.setattr("lib.ssh_pool.paramiko.Transport", FakeTransport)
    monkeypatch.setattr("lib.ssh_pool.time.sleep", sleep)
    pool = make_pool(creds)
    result = pool._transport("node")
    assert result.keepalive == 30
    assert pool._transports["node"] is result
    assert sleep.call_args_list == [call(0.0), call(2.0), call(5.0)]


def test_direct_connect_is_bounded_by_a_timeout(monkeypatch):
    creds = Mock()
    creds.get.return_value = Credential("u", "p")
    seen = []
    monkeypatch.setattr(
        "lib.ssh_pool._open_socket",
        lambda host, port, timeout: seen.append((host, port, timeout)) or (host, port),
    )
    monkeypatch.setattr("lib.ssh_pool.paramiko.Transport", FakeTransport)

    make_pool(creds)._transport("node")

    assert seen == [("198.51.100.101", 22, SSHPool._CONNECT_TIMEOUT_SECONDS)]


def test_connect_timeout_is_retried_then_classified_as_network(monkeypatch):
    creds = Mock()
    creds.get.return_value = Credential("u", "p")
    attempts = []

    def unreachable(host, port, timeout):
        attempts.append(host)
        raise socket.timeout("timed out")

    monkeypatch.setattr("lib.ssh_pool._open_socket", unreachable)
    monkeypatch.setattr("lib.ssh_pool.paramiko.Transport", FakeTransport)
    monkeypatch.setattr("lib.ssh_pool.time.sleep", Mock())

    with pytest.raises(SSHNetworkFailed, match="分类=网络"):
        make_pool(creds)._transport("node")
    assert len(attempts) == SSHPool._CONNECT_MAX_ATTEMPTS
    assert FakeTransport.instances == []


def test_open_socket_passes_the_timeout_to_the_tcp_connect(monkeypatch):
    from lib import ssh_pool

    calls = []
    monkeypatch.setattr(
        ssh_pool.socket, "create_connection",
        lambda address, timeout: calls.append((address, timeout)) or "sock",
    )

    assert _real_open_socket("198.51.100.101", 22, 10.0) == "sock"
    assert calls == [(("198.51.100.101", 22), 10.0)]
