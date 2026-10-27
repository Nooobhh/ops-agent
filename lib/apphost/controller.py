"""中台进程控制:起停、状态探测与日志(Linux CLI `opsagent start/stop/status` 与离线升级用)。

停止走 web_server 的停止请求文件(正常退出、lifespan 带走 worker),超时才强杀
整个进程树;强杀单个进程会在 Windows 上留下孤儿 triage worker。
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from enum import Enum
from pathlib import Path
from typing import Callable, Optional
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

STOP_REQUEST_NAME = "web_server.stop"  # scripts/web_server.py 监视此文件
HOLDER_NAME = "web_server.holder.json"
LOG_NAME = "web_server.log"
LOG_ROTATE_BYTES = 5 * 1024 * 1024
CREATE_NO_WINDOW = 0x08000000
LOGGER = logging.getLogger(__name__)


class ServerState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    UNRESPONSIVE = "unresponsive"
    STOPPING = "stopping"
    FAILED = "failed"


def server_python(executable: str = sys.executable) -> str:
    """托盘在 Windows 上跑 pythonw;中台须用 python.exe + CREATE_NO_WINDOW,
    子进程 worker 才会继承隐藏控制台而不是各自弹出黑窗。"""
    path = Path(executable)
    if os.name == "nt" and path.name.lower() == "pythonw.exe":
        candidate = path.with_name("python.exe")
        if candidate.exists():
            return str(candidate)
    return executable


def probe_health(port: int, timeout: float = 2.0) -> bool:
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        return response.status == 200 and data.get("status") == "ok"
    except (OSError, URLError, ValueError):
        return False


class ServerController:
    def __init__(
        self,
        repo_root: Path,
        state_dir: Path,
        *,
        port: int = 8420,
        python: Optional[str] = None,
        server_args: Optional[list] = None,
        probe: Optional[Callable[[], bool]] = None,
        popen: Callable = subprocess.Popen,
        prepare: Optional[Callable[[], Optional[str]]] = None,
        stop_timeout: float = 45.0,
    ):
        self.repo_root = Path(repo_root)
        self.state_dir = Path(state_dir)
        self.port = port
        self.python = python or server_python()
        self.server_args = server_args
        self._probe = probe or (lambda: probe_health(port))
        self._popen = popen
        self._prepare = prepare
        self.stop_timeout = stop_timeout
        self._preparing = False
        self._process: Optional[subprocess.Popen] = None
        self._ever_healthy = False
        self._stopping = False
        self.last_error = ""

    @property
    def console_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    @property
    def log_path(self) -> Path:
        return self.state_dir / LOG_NAME

    def is_healthy(self) -> bool:
        return self._probe()

    def refresh(self) -> ServerState:
        if self._stopping:
            return ServerState.STOPPING
        if self._preparing:
            return ServerState.STARTING
        if self._probe():
            self._ever_healthy = True
            self.last_error = ""
            return ServerState.RUNNING
        process = self._process
        if process is not None and process.poll() is None:
            return ServerState.UNRESPONSIVE if self._ever_healthy else ServerState.STARTING
        if process is not None:
            self._process = None
            if process.returncode != 0:
                self.last_error = (
                    f"中台异常退出(code {process.returncode}),详见日志"
                    if self._ever_healthy else self._failure_message(process.returncode)
                )
        self._ever_healthy = False
        return ServerState.FAILED if self.last_error else ServerState.STOPPED

    def server_pid(self) -> Optional[int]:
        """自己拉起的进程优先;否则取 holder(外部起的实例)。holder 在中台退出后
        会残留,调用方只在探活成功时信它——stop() 超时强杀前同样以探活为准。"""
        process = self._process
        if process is not None and process.poll() is None:
            return process.pid
        pid = _read_holder(self.state_dir / HOLDER_NAME).get("pid")
        return pid if isinstance(pid, int) else None

    def start(self) -> bool:
        """已在运行(含外部起的实例)则不重复拉起。"""
        if self._probe():
            return False
        process = self._process
        if process is not None and process.poll() is None:
            return False
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._rotate_log()
        self.last_error = ""
        self._ever_healthy = False
        if self._prepare is not None:
            # 准备失败(如索引重建)不拦中台启动,知识页会显式报索引缺失/过期
            self._preparing = True
            try:
                warning = self._prepare()
            finally:
                self._preparing = False
            if warning:
                LOGGER.warning(warning)
        argv = self.server_args or [
            self.python,
            str(self.repo_root / "scripts" / "web_server.py"),
            "--port", str(self.port),
            "--state-dir", str(self.state_dir),
        ]
        env = dict(os.environ)
        # 重定向到日志文件时 Windows 默认按 GBK 写;中台按 macOS 默认 UTF-8 开发
        env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
        kwargs = {"cwd": str(self.repo_root), "env": env, "stdin": subprocess.DEVNULL}
        if os.name == "nt":
            kwargs["creationflags"] = CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True  # 自成进程组,强杀时连 worker 一起带走
        with open(self.log_path, "ab") as log:
            kwargs.update(stdout=log, stderr=subprocess.STDOUT)
            self._process = self._popen(argv, **kwargs)
        return True

    def stop(self, *, poll_interval: float = 0.5) -> None:
        pid = self.server_pid()
        if pid is None:
            return
        self._stopping = True
        try:
            (self.state_dir / STOP_REQUEST_NAME).write_text("", encoding="utf-8")
            deadline = time.monotonic() + self.stop_timeout
            while time.monotonic() < deadline:
                if not self._alive(pid):
                    break
                time.sleep(poll_interval)
            else:
                kill_tree(pid, own_group=self._owns(pid))
                if self._process is not None:
                    self._process.wait(timeout=10)
        finally:
            (self.state_dir / STOP_REQUEST_NAME).unlink(missing_ok=True)
            if self._process is not None and self._process.poll() is not None:
                self._process = None
            self._ever_healthy = False
            self._stopping = False

    def _alive(self, pid: int) -> bool:
        if self._owns(pid):
            return self._process.poll() is None
        return self._probe()

    def _owns(self, pid: int) -> bool:
        return self._process is not None and self._process.pid == pid

    def _rotate_log(self) -> None:
        try:
            if self.log_path.stat().st_size > LOG_ROTATE_BYTES:
                os.replace(self.log_path, self.log_path.with_name(LOG_NAME + ".1"))
        except OSError:
            pass

    def _failure_message(self, returncode: int) -> str:
        """中台启动失败经 emit_err 打一行 JSON,取日志尾部最后一条的 message。"""
        try:
            with open(self.log_path, "rb") as log:
                log.seek(0, os.SEEK_END)
                log.seek(max(0, log.tell() - 8192))
                tail = log.read().decode("utf-8", errors="replace")
        except OSError:
            tail = ""
        for line in reversed(tail.splitlines()):
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if isinstance(payload, dict) and payload.get("message"):
                return str(payload["message"])
        return f"中台进程退出(code {returncode}),详见日志"


def kill_tree(pid: int, *, own_group: bool) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            creationflags=CREATE_NO_WINDOW,
        )
        return
    try:
        # 只杀自己建的进程组;外部(终端)起的实例的组可能是整条 shell 作业
        if own_group and os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _read_holder(path: Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}
