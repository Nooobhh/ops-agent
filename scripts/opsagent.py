#!/usr/bin/env python3
"""opsagent:中台应用形态的统一命令入口(Linux CLI;Windows 外壳也经它做首启准备)。

  serve [web_server 参数]        前台跑中台,给 systemd 等进程管理器用
  start | stop | restart | status 后台托管中台(停止走停止请求文件,超时才强杀)
  prepare                        安装包首启与版本变化时重建知识索引;源码态什么都不做
  kb lint|rebuild [参数]         知识库 lint / 重建派生物
  run <脚本> [参数]              用当前运行时与数据根执行 scripts/<脚本>.py
  version                        打印应用版本
  upgrade <tarball>              Linux 安装形态离线升级(切 app/current,健康失败切回)

除 serve/kb/run 透传子进程输出外,stdout 一行 JSON。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from lib.apphost.controller import HOLDER_NAME, LOG_NAME, ServerController, ServerState
from lib.apphost.prepare import PACKAGE_INFO, prepare_package
from lib.data_root import resolve_data_root
from scripts._common import emit_err, emit_ok

HOME_ENV = "OPSAGENT_HOME"  # 安装形态的 bin/opsagent 设置,指向安装根
DEFAULT_PORT = 8420
SCRIPT_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["serve"]:
        from scripts import web_server

        return web_server.run(argv[1:])
    if argv[:1] in (["kb"], ["run"]):
        return _passthrough(argv)
    args = _parse_args(argv)
    return args.handler(args)


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog="opsagent", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, handler, help_text in (
        ("start", _start, "后台拉起中台并等待就绪"),
        ("stop", _stop, "正常停止中台"),
        ("restart", _restart, "停止后重新拉起"),
        ("status", _status, "中台状态"),
    ):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("--port", type=int, help=f"缺省取运行中实例的端口,否则 {DEFAULT_PORT}")
        cmd.add_argument("--wait", type=float, default=180.0, help="等待就绪秒数")
        cmd.set_defaults(handler=handler)
    sub.add_parser("prepare", help="首启/升级后的知识库准备").set_defaults(handler=_prepare)
    sub.add_parser("version", help="应用版本").set_defaults(handler=_version)
    cmd = sub.add_parser("upgrade", help="离线升级(仅 Linux 安装形态)")
    cmd.add_argument("tarball")
    cmd.add_argument("--port", type=int, default=DEFAULT_PORT)
    cmd.add_argument("--wait", type=float, default=180.0)
    cmd.set_defaults(handler=_upgrade)
    sub.add_parser("serve", help="前台跑中台(参数同 web_server.py)")
    sub.add_parser("kb", help="kb lint|rebuild [参数]")
    sub.add_parser("run", help="run <脚本> [参数]")
    return parser.parse_args(argv)


def _state_dir() -> Path:
    return resolve_data_root(REPO_ROOT) / "state"


def _controller(args) -> ServerController:
    state_dir = _state_dir()
    port = args.port or _holder(state_dir).get("port") or DEFAULT_PORT
    return ServerController(
        REPO_ROOT, state_dir, port=int(port),
        prepare=lambda: prepare_package(REPO_ROOT, sys.executable, state_dir / LOG_NAME),
    )


def _start(args) -> int:
    controller = _controller(args)
    if not controller.start():
        return _report(controller, started=False)
    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline:
        state = controller.refresh()
        if state in (ServerState.RUNNING, ServerState.FAILED, ServerState.STOPPED):
            break
        time.sleep(1.0)
    if controller.refresh() != ServerState.RUNNING:
        return emit_err("start_failed", controller.last_error or f"{int(args.wait)}s 内未就绪",
                        log=str(controller.log_path))
    return _report(controller, started=True)


def _stop(args) -> int:
    controller = _controller(args)
    controller.stop()
    return _report(controller)


def _restart(args) -> int:
    _controller(args).stop()
    return _start(args)


def _status(args) -> int:
    return _report(_controller(args))


def _report(controller: ServerController, **extra) -> int:
    state = controller.refresh()
    pid = controller.server_pid() if state == ServerState.RUNNING else None
    return emit_ok(state=state.value, pid=pid, port=controller.port, url=controller.console_url,
                   log=str(controller.log_path), **extra)


def _prepare(_args) -> int:
    state_dir = _state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    error = prepare_package(REPO_ROOT, sys.executable, state_dir / LOG_NAME)
    if error:
        return emit_err("prepare_failed", error, log=str(state_dir / LOG_NAME))
    return emit_ok()


def _version(_args) -> int:
    try:
        info = json.loads((REPO_ROOT / PACKAGE_INFO).read_text(encoding="utf-8"))
        return emit_ok(version=info.get("version"), commit=info.get("commit"), packaged=True)
    except (OSError, ValueError):
        text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
        return emit_ok(version=match.group(1) if match else None, packaged=False)


def _upgrade(args) -> int:
    from lib.apphost.upgrade import UpgradeError, upgrade

    home = os.environ.get(HOME_ENV, "").strip()
    if not home:
        return emit_err("not_installed", "upgrade 只用于 Linux 安装形态(经 bin/opsagent 运行)")
    try:
        result = upgrade(Path(home), Path(args.tarball), resolve_data_root(REPO_ROOT),
                         port=args.port, wait_seconds=args.wait)
    except UpgradeError as exc:
        return emit_err("upgrade_failed", str(exc))
    return emit_ok(**result)


def _passthrough(argv) -> int:
    if argv[0] == "kb":
        action, rest = (argv[1] if len(argv) > 1 else ""), argv[2:]
        if action == "lint":
            cmd = [sys.executable, "-m", "lib.kb", *rest]
        elif action == "rebuild":
            cmd = [sys.executable, str(REPO_ROOT / "scripts" / "build_wiki.py"), *rest]
        else:
            return emit_err("usage", "用法: opsagent kb lint|rebuild [参数]")
    else:
        name = argv[1] if len(argv) > 1 else ""
        script = REPO_ROOT / "scripts" / f"{name}.py"
        if not SCRIPT_NAME.match(name) or name == "opsagent" or not script.is_file():
            return emit_err("unknown_script", f"scripts/ 下没有 {name!r};用法: opsagent run <脚本> [参数]")
        cmd = [sys.executable, str(script), *argv[2:]]
    return subprocess.run(cmd, cwd=str(REPO_ROOT)).returncode


def _holder(state_dir: Path) -> dict:
    try:
        value = json.loads((state_dir / HOLDER_NAME).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


if __name__ == "__main__":
    sys.exit(main())
