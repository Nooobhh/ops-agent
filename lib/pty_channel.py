"""Interactive PTY command channel.

PTY sessions merge stdout and stderr by design; exec() returns the merged stream.
Timeouts mark the shell dead so callers can discard and recreate the channel.
"""
from __future__ import annotations

import codecs
import re
import secrets
import time
from typing import Optional

ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class PtyChannelDead(OSError):
    """PTY channel 已断; retryable 表示远端是否可能已收到半条命令。"""

    def __init__(self, message: str, *, retryable: bool):
        self.retryable = retryable
        super().__init__(message)


class PtyChannel:
    def __init__(self, transport, term: str = "dumb", width: int = 500):
        self._transport = transport
        self._chan = transport.open_session(timeout=10)
        self._chan.settimeout(1)
        self._chan.get_pty(term=term, width=width, height=24)
        self._chan.invoke_shell()
        self._dead = False
        # 交互 shell 退出(含断链 SIGHUP)时会把本会话命令写进 HISTFILE,push 的
        # base64 内容也在其中,故须在哨兵前关 history;bash 关 history 也一并停掉
        # `!` 展开(否则 `x!d` 会被换成历史命令)。set 的 command 前缀不能省:
        # dash/zsh 不认 history 选项,裸 set 报错会中止整行,setup 哨兵发不出来。
        self._send(
            "stty -echo -onlcr 2>/dev/null; export PS1=''; "
            "unset HISTFILE; command set +o history 2>/dev/null; "
            "printf '\\n@OPS:SETUP:0@\\n'\n"
        )
        self._drain_setup(timeout=2.0)

    @property
    def dead(self) -> bool:
        return bool(
            self._dead
            or getattr(self._chan, "closed", False)
            or getattr(self._chan, "eof_received", False)
        )

    def exec(
        self,
        cmd: str,
        timeout: float,
        max_output_bytes: Optional[int] = None,
    ) -> tuple[Optional[int], str, bool]:
        """返回 (exit_code, output, capped)。capped=True 表示撞输出上限提前收，
        此时 exit_code 必为 None——哨兵行没读到,命令是否跑完无从判断。"""
        if self.dead:
            raise PtyChannelDead("PTY channel is dead", retryable=True)
        token = secrets.token_hex(6)
        sentinel_re = re.compile(rf"^@OPS:{re.escape(token)}:(-?\d+)@$")
        deadline = time.monotonic() + timeout
        self._send(f"{cmd}; printf '\\n@OPS:{token}:%s@\\n' $?\n")
        try:
            code, out, timed_out, capped = self._read_until(
                sentinel_re, token, deadline,
                max_output_bytes=max_output_bytes,
            )
        except OSError as exc:
            self._dead = True
            raise PtyChannelDead(str(exc), retryable=False) from exc
        if timed_out or capped:
            # 两种情况哨兵行都没读到:通道里残留的输出会污染下一次 exec,
            # 故一律作废通道,不再复用。作废前必须先止住远端——只 close 的话
            # 命令还在跑,输出会继续灌向已关闭的通道并冲垮复用的 transport,
            # 一次超限就会连累之后所有命令。
            self._interrupt_remote()
            self._dead = True
            self._chan.close()
            return None, out, capped
        return code, out, False

    def _interrupt_remote(self, grace: float = 0.5) -> None:
        """尽力而为地中断远端命令:PTY 下 Ctrl+C 能真正终止前台进程,
        随后把在途输出丢弃掉,让通道安静下来再关。失败不抛——此时通道
        本就要作废,清理不掉也只是回到「只 close」的旧行为。"""
        try:
            self._chan.send(b"\x03")
        except Exception:  # noqa: BLE001
            return
        deadline = time.monotonic() + grace
        try:
            while time.monotonic() < deadline:
                if self._chan.recv_ready():
                    self._chan.recv(65536)  # 丢弃:只为排空在途数据
                else:
                    time.sleep(0.02)
        except Exception:  # noqa: BLE001
            pass

    def _drain_setup(self, timeout: float) -> None:
        sentinel_re = re.compile(r"^@OPS:SETUP:0@$")
        _, _, timed_out, _ = self._read_until(sentinel_re, "SETUP", time.monotonic() + timeout, keep_output=False)
        if timed_out:
            self._dead = True
            self._chan.close()

    def _read_until(self, sentinel_re, token: str, deadline: float,
                    keep_output: bool = True,
                    max_output_bytes: Optional[int] = None,
                    ) -> tuple[Optional[int], str, bool, bool]:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        pending = ""
        output: list[str] = []
        received = 0

        def _flush_tail() -> str:
            nonlocal pending
            pending += self._normalize(decoder.decode(b"", final=True))
            if keep_output and pending:
                output.append(pending)
            return self._clean_output("".join(output), token)

        while True:
            # deadline 与字节上限都必须在内层查:PTY 持续输出时 recv_ready() 恒真,
            # 只在外层判会读到数据耗尽为止,超时与上限双双失效
            while self._chan.recv_ready():
                raw = self._chan.recv(65536)
                received += len(raw)
                pending += self._normalize(decoder.decode(raw, final=False))
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    match = sentinel_re.match(line)
                    if match:
                        code = int(match.group(1)) if match.groups() else 0
                        return code, self._clean_output("".join(output), token), False, False
                    if keep_output:
                        output.append(line + "\n")
                if max_output_bytes is not None and received >= max_output_bytes:
                    return None, _flush_tail(), False, True
                if time.monotonic() > deadline:
                    return None, _flush_tail(), True, False
            if time.monotonic() > deadline:
                return None, _flush_tail(), True, False
            time.sleep(0.02)

    def _send(self, data: str) -> None:
        sent = 0
        raw = data.encode("utf-8")
        while sent < len(raw):
            try:
                n = self._chan.send(raw[sent:])
            except OSError as exc:
                self._dead = True
                raise PtyChannelDead(
                    str(exc), retryable=sent == 0
                ) from exc
            if n is None:
                n = len(raw) - sent
            if n <= 0:
                self._dead = True
                raise PtyChannelDead(
                    "PTY channel send made no progress", retryable=sent == 0
                )
            sent += n

    def _parse(self, text: str, token: str, sentinel_re) -> Optional[tuple[int, str]]:
        normalized = self._normalize(text)
        lines = normalized.splitlines(keepends=True)
        found = None
        pos = 0
        for line in lines:
            body = line.rstrip("\n")
            match = sentinel_re.match(body)
            next_pos = pos + len(line)
            if match:
                found = (next_pos - len(line), int(match.group(1)))
            pos = next_pos
        if found is None:
            return None
        sentinel_start, exit_code = found
        return exit_code, self._clean_output(normalized[:sentinel_start], token)

    @staticmethod
    def _normalize(text: str) -> str:
        return ANSI_CSI_RE.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))

    def _clean_output(self, text: str, token: str) -> str:
        normalized = self._normalize(text)
        cleaned = []
        for line in normalized.splitlines(keepends=True):
            if f"@OPS:{token}:" in line:
                continue
            cleaned.append(line)
        return "".join(cleaned)
