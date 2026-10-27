#!/usr/bin/env python3
"""在 macOS/Linux 上构建发布产物,输出到 build/release/<版本>/。

应用目录(app/<版本>/)= git HEAD 代码(git archive,不含 tests/desktop/.github;envs/ 下只留示例包)
+ 前端构建产物 + 目标平台 Python 与依赖(版本钉 mac 开发 venv)+ package-info.json。
发布包只装程序,所有环境通用:环境数据(环境包)与知识库是应用数据,由各机放进数据目录,不进包。

- windows:OpsAgent-app-<ver>-win-x64.zip(更新用,应用目录内容)、OpsAgent-shell-<ver>.exe(Go 外壳)、
  OpsAgent-<ver>-win-x64.zip(首装:OpsAgent.exe + app\\current + app\\<ver>\\)
- linux:OpsAgent-<ver>-linux-x64.tar.gz(opsagent/bin/opsagent + app/current -> <ver> + app/<ver>/)

本机需:git、npm、uv、go。过程输出走 stderr,stdout 一行 JSON。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from lib.envpack import env_timezone
from scripts._common import emit_err  # noqa: E402

PYTHON_BASE = "https://releases.astral.sh/github/python-build-standalone/releases/download/20260303/"
PYTHON_VERSION = "3.14"
DEV_ONLY_REQUIREMENTS = {"pytest"}
CODE_EXCLUDES = ("tests/", "desktop/", ".github/")
EXAMPLE_PACK = "envs/example/"     # 包内唯一的环境包:演示用,真实环境数据在数据目录
RUNTIME_SKIP = ("share/terminfo",)
PRIVATE_CONFIG = (
    "config/model.local.yaml", "config/credentials.json", "config/auth-profiles.json",
    "config/local-dev.yaml", "config/vector.local.yaml", "config/user.local.yaml",
    "config/paths.local.yaml", "config/plugins.local.yaml", "config/weekly-report.local.yaml",
)
COMMON_REQUIRED = (
    "package-info.json", "scripts/opsagent.py", "scripts/web_server.py", "web/frontend/dist/index.html",
)


@dataclass(frozen=True)
class Target:
    name: str
    python_file: str
    python_sha256: str  # 与上游 SHA256SUMS 核对过;换版本时同步更新
    wheel_platform: str
    python: str  # 应用目录内的解释器
    site_packages: str


TARGETS = {
    "windows": Target(
        "windows", "cpython-3.14.3%2B20260303-x86_64-pc-windows-msvc-install_only_stripped.tar.gz",
        "950c5f21a015c1bdd1337f233456df2470fab71e4d794407d27a84cb8b9909a0",
        "x86_64-pc-windows-msvc", "runtime/python.exe", "runtime/Lib/site-packages",
    ),
    # manylinux2014 = glibc 2.17,覆盖 CentOS 7 与 Ubuntu 18.04+
    "linux": Target(
        "linux", "cpython-3.14.3%2B20260303-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz",
        "0a73413f89efd417871876c9accaab28a9d1e3cd6358fbfff171a38ec99302f0",
        "x86_64-manylinux2014", "runtime/bin/python3", "runtime/lib/python3.14/site-packages",
    ),
}


class BuildError(RuntimeError):
    pass


def main(argv=None) -> int:
    args = _parse_args(argv)
    try:
        summary = build(args)
    except BuildError as exc:
        return emit_err("build_failed", str(exc))
    print(json.dumps({"ok": True, **summary}, ensure_ascii=False))
    return 0


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", choices=("windows", "linux", "all"), default="all")
    parser.add_argument("--version", help="缺省取 pyproject.toml;测试发布给预发布号如 1.0.0-m9.1")
    parser.add_argument("--out-dir", help="缺省 build/release/<版本>")
    parser.add_argument("--cache-dir", default=str(Path.home() / ".cache" / "ops-agent-packaging"))
    parser.add_argument("--skip-frontend-build", action="store_true",
                        help="直接用现有 web/frontend/dist(须与 HEAD 一致)")
    parser.add_argument("--allow-dirty", action="store_true",
                        help="工作区有未提交改动时仍打包(代码取 HEAD,前端与外壳取工作区)")
    return parser.parse_args(argv)


def build(args) -> dict:
    if _git("status", "--porcelain", "--untracked-files=no") and not args.allow_dirty:
        raise BuildError("工作区有未提交改动:包内代码取 HEAD、前端与外壳取工作区,两者会不一致;先提交或加 --allow-dirty")
    version = args.version or _project_version()
    if not re.fullmatch(r"\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?", version):
        raise BuildError(f"版本号 {version!r} 不是 SemVer")
    commit = _git("rev-parse", "--short", "HEAD")
    # 外壳在 desktop/ 下 go build,相对路径须先转绝对
    out_dir = Path(args.out_dir).resolve() if args.out_dir else REPO_ROOT / "build" / "release" / version
    out_dir.mkdir(parents=True, exist_ok=True)
    targets = list(TARGETS) if args.target == "all" else [args.target]
    artifacts = []
    with tempfile.TemporaryDirectory() as tmp:
        common = Path(tmp) / "common"
        _export_code(common)
        _build_frontend(common, skip=args.skip_frontend_build)
        info = {
            "version": version, "commit": commit,
            "built_at": datetime.now(env_timezone()).isoformat(timespec="seconds"),
        }
        for name in targets:
            target = TARGETS[name]
            app = Path(tmp) / name / "app"
            shutil.copytree(common, app, symlinks=True)
            _install_runtime(app, target, Path(args.cache_dir))
            _install_dependencies(app, target, Path(tmp))
            (app / "package-info.json").write_text(json.dumps(
                {**info, "python": target.python_file.replace("%2B", "+")}, ensure_ascii=False, indent=2,
            ), encoding="utf-8")
            _verify(app, target)
            if name == "windows":
                artifacts += _package_windows(app, version, out_dir, Path(tmp))
            else:
                artifacts += _package_linux(app, version, out_dir)
    return {"version": version, "commit": commit,
            "out_dir": str(out_dir), "artifacts": artifacts}


def _export_code(stage: Path) -> None:
    _log("导出代码(git archive HEAD)")
    archive = subprocess.run(
        ["git", "archive", "--format=tar", "HEAD"], cwd=REPO_ROOT, check=True, capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        members = [m for m in tar.getmembers() if _packaged(m.name)]
        tar.extractall(stage, members=members, filter="data")


def _packaged(name: str) -> bool:
    """git archive 条目是否进包:去掉测试/外壳/CI;envs/ 下只留示例包(拆仓前私有环境包还在核心仓里)。"""
    if name.startswith(CODE_EXCLUDES):
        return False
    return not name.startswith("envs/") or name == "envs/" or name.startswith(EXAMPLE_PACK)


def _build_frontend(stage: Path, *, skip: bool) -> None:
    frontend = REPO_ROOT / "web" / "frontend"
    if not skip:
        _log("构建前端(npm ci / build / test)")
        for cmd in (["npm", "ci"], ["npm", "run", "build"], ["npm", "test"]):
            _run(cmd, cwd=frontend)
    dist = frontend / "dist"
    if not (dist / "index.html").exists():
        raise BuildError("web/frontend/dist 不存在,去掉 --skip-frontend-build 重新构建")
    shutil.copytree(dist, stage / "web" / "frontend" / "dist")


def _install_runtime(app: Path, target: Target, cache_dir: Path) -> None:
    tarball = _fetch(PYTHON_BASE + target.python_file, target.python_sha256, cache_dir)
    _log(f"解出 {target.name} 版 Python")
    with tarfile.open(tarball) as tar:
        members = []
        for member in tar.getmembers():
            if not member.name.startswith("python/"):
                continue
            member.name = member.name[len("python/"):]
            # terminfo 里有只差大小写的条目互为链接,在 mac 的大小写不敏感盘上解不出来;中台用不到
            if member.name and not member.name.startswith(RUNTIME_SKIP):
                members.append(member)
        tar.extractall(app / "runtime", members=members, filter="data")


def _install_dependencies(app: Path, target: Target, tmp: Path) -> None:
    _log(f"安装 {target.name} 依赖(uv 跨平台 wheel)")
    requirements = [
        line for line in (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
        and re.split(r"[<>=!~\[ ;]", line.strip(), maxsplit=1)[0].lower() not in DEV_ONLY_REQUIREMENTS
    ]
    req_file = tmp / "requirements-app.txt"
    req_file.write_text("\n".join(requirements) + "\n", encoding="utf-8")
    # 钉到 Mac 开发 venv 实测过的版本,否则包会解析到未经测试的新版本
    constraints = tmp / "constraints-dev-venv.txt"
    constraints.write_text(subprocess.run(
        ["uv", "pip", "freeze", "--python", sys.executable],
        check=True, capture_output=True, text=True,
    ).stdout, encoding="utf-8")
    _run([
        "uv", "pip", "install", "--quiet",
        "--target", str(app / target.site_packages),
        "--python-platform", target.wheel_platform, "--python-version", PYTHON_VERSION,
        "--only-binary", ":all:", "-r", str(req_file), "-c", str(constraints),
    ], cwd=REPO_ROOT)


def _verify(app: Path, target: Target) -> None:
    required = [*COMMON_REQUIRED, target.python, EXAMPLE_PACK + "env.yaml",
                *(f"{target.site_packages}/{pkg}" for pkg in ("fastapi", "uvicorn", "paramiko", "yaml", "tzdata"))]
    missing = [p for p in required if not (app / p).exists()]
    if missing:
        raise BuildError(f"{target.name} 应用目录缺少: {', '.join(missing)}")
    leaked = [p for p in PRIVATE_CONFIG if (app / p).exists()]
    for name in ("knowledge", "records", "models", "env", "seed"):
        if (app / name).exists():
            leaked.append(f"{name}/")
    leaked += [f"envs/{p.name}/" for p in (app / "envs").iterdir() if p.name != "example"]
    leaked += [str(p.relative_to(app)) for p in (app / "state").glob("*") if p.name != ".gitkeep"]
    leaked += [str(p.relative_to(app)) for p in (app / "build").glob("*") if p.name != ".gitkeep"]
    if leaked:
        raise BuildError(f"包内不应出现本地数据或私有配置: {', '.join(leaked)}")


def _package_windows(app: Path, version: str, out_dir: Path, tmp: Path) -> list:
    shell = out_dir / f"OpsAgent-shell-{version}.exe"
    _build_shell(shell)
    app_zip = out_dir / f"OpsAgent-app-{version}-win-x64.zip"
    _write_zip(app_zip, ((p, p.relative_to(app).as_posix()) for p in _files(app)))
    current = tmp / "current"
    current.write_text(version + "\n", encoding="utf-8")
    install = out_dir / f"OpsAgent-{version}-win-x64.zip"
    _write_zip(install, [
        (shell, "OpsAgent/OpsAgent.exe"),
        (REPO_ROOT / "packaging" / "windows" / "README-Windows.txt", "OpsAgent/README-Windows.txt"),
        (current, "OpsAgent/app/current"),
        *((p, f"OpsAgent/app/{version}/{p.relative_to(app).as_posix()}") for p in _files(app)),
    ])
    return [_artifact(shell, "shell"), _artifact(app_zip, "app"), _artifact(install, "install")]


def _package_linux(app: Path, version: str, out_dir: Path) -> list:
    path = out_dir / f"OpsAgent-{version}-linux-x64.tar.gz"
    _log(f"写出 {path.name}")
    tmp_path = path.with_suffix(".gz.tmp")
    now = int(time.time())

    def owner(info: tarfile.TarInfo) -> tarfile.TarInfo:  # 包内不带打包机的用户名
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        return info

    def entry(name, kind, *, mode=0o755, linkname=""):
        info = tarfile.TarInfo(name)
        info.type, info.mode, info.mtime, info.linkname = kind, mode, now, linkname
        return info

    with tarfile.open(tmp_path, "w:gz", compresslevel=6) as tar:
        for name in ("opsagent", "opsagent/bin", "opsagent/app", "opsagent/data"):
            tar.addfile(entry(name, tarfile.DIRTYPE))
        tar.add(REPO_ROOT / "packaging" / "linux" / "opsagent", "opsagent/bin/opsagent", filter=owner)
        tar.add(REPO_ROOT / "packaging" / "linux" / "README-Linux.txt", "opsagent/README-Linux.txt", filter=owner)
        tar.addfile(entry("opsagent/app/current", tarfile.SYMTYPE, mode=0o777, linkname=version))
        tar.add(app, f"opsagent/app/{version}", filter=owner)
    tmp_path.replace(path)
    return [_artifact(path, "linux")]


def _build_shell(dest: Path) -> None:
    """-trimpath/-buildvcs=false 且不注入版本号:desktop/ 不变时 exe 字节不变,更新时不换外壳。"""
    _log("交叉编译 Go 外壳")
    tree = _git("rev-parse", "--short=10", "HEAD:desktop")
    if _git("status", "--porcelain", "--", "desktop"):
        tree += "-dirty"
    env = dict(os.environ, GOOS="windows", GOARCH="amd64", CGO_ENABLED="0")
    _run(["go", "build", "-trimpath", "-buildvcs=false",
          "-ldflags", f"-H=windowsgui -s -w -X main.shellBuild={tree}", "-o", str(dest), "."],
         cwd=REPO_ROOT / "desktop", env=env)


def _files(root: Path):
    return sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink())


def _write_zip(zip_path: Path, entries) -> None:
    _log(f"写出 {zip_path.name}")
    tmp_path = zip_path.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path, name in entries:
            archive.write(path, name)
    tmp_path.replace(zip_path)


def _artifact(path: Path, kind: str) -> dict:
    return {"name": path.name, "kind": kind, "path": str(path), "size": path.stat().st_size,
            "sha256": _sha256(path)}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fetch(url: str, sha256: str, cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / url.rsplit("/", 1)[-1].replace("%2B", "+")
    if not target.exists():
        _log(f"下载 {url}")
        partial = target.with_suffix(target.suffix + ".part")
        # 镜像拒绝 urllib 默认 UA(403)
        request = Request(url, headers={"User-Agent": "ops-agent-packaging/1.0"})
        try:
            with urlopen(request, timeout=60) as response, open(partial, "wb") as handle:
                shutil.copyfileobj(response, handle)
        except (OSError, URLError) as exc:
            partial.unlink(missing_ok=True)
            raise BuildError(f"下载失败: {url}: {exc}") from exc
        partial.replace(target)
    digest = _sha256(target)
    if digest != sha256:
        target.unlink()
        raise BuildError(f"{target.name} 校验失败: {digest}")
    return target


def _project_version() -> str:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise BuildError("pyproject.toml 缺少 version")
    return match.group(1)


def _git(*args, cwd: Path = REPO_ROOT) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def _run(cmd, *, cwd: Path, env=None) -> None:
    try:
        subprocess.run(cmd, cwd=cwd, env=env, check=True, stdout=sys.stderr, stderr=sys.stderr)
    except FileNotFoundError as exc:
        raise BuildError(f"找不到命令 {cmd[0]}(见脚本头部的本机依赖)") from exc
    except subprocess.CalledProcessError as exc:
        raise BuildError(f"命令失败({exc.returncode}): {' '.join(map(str, cmd))}") from exc


def _log(message: str) -> None:
    print(f"[build] {message}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    sys.exit(main())
