#!/usr/bin/env python3
"""运维中台只读 Web 服务入口。"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import socket
import sys
import threading
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn

from lib.data_root import resolve_data_root
from lib.file_lock import FileLock
from lib.network_env import configure_direct_network
from lib.apphost.controller import STOP_REQUEST_NAME
from lib.triage_agent.supervisor import LocalProcessSupervisor
from scripts._common import emit_err
from web.server.app import create_app

_PROCESS_STARTED_AT = time.time()


def run(argv=None) -> int:
    args = _parse_args(argv)
    if not math.isfinite(args.chronic_hours) or args.chronic_hours <= 0:
        return emit_err(
            "invalid_chronic_hours",
            "常驻告警阈值必须是大于 0 的有限小时数；请调整 --chronic-hours。",
            chronic_hours=args.chronic_hours,
        )
    repo_root = Path(__file__).resolve().parents[1]
    state_dir = Path(args.state_dir)
    if not state_dir.is_absolute():
        state_dir = resolve_data_root(repo_root) / state_dir
    lock_path = state_dir / "web_server.lock"
    # 持有者信息另存:Windows 的锁是锁文件首字节的字节锁,持锁期间截断重写锁文件
    # 在 macOS/CI 上无法验证,而单实例保证依赖它
    holder_path = state_dir / "web_server.holder.json"
    lock = FileLock(str(lock_path), timeout=0)
    try:
        lock.__enter__()
    except TimeoutError:
        holder = _read_holder(holder_path)
        return emit_err(
            "already_running",
            f"Web 中台已在运行 (PID {holder.get('pid', '未知')}, 端口 {holder.get('port', '未知')})；请直接访问现有实例。",
            pid=holder.get("pid"), port=holder.get("port"), host=holder.get("host"),
        )

    try:
        if not _port_available(args.host, args.port):
            if _is_web_console(args.host, args.port):
                return emit_err(
                    "web_console_port_in_use",
                    f"端口 {args.port} 已由 Web 中台占用；请直接访问现有实例或停止旧实例。",
                    port=args.port, host=args.host,
                )
            return emit_err(
                "port_in_use",
                f"端口 {args.port} 已被其他进程占用；请停止占用进程或更换 --port。",
                port=args.port, host=args.host,
            )

        _write_holder(holder_path, {"pid": os.getpid(), "host": args.host, "port": args.port})
        stop_path = state_dir / STOP_REQUEST_NAME
        _clear_stale_stop_request(stop_path, _PROCESS_STARTED_AT)
        triage_supervisor = _build_triage_supervisor(repo_root, state_dir)
        app = create_app(
            state_dir=state_dir,
            repo_root=repo_root,
            chronic_hours=args.chronic_hours,
            triage_supervisor=triage_supervisor,
        )
        detail = "startup failed"
        try:
            started = _serve(app, args.host, args.port, stop_path)
        except (OSError, SystemExit) as exc:
            started, detail = False, str(exc)
        if not started:
            return emit_err(
                "server_start_failed",
                f"Web 中台无法绑定端口 {args.port}；请检查端口占用后重试。",
                port=args.port, detail=detail,
            )
        return 0
    finally:
        lock.__exit__(None, None, None)


def main() -> int:
    configure_direct_network()
    return run()


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8420)
    parser.add_argument("--state-dir", default="state")
    parser.add_argument(
        "--chronic-hours",
        type=float,
        default=24.0,
        help="常驻告警判定阈值（小时，默认 24）",
    )
    return parser.parse_args(argv)


def _read_holder(path: Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_holder(path: Path, holder: dict) -> None:
    Path(path).write_text(json.dumps(holder, ensure_ascii=False), encoding="utf-8")


def _port_available(host: str, port: int) -> bool:
    try:
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))
        return True
    except OSError:
        return False


def _is_web_console(host: str, port: int) -> bool:
    probe_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    try:
        with urlopen(f"http://{probe_host}:{port}/api/health", timeout=0.25) as response:
            data = json.loads(response.read().decode("utf-8"))
        return response.status == 200 and data.get("status") == "ok"
    except (OSError, URLError, ValueError):
        return False


def _serve(app, host: str, port: int, stop_path: Path) -> bool:
    """跑 uvicorn 直到收到信号或停止请求文件;返回是否曾成功启动。"""
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port))
    logging.getLogger("uvicorn.access").addFilter(_skip_healthy_probe)
    _watch_stop_request(server, stop_path)
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    return server.started


def _skip_healthy_probe(record: logging.LogRecord) -> bool:
    """托盘每 2 秒探活,成功的 /api/health 访问日志一天约 2.6MB,常驻时只留异常探活。"""
    args = record.args
    return not (
        isinstance(args, tuple) and len(args) >= 5
        and args[2] == "/api/health" and args[4] == 200
    )


def _clear_stale_stop_request(path: Path, started_at: float) -> None:
    """早于本进程的停止请求是上次残留,不能让本次启动即退;启动期间写入的保留照办。"""
    try:
        if path.stat().st_mtime < started_at:
            path.unlink()
    except OSError:
        pass


def _watch_stop_request(server, stop_path: Path, interval: float = 1.0) -> threading.Thread:
    # 托盘等外部入口经此文件请求正常退出:Windows 无法向他进程发 SIGTERM,
    # 强杀会跳过 lifespan 收尾、留下孤儿 triage worker,故两平台统一走文件信号
    def loop():
        while not server.should_exit:
            if stop_path.exists():
                try:
                    stop_path.unlink()
                except OSError:
                    pass
                server.should_exit = True
                return
            time.sleep(interval)

    thread = threading.Thread(target=loop, name="stop-request-watch", daemon=True)
    thread.start()
    return thread


def _build_triage_supervisor(repo_root: Path, state_dir: Path):
    repo_root = Path(repo_root)
    return LocalProcessSupervisor(
        repo_root=repo_root,
        state_dir=Path(state_dir),
    )


if __name__ == "__main__":
    sys.exit(main())
