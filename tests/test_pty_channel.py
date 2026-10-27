import re

import pytest

from lib.pty_channel import PtyChannel, PtyChannelDead


FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures" / "pty"


class FakeTransport:
    def __init__(self, chan):
        self.chan = chan

    def open_session(self, timeout=None):
        return self.chan

    def is_active(self):
        return True


class ScriptedChannel:
    def __init__(self, responses=None, startup=None):
        self.responses = list(responses or [])
        self.sent = []
        self.out = [startup.encode("utf-8")] if startup else []
        self.closed = False
        self.pty = None
        self.shell = False
        self.timeout = None

    def settimeout(self, timeout):
        self.timeout = timeout

    def get_pty(self, term="dumb", width=500, height=24):
        self.pty = (term, width, height)

    def invoke_shell(self):
        self.shell = True

    def send(self, data):
        self.sent.append(data)
        text = data.decode("utf-8") if isinstance(data, bytes) else data
        if "@OPS:SETUP:" in text and not any(b"@OPS:SETUP:" in chunk for chunk in self.out):
            self.out.append(b"@OPS:SETUP:0@\n")
        match = re.search(r"@OPS:([0-9a-f]{12}):", text)
        if match and self.responses:
            raw = self.responses.pop(0).format(token=match.group(1)).encode("utf-8")
            self.out.extend(_split_utf8(raw))
        return len(data)

    def recv_ready(self):
        return bool(self.out)

    def recv(self, size):
        return self.out.pop(0)

    def close(self):
        self.closed = True


class HangingChannel(ScriptedChannel):
    def recv_ready(self):
        return bool(self.out)


class FloodChannel(ScriptedChannel):
    """命令输出无穷无尽且永不吐哨兵行:模拟 `yes` / `tail -f`。"""

    LIMIT = 50_000
    CHUNK = 64

    def __init__(self):
        super().__init__()
        self.floods = 0
        self.interrupted = False

    def send(self, data):
        text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
        if "\x03" in text:
            self.interrupted = True  # 远端收到 Ctrl+C 后停止输出
        if "@OPS:SETUP:" in text:
            self.out.append(b"@OPS:SETUP:0@\n")
        return len(data)

    def recv_ready(self):
        if self.out:
            return True
        return not self.interrupted and self.floods < self.LIMIT

    def recv(self, size):
        if self.out:
            return self.out.pop(0)
        self.floods += 1
        return b"y" * self.CHUNK


class PartialByteSendChannel(ScriptedChannel):
    def __init__(self):
        super().__init__()
        self.received = bytearray()

    def send(self, data):
        assert isinstance(data, bytes)
        chunk = data[:4]
        self.received.extend(chunk)
        return len(chunk)


class ZeroByteFailChannel(ScriptedChannel):
    def __init__(self):
        super().__init__()
        self.fail_send = False

    def send(self, data):
        if self.fail_send:
            raise OSError("Socket is closed")
        return super().send(data)


class PartialThenFailChannel(ScriptedChannel):
    def __init__(self):
        super().__init__()
        self.setup_complete = False
        self.command_started = False

    def send(self, data):
        text = data.decode("utf-8") if isinstance(data, bytes) else data
        if not self.setup_complete and "@OPS:SETUP:" in text:
            self.setup_complete = True
            return super().send(data)
        if self.setup_complete and not self.command_started:
            self.command_started = True
            return max(1, len(data) // 2)
        if self.setup_complete:
            raise OSError("Socket is closed")
        return max(1, len(data) // 2)


def _split_utf8(data):
    marker = "第二".encode("utf-8")
    idx = data.find(marker)
    if idx >= 0:
        return [data[:idx + 1], data[idx + 1:idx + 4], data[idx + 4:]]
    return [data]


def test_fixture_parsing_with_and_without_echo(monkeypatch):
    monkeypatch.setattr("lib.pty_channel.secrets.token_hex", lambda n: "abc123def456")
    no_echo = (FIXTURES / "no_echo.txt").read_text(encoding="utf-8")
    with_echo = (FIXTURES / "with_echo.txt").read_text(encoding="utf-8")
    chan = ScriptedChannel([no_echo, with_echo])

    pty = PtyChannel(FakeTransport(chan))
    assert chan.pty == ("dumb", 500, 24)
    assert chan.shell
    assert pty.exec("echo hello", timeout=1) == (0, "hello\n", False)
    assert pty.exec("echo hello", timeout=1) == (0, "hello\n", False)


def test_startup_banner_is_drained_before_first_exec(monkeypatch):
    monkeypatch.setattr("lib.pty_channel.secrets.token_hex", lambda n: "abc123def456")
    startup = (FIXTURES / "startup_banner.txt").read_text(encoding="utf-8")
    no_echo = (FIXTURES / "no_echo.txt").read_text(encoding="utf-8")
    chan = ScriptedChannel([no_echo], startup=startup)

    pty = PtyChannel(FakeTransport(chan))

    assert pty.exec("echo hello", timeout=1) == (0, "hello\n", False)


def test_setup_disables_shell_history_before_sentinel():
    """交互 shell 退出时会把命令(含 push 的 base64 内容)写进 ~/.bash_history,
    history 必须在 setup 哨兵之前关掉;set 带 command 前缀,dash/zsh 报错才不会中止整行。"""
    chan = ScriptedChannel()
    PtyChannel(FakeTransport(chan))

    setup = b"".join(chan.sent).decode("utf-8")
    sentinel_at = setup.index("@OPS:SETUP:0@")
    assert -1 < setup.find("unset HISTFILE;") < sentinel_at
    assert -1 < setup.find("command set +o history") < sentinel_at


def test_token_randomness_and_exact_matching(monkeypatch):
    tokens = iter(["111111111111", "222222222222"])
    monkeypatch.setattr("lib.pty_channel.secrets.token_hex", lambda n: next(tokens))
    responses = [
        "fake\n@OPS:000000000000:99@\nreal\n@OPS:{token}:3@\n",
        "again\n@OPS:{token}:0@\n",
    ]
    chan = ScriptedChannel(responses)
    pty = PtyChannel(FakeTransport(chan))

    assert pty.exec("cmd1", timeout=1) == (3, "fake\n@OPS:000000000000:99@\nreal\n", False)
    assert pty.exec("cmd2", timeout=1) == (0, "again\n", False)
    assert b"111111111111" in chan.sent[1]
    assert b"222222222222" in chan.sent[2]


def test_ansi_cr_and_utf8_boundary_cleanup(monkeypatch):
    monkeypatch.setattr("lib.pty_channel.secrets.token_hex", lambda n: "abc123def456")
    fixture = (FIXTURES / "ansi_long.txt").read_text(encoding="utf-8")
    pty = PtyChannel(FakeTransport(ScriptedChannel([fixture])))

    code, out, _capped = pty.exec("cmd", timeout=1)

    assert code == 7
    assert "\x1b" not in out
    assert "\r" not in out
    assert "first\n第二行\n" in out
    assert "a" * 100 in out


def test_timeout_returns_partial_and_marks_dead(monkeypatch):
    monkeypatch.setattr("lib.pty_channel.secrets.token_hex", lambda n: "abc123def456")
    chan = HangingChannel()
    pty = PtyChannel(FakeTransport(chan))

    code, out, _capped = pty.exec("sleep 999", timeout=0.01)

    assert code is None
    assert out == ""
    assert pty.dead is True
    assert FakeTransport(chan).is_active()


def test_send_non_ascii_command_once_with_partial_byte_sends():
    chan = PartialByteSendChannel()
    pty = PtyChannel(FakeTransport(chan))
    chan.received.clear()

    command = "grep 中文告警 /tmp/x\n"
    pty._send(command)

    assert bytes(chan.received) == command.encode("utf-8")


def test_remote_close_marks_dead():
    chan = ScriptedChannel()
    pty = PtyChannel(FakeTransport(chan))

    chan.closed = True

    assert pty.dead is True


def test_send_failure_before_any_bytes_marks_dead_and_is_retryable():
    chan = ZeroByteFailChannel()
    pty = PtyChannel(FakeTransport(chan))
    chan.fail_send = True

    with pytest.raises(PtyChannelDead) as exc_info:
        pty._send("date\n")

    assert exc_info.value.retryable is True
    assert pty.dead is True


def test_send_failure_after_partial_bytes_is_not_retryable():
    chan = PartialThenFailChannel()
    pty = PtyChannel(FakeTransport(chan))

    with pytest.raises(PtyChannelDead) as exc_info:
        pty._send("date\n")

    assert exc_info.value.retryable is False
    assert pty.dead is True


def test_eof_received_marks_dead():
    chan = ScriptedChannel()
    pty = PtyChannel(FakeTransport(chan))
    chan.eof_received = 1

    assert pty.dead is True


def test_exec_fails_fast_when_eof_received():
    chan = ScriptedChannel()
    pty = PtyChannel(FakeTransport(chan))
    sent_before = len(chan.sent)
    chan.eof_received = 1

    with pytest.raises(PtyChannelDead) as exc_info:
        pty.exec("date", timeout=0.01)

    assert exc_info.value.retryable is True
    assert len(chan.sent) == sent_before


def test_read_failure_after_send_marks_dead_and_is_not_retryable(monkeypatch):
    chan = ScriptedChannel()
    pty = PtyChannel(FakeTransport(chan))

    def fail_read(*args, **kwargs):
        raise OSError("Socket is closed")

    monkeypatch.setattr(pty, "_read_until", fail_read)

    with pytest.raises(PtyChannelDead) as exc_info:
        pty.exec("date", timeout=1)

    assert exc_info.value.retryable is False
    assert pty.dead is True


def test_exec_stops_at_output_cap_and_retires_the_channel(monkeypatch):
    """撞上限须提前收:PTY 把输出全攒在内存里,不设流式上限时 `yes` 会撑爆。
    哨兵行没读到,通道里的残留会污染下一次 exec,所以必须一并作废通道。"""
    monkeypatch.setattr("lib.pty_channel.secrets.token_hex", lambda n: "abc123def456")
    chan = FloodChannel()
    pty = PtyChannel(FakeTransport(chan))

    code, out, capped = pty.exec("yes", timeout=30, max_output_bytes=4096)

    assert capped is True
    assert code is None, "哨兵行没读到,拿不到退出码"
    # 上限不生效时会读满 LIMIT*CHUNK = 3.2 MiB
    assert len(out.encode("utf-8")) < 20_000
    assert chan.floods < chan.LIMIT
    # 只 close 不中断,远端会继续朝已关闭的通道灌数据并冲垮复用的 transport
    assert chan.interrupted is True, "作废通道前必须先中断远端命令"
    assert pty.dead is True
    assert chan.closed is True


def test_exec_without_cap_keeps_reading_until_sentinel(monkeypatch):
    """不传上限时行为不变:照常读到哨兵行,capped 为 False。"""
    monkeypatch.setattr("lib.pty_channel.secrets.token_hex", lambda n: "abc123def456")
    pty = PtyChannel(FakeTransport(ScriptedChannel(["ok\n@OPS:{token}:0@\n"])))

    assert pty.exec("echo ok", timeout=1) == (0, "ok\n", False)
    assert pty.dead is False
