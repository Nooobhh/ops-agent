import inspect
import time

from lib.cmd_filter import CmdFilter, default_rules_path
from lib.config import NodeInfo, PlatformConfig
from lib.credentials import Credential
from lib.ssh_pool import CmdResult, SSHPool


class BufferedChannel:
    def __init__(self, stdout: bytes, *, eof: bool = True):
        self._stdout = bytearray(stdout)
        self.eof_received = eof

    def recv_ready(self):
        return bool(self._stdout)

    def recv(self, size):
        data = bytes(self._stdout[:size])
        del self._stdout[:size]
        return data

    def recv_stderr_ready(self):
        return False

    def recv_stderr(self, size):
        raise AssertionError("stderr is not ready")

    def exit_status_ready(self):
        return True


class DelayedTailChannel:
    """Reports process exit before a temporarily unavailable output tail."""

    def __init__(self):
        self._ready_calls = 0
        self._stdout = bytearray()
        self._batches = iter((b"first\n", b"tail\n"))

    @property
    def eof_received(self):
        return self._ready_calls >= 6 and not self._stdout

    def recv_ready(self):
        self._ready_calls += 1
        if self._ready_calls in {1, 4}:
            self._stdout.extend(next(self._batches))
        return bool(self._stdout)

    def recv(self, size):
        data = bytes(self._stdout[:size])
        del self._stdout[:size]
        return data

    def recv_stderr_ready(self):
        return False

    def recv_stderr(self, size):
        raise AssertionError("stderr is not ready")

    def exit_status_ready(self):
        return True


class HangingChannel(BufferedChannel):
    def __init__(self):
        super().__init__(b"", eof=False)

    def exit_status_ready(self):
        return False


class ExitStatusBombChannel(BufferedChannel):
    def settimeout(self, _timeout):
        pass

    def exec_command(self, _cmd):
        pass

    def recv_exit_status(self):
        raise AssertionError("truncated commands must not wait for exit status")

    def close(self):
        pass


class StderrFloodChannel:
    """stderr 持续可读:模拟 `yes >&2`。上限只算 stdout 时会无限占内存。"""

    LIMIT = 10_000
    CHUNK = 64

    def __init__(self):
        self.reads = 0
        self.eof_received = False

    def recv_ready(self):
        return False

    def recv(self, size):
        raise AssertionError("stdout is not ready")

    def recv_stderr_ready(self):
        return self.reads < self.LIMIT

    def recv_stderr(self, size):
        self.reads += 1
        return b"e" * min(size, self.CHUNK)

    def exit_status_ready(self):
        return False


class AlwaysReadableChannel:
    """持续可读:每次 recv 只回 1 字节。deadline 若只在外层查,内层会一直读
    到数据耗尽才轮到超时判定。"""

    LIMIT = 200_000

    def __init__(self):
        self.reads = 0
        self.eof_received = False

    def recv_ready(self):
        return self.reads < self.LIMIT

    def recv(self, size):
        self.reads += 1
        return b"x"

    def recv_stderr_ready(self):
        return False

    def recv_stderr(self, size):
        raise AssertionError("stderr is not ready")

    def exit_status_ready(self):
        return False


class OneShotTransport:
    def __init__(self, channel):
        self.channel = channel

    def is_active(self):
        return True

    def open_session(self, timeout=None):
        return self.channel


class ClosedWithoutEofChannel(BufferedChannel):
    def __init__(self, stdout):
        super().__init__(stdout, eof=False)
        self.eof_received = 0
        self.closed = True

    def exit_status_ready(self):
        return True


def test_drain_waits_for_eof_after_exit_status_and_keeps_delayed_tail():
    stdout, stderr, timed_out, truncated = SSHPool._drain(
        DelayedTailChannel(),
        deadline=time.monotonic() + 1,
    )

    assert stdout == "first\ntail\n"
    assert stderr == ""
    assert timed_out is False
    assert truncated is False


def test_drain_without_limit_returns_two_megabytes_unmodified():
    payload = b"x" * (2 * 1024 * 1024)

    stdout, _, _, truncated = SSHPool._drain(
        BufferedChannel(payload),
        deadline=time.monotonic() + 1,
        max_output_bytes=None,
    )

    assert stdout == payload.decode("ascii")
    assert truncated is False


def test_drain_with_limit_marks_truncation_and_appends_notice():
    stdout, _, timed_out, truncated = SSHPool._drain(
        BufferedChannel(b"x" * 2048),
        deadline=time.monotonic() + 1,
        max_output_bytes=1024,
    )

    assert len(stdout) > 1024
    assert "truncated" in stdout.lower()
    assert timed_out is False
    assert truncated is True


def test_drain_limit_counts_stderr_so_a_flood_cannot_exhaust_memory():
    """上限对 stdout+stderr 合计生效:只限 stdout 时 `yes >&2` 可无限占内存。"""
    channel = StderrFloodChannel()

    stdout, stderr, timed_out, truncated = SSHPool._drain(
        channel,
        deadline=time.monotonic() + 5,
        max_output_bytes=4096,
    )

    assert truncated is True
    assert timed_out is False, "应由上限触发返回,而不是拖到超时"
    assert stdout == ""
    assert "truncated" in stderr.lower()
    # 上限不生效时会读满 LIMIT*CHUNK = 640 KiB
    assert len(stderr.encode("utf-8")) < 10_000
    assert channel.reads < channel.LIMIT


def test_drain_checks_deadline_between_reads_while_output_keeps_arriving():
    """deadline 必须在内层循环里查:远端持续输出时 recv_ready() 恒为真,
    外层判定永远轮不到,超时就失效了。"""
    channel = AlwaysReadableChannel()

    _stdout, _stderr, timed_out, truncated = SSHPool._drain(
        channel,
        deadline=time.monotonic() - 1,
        max_output_bytes=None,
    )

    assert timed_out is True
    assert truncated is False
    # deadline 已过期,读一次就该返回;旧实现会读满 LIMIT 次
    assert channel.reads <= 1


def test_drain_timeout_semantics_are_unchanged():
    stdout, stderr, timed_out, truncated = SSHPool._drain(
        HangingChannel(),
        deadline=time.monotonic() - 1,
        max_output_bytes=None,
    )

    assert (stdout, stderr) == ("", "")
    assert timed_out is True
    assert truncated is False


def test_public_interface_defaults_preserve_existing_callers():
    parameter = inspect.signature(SSHPool.run).parameters["max_output_bytes"]

    assert parameter.default is None
    assert CmdResult("n", "c", 0, "", "", 0).truncated is False


def test_run_does_not_wait_for_exit_status_after_output_is_truncated():
    platform = PlatformConfig("alpha", {"node1": NodeInfo(host="198.51.100.101")})

    class Credentials:
        def get(self, _host):
            return Credential("user", "password")

    channel = ExitStatusBombChannel(b"0123456789")
    pool = SSHPool(
        platform,
        Credentials(),
        CmdFilter.from_yaml(default_rules_path()),
    )
    pool._transports["node1"] = OneShotTransport(channel)

    result = pool.run("node1", "echo output", max_output_bytes=4)

    assert result.truncated is True


def test_closed_channel_without_eof_uses_exit_status_safety_net():
    stdout, stderr, timed_out, truncated = SSHPool._drain(
        ClosedWithoutEofChannel(b"done\n"),
        deadline=time.monotonic() + 1,
    )

    assert (stdout, stderr) == ("done\n", "")
    assert timed_out is False
    assert truncated is False
