import os
import socket
import sys
import time

from lib.apphost import controller as controller_module
from lib.apphost.controller import ServerController, ServerState, server_python

# 与中台同契约的替身:/api/health、holder 文件、停止请求文件、带一个 worker 子进程
FAKE_SERVER = r'''
import http.server, json, os, subprocess, sys, threading, time
port, state_dir, mode = int(sys.argv[1]), sys.argv[2], sys.argv[3]
if mode == "fail":
    print(json.dumps({"ok": False, "error": "port_in_use",
                      "message": "端口 1 已被其他进程占用"}, ensure_ascii=False))
    sys.exit(1)
worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
with open(os.path.join(state_dir, "worker.pid"), "w") as f:
    f.write(str(worker.pid))
with open(os.path.join(state_dir, "web_server.holder.json"), "w") as f:
    json.dump({"pid": os.getpid(), "port": port}, f)

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"status": "ok"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass

server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
stop = os.path.join(state_dir, "web_server.stop")
while True:
    if mode == "graceful" and os.path.exists(stop):
        # 与 web_server 的停止监视一致:删不掉(Windows 上杀软正扫这个新文件时会共享冲突)也照常退出,
        # 否则假中台带着异常退出、worker 成孤儿,控制器那边仍判 STOPPED
        try:
            os.remove(stop)
        except OSError:
            pass
        worker.terminate()
        worker.wait()
        server.shutdown()
        sys.exit(0)
    time.sleep(0.05)
'''


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _controller(tmp_path, mode, **kwargs) -> ServerController:
    script = tmp_path / "fake_server.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    port = _free_port()
    return ServerController(
        tmp_path, state_dir, port=port,
        server_args=[sys.executable, str(script), str(port), str(state_dir), mode],
        **kwargs,
    )


def _wait_state(controller, expected, timeout=10.0) -> ServerState:
    deadline = time.monotonic() + timeout
    state = controller.refresh()
    while state is not expected and time.monotonic() < deadline:
        time.sleep(0.05)
        state = controller.refresh()
    return state


class _ProcessWatch:
    """在进程确定还活着时盯住它,之后判断「这一个进程」是否已退出。

    不能按 PID 反复探活:Windows 回收 PID 很快,worker 退出后同号几秒内就可能分给别的进程,
    按号探活会把新进程当成 worker 还活着(Windows CI 上偶发失败即此因)。Windows 上先开句柄
    再等它被置位,句柄指向的就是原进程;Linux/macOS 的 PID 顺序分配,短时间内不会回绕。
    """

    def __init__(self, pid: int):
        self.pid = pid
        self.handle = None
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            self._kernel32 = kernel32
            # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
            self.handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, pid)
            assert self.handle, f"打不开 worker 进程 {pid}(错误 {ctypes.get_last_error()})"

    def gone(self, timeout: float = 5.0) -> bool:
        if self.handle is not None:
            try:
                return self._kernel32.WaitForSingleObject(self.handle, int(timeout * 1000)) == 0  # WAIT_OBJECT_0
            finally:
                self._kernel32.CloseHandle(self.handle)
                self.handle = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(self.pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.05)
        return False


def _watch_worker(controller) -> _ProcessWatch:
    return _ProcessWatch(int((controller.state_dir / "worker.pid").read_text()))


def _server_log(controller) -> str:
    """断言失败时带上假中台输出(它写在 state 下的日志里,pytest 抓不到)。"""
    try:
        return controller.log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
    except OSError as exc:
        return f"<读不到 {controller.log_path}: {exc}>"


def test_start_then_graceful_stop_takes_worker_down(tmp_path):
    controller = _controller(tmp_path, "graceful")

    assert controller.start() is True
    assert _wait_state(controller, ServerState.RUNNING) is ServerState.RUNNING
    worker = _watch_worker(controller)
    assert controller.start() is False  # 已在运行不重复拉起

    controller.stop()

    assert controller.refresh() is ServerState.STOPPED
    assert worker.gone(), f"原 worker 进程 {worker.pid} 停止后仍在运行;假中台输出:\n{_server_log(controller)}"
    assert not (controller.state_dir / "web_server.stop").exists()


def test_stop_timeout_kills_whole_process_tree(tmp_path):
    controller = _controller(tmp_path, "stubborn", stop_timeout=0.5)
    controller.start()
    assert _wait_state(controller, ServerState.RUNNING) is ServerState.RUNNING
    worker = _watch_worker(controller)

    controller.stop(poll_interval=0.05)

    assert controller.refresh() is ServerState.STOPPED
    assert worker.gone(), f"原 worker 进程 {worker.pid} 强杀后仍在运行;假中台输出:\n{_server_log(controller)}"


def test_startup_failure_surfaces_emit_err_message(tmp_path):
    controller = _controller(tmp_path, "fail")
    controller.start()

    assert _wait_state(controller, ServerState.FAILED) is ServerState.FAILED
    assert controller.last_error == "端口 1 已被其他进程占用"
    assert "port_in_use" in controller.log_path.read_text(encoding="utf-8")


def test_external_instance_is_not_spawned_again(tmp_path):
    def refuse(*_args, **_kwargs):
        raise AssertionError("不应拉起新进程")

    controller = ServerController(tmp_path, tmp_path / "state", probe=lambda: True, popen=refuse)

    assert controller.start() is False
    assert controller.refresh() is ServerState.RUNNING


class _FakeProcess:
    def __init__(self, returncode=None):
        self.pid = 4242
        self.returncode = returncode

    def poll(self):
        return self.returncode


def test_alive_but_unhealthy_reads_starting_then_unresponsive(tmp_path):
    healthy = {"value": False}
    process = _FakeProcess()
    controller = ServerController(
        tmp_path, tmp_path / "state", probe=lambda: healthy["value"],
        popen=lambda *_args, **_kwargs: process,
    )
    controller.start()
    assert controller.refresh() is ServerState.STARTING

    healthy["value"] = True
    assert controller.refresh() is ServerState.RUNNING

    healthy["value"] = False
    assert controller.refresh() is ServerState.UNRESPONSIVE

    process.returncode = 1
    assert controller.refresh() is ServerState.FAILED
    assert "异常退出" in controller.last_error


def test_log_rotates_when_oversized(tmp_path, monkeypatch):
    monkeypatch.setattr(controller_module, "LOG_ROTATE_BYTES", 10)
    controller = ServerController(
        tmp_path, tmp_path / "state", probe=lambda: False,
        popen=lambda *_args, **_kwargs: _FakeProcess(),
    )
    controller.state_dir.mkdir()
    controller.log_path.write_text("x" * 20, encoding="utf-8")

    controller.start()

    assert (controller.state_dir / "web_server.log.1").read_text(encoding="utf-8") == "x" * 20


def test_server_python_keeps_console_interpreter():
    assert server_python("/opt/py/bin/python3") == "/opt/py/bin/python3"
