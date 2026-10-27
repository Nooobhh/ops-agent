"""conftest 硬闸自检:测试进程连不出回环之外,调度器拿不到真实采集 tick。"""
import socket

import pytest

import lib.scheduler as scheduler_module


def test_non_loopback_connect_is_refused_before_leaving_the_host():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(ConnectionRefusedError, match="测试禁止连接外部地址"):
            sock.connect(("192.0.2.10", 80))
        with pytest.raises(ConnectionRefusedError):
            sock.connect_ex(("198.51.100.7", 443))


def test_loopback_connect_is_allowed():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    with server, socket.create_connection(server.getsockname(), timeout=2):
        pass


def test_scheduler_ticks_refuse_unless_a_test_replaces_them():
    for runner in (scheduler_module.alert_poll, scheduler_module.resource_collect):
        with pytest.raises(RuntimeError, match="测试禁止调度器跑真实采集 tick"):
            runner.run(["--state-dir", "/nonexistent"])
