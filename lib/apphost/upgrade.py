"""Linux 安装形态的离线升级:解出新版 app 目录 → 切 ``app/current`` 符号链接 → 拉起验证健康 → 失败切回。

安装根布局(与 Windows 外壳同构):``bin/opsagent``、``app/<版本>/``、``app/current -> <版本>``、``data/``。
升级前中台须已停止(由 systemd 或 ``opsagent stop`` 停);验证时临时拉起新版,通过后停掉交还进程管理器。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

from lib.apphost.controller import ServerController, ServerState
from lib.apphost.prepare import PACKAGE_INFO

LAUNCHER = Path("packaging") / "linux" / "opsagent"  # 包内即 bin/opsagent 的来源


class UpgradeError(RuntimeError):
    pass


def installed_version(app_dir: Path) -> str:
    try:
        info = json.loads((Path(app_dir) / PACKAGE_INFO).read_text(encoding="utf-8"))
        return str(info["version"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise UpgradeError(f"{app_dir} 缺少有效的 {PACKAGE_INFO}") from exc


def current_version(home: Path) -> str:
    link = Path(home) / "app" / "current"
    if not link.is_symlink():
        raise UpgradeError(f"{link} 不是符号链接,不是 opsagent 安装目录")
    return Path(link.readlink()).name


def switch_current(home: Path, version: str) -> None:
    """先建临时链接再 rename,切换是原子的。"""
    app = Path(home) / "app"
    tmp = app / ".current.tmp"
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(version)
    tmp.replace(app / "current")


def upgrade(
    home: Path,
    tarball: Path,
    data_root: Path,
    *,
    port: int = 8420,
    wait_seconds: float = 180.0,
    controller_factory: Optional[Callable[[Path], ServerController]] = None,
    run: Callable = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    home, data_root = Path(home), Path(data_root)
    state_dir = data_root / "state"
    factory = controller_factory or (lambda app_dir: _controller(app_dir, state_dir, port, run))
    previous = current_version(home)
    if factory(home / "app" / previous).is_healthy():
        raise UpgradeError("中台正在运行;先停止(systemctl --user stop opsagent 或 opsagent stop)再升级")

    version = _unpack(home, Path(tarball))
    switch_current(home, version)
    controller = factory(home / "app" / version)
    controller.start()
    deadline = time.monotonic() + wait_seconds
    healthy = False
    while time.monotonic() < deadline:
        state = controller.refresh()
        if state == ServerState.RUNNING:
            healthy = True
            break
        if state in (ServerState.FAILED, ServerState.STOPPED):  # 进程已退出,不必等满
            break
        sleep(1.0)
    controller.stop()
    if not healthy:
        switch_current(home, previous)
        shutil.rmtree(home / "app" / version, ignore_errors=True)
        raise UpgradeError(
            f"{version} 在 {int(wait_seconds)}s 内未就绪,已切回 {previous};详见 {controller.log_path}")
    launcher = home / "app" / version / LAUNCHER
    if launcher.is_file():
        shutil.copy2(launcher, home / "bin" / "opsagent")
    _cleanup(home, keep={version, previous})
    return {"from": previous, "to": version}


def _controller(app_dir: Path, state_dir: Path, port: int, run: Callable) -> ServerController:
    """新版本必须用新版本自带的运行时与代码做准备和启动,不能用当前进程(旧版)的代码。"""
    python = str(app_dir / "runtime" / "bin" / "python3")

    def prepare() -> Optional[str]:
        state_dir.mkdir(parents=True, exist_ok=True)
        result = run([python, str(app_dir / "scripts" / "opsagent.py"), "prepare"],
                     cwd=str(app_dir), stdin=subprocess.DEVNULL, capture_output=True, text=True)
        return None if result.returncode == 0 else (result.stdout or result.stderr).strip()

    return ServerController(app_dir, state_dir, port=port, python=python, prepare=prepare)


def _unpack(home: Path, tarball: Path) -> str:
    """包内为 ``opsagent/app/<版本>/...``;解到 app/ 同盘临时目录再改名,半截解压不会冒充已安装版本。"""
    app = home / "app"
    with tempfile.TemporaryDirectory(dir=app, prefix=".upgrade-") as tmp:
        try:
            with tarfile.open(tarball) as archive:
                archive.extractall(tmp, filter="data")
        except (OSError, tarfile.TarError) as exc:
            raise UpgradeError(f"解包失败: {tarball}: {exc}") from exc
        candidates = [p for p in (Path(tmp) / "opsagent" / "app").glob("*")
                      if p.is_dir() and not p.is_symlink()]
        if len(candidates) != 1:
            raise UpgradeError(f"{tarball} 不是 opsagent 安装包(应含唯一的 opsagent/app/<版本>/)")
        version = installed_version(candidates[0])
        if candidates[0].name != version:
            raise UpgradeError(f"包内目录名 {candidates[0].name} 与版本 {version} 不一致")
        target = app / version
        if target.exists():
            raise UpgradeError(f"{version} 已安装({target})")
        candidates[0].rename(target)
    return version


def _cleanup(home: Path, *, keep: set) -> None:
    for path in (home / "app").iterdir():
        if path.is_dir() and not path.is_symlink() and not path.name.startswith(".") and path.name not in keep:
            shutil.rmtree(path, ignore_errors=True)
