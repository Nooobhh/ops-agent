#!/usr/bin/env python3
"""发版:构建全部产物 → 写 manifest.json → 本机私钥签名 → gh release create 到发布仓。

正式版:版本取 pyproject;要求工作区干净、HEAD 有 v<版本> tag、CHANGELOG 有该版本块(即更新说明)。
发布包只装程序(示例环境包随代码),所有环境通用;环境数据与知识库由各机放进数据目录。
测试版:--version 给预发布号(如 1.0.0-m9.2)并 --notes 写说明,跳过 tag/CHANGELOG 检查。
--dry-run 只构建与签名不发布。私钥缺省 ~/.ops-agent/release-signing.key(不进仓、不进 CI)。
发布仓是只放产物的独立仓,须已有一个提交(建仓时 --add-readme),release 标签打在它上面。
过程输出走 stderr,stdout 一行 JSON。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from lib.envpack import env_timezone
from scripts._common import emit_err

DESKTOP = REPO_ROOT / "desktop"
MANIFEST = "manifest.json"
DEFAULT_KEY = Path.home() / ".ops-agent" / "release-signing.key"


class ReleaseError(RuntimeError):
    pass


def main(argv=None) -> int:
    args = _parse_args(argv)
    try:
        summary = release(args)
    except ReleaseError as exc:
        return emit_err("release_failed", str(exc))
    print(json.dumps({"ok": True, **summary}, ensure_ascii=False))
    return 0


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", help="发布仓 owner/name(--dry-run 可省)")
    parser.add_argument("--version", help="测试版预发布号;缺省取 pyproject 并按正式版检查")
    parser.add_argument("--notes", help="测试版更新说明")
    parser.add_argument("--key", default=str(DEFAULT_KEY), help="签名私钥")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-build", action="store_true", help="复用 build/release/<版本>/ 下已构建的产物")
    parser.add_argument("--allow-dirty", action="store_true", help="仅测试版:工作区有改动也构建")
    return parser.parse_args(argv)


def release(args) -> dict:
    if not args.dry_run and not args.repo:
        raise ReleaseError("缺 --repo")
    if not Path(args.key).is_file():
        raise ReleaseError(f"找不到签名私钥 {args.key};发版只在持有私钥的机器上进行")
    stable = args.version is None
    version = _project_version() if stable else args.version
    if stable:
        if args.allow_dirty or _git("status", "--porcelain"):
            raise ReleaseError("正式版要求工作区干净")
        if f"v{version}" not in _git("tag", "--points-at", "HEAD").split():
            raise ReleaseError(f"HEAD 上没有 v{version} tag")
        notes = changelog_notes((REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), version)
    else:
        if "-" not in version:
            raise ReleaseError("测试版须用预发布号(如 1.0.0-m9.2),正式号只走正式流程")
        notes = (args.notes or "").strip() or f"测试版 {version}"
    out_dir = REPO_ROOT / "build" / "release" / version
    artifacts = _artifacts(out_dir, version, skip=args.skip_build, allow_dirty=args.allow_dirty)
    manifest = write_manifest(out_dir, version, notes, artifacts,
                              released_at=datetime.now(env_timezone()).isoformat(timespec="seconds"))
    _sign(manifest, Path(args.key))
    files = [manifest, manifest.with_name(MANIFEST + ".sig"), *(Path(a["path"]) for a in artifacts)]
    if not args.dry_run:
        _publish(args.repo, version, notes, files, out_dir)
    return {"version": version, "repo": args.repo, "dry_run": args.dry_run, "files": [str(f) for f in files]}


def changelog_notes(text: str, version: str) -> str:
    match = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)", text, re.MULTILINE | re.DOTALL)
    if not match or not match.group(1).strip():
        raise ReleaseError(f"CHANGELOG.md 没有 [{version}] 版本块")
    return match.group(1).strip()


def write_manifest(out_dir: Path, version: str, notes: str, artifacts: list, *, released_at: str) -> Path:
    by_kind = {a["kind"]: a for a in artifacts}
    for kind in ("app", "shell", "install", "linux"):
        if kind not in by_kind:
            raise ReleaseError(f"缺少 {kind} 产物")
    manifest = {
        "version": version,
        "released_at": released_at,
        "notes": notes,
        "shell_sha256": by_kind["shell"]["sha256"],
        "assets": [{k: a[k] for k in ("name", "kind", "size", "sha256")} for a in artifacts],
    }
    path = out_dir / MANIFEST
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _artifacts(out_dir: Path, version: str, *, skip: bool, allow_dirty: bool) -> list:
    summary = out_dir / "build-summary.json"
    if not skip:
        cmd = [sys.executable, str(REPO_ROOT / "scripts" / "build_package.py"), "--version", version,
               "--out-dir", str(out_dir)]
        if allow_dirty:
            cmd.append("--allow-dirty")
        result = subprocess.run(cmd, cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=sys.stderr, text=True)
        line = (result.stdout.strip().splitlines() or ["{}"])[-1]
        if result.returncode != 0:
            raise ReleaseError(f"构建失败: {line}")
        out_dir.mkdir(parents=True, exist_ok=True)
        summary.write_text(line, encoding="utf-8")
    try:
        return json.loads(summary.read_text(encoding="utf-8"))["artifacts"]
    except (OSError, ValueError, KeyError) as exc:
        raise ReleaseError(f"没有可复用的构建产物 {summary}") from exc


def _sign(manifest: Path, key: Path) -> None:
    """签完立刻用外壳内置公钥(desktop/release.pub)验一遍:公钥没换上去的话,客户端会全部拒收。"""
    tool = ["go", "run", "./cmd/opsagent-release"]
    for cmd in ([*tool, "sign", "-key", str(key), str(manifest)],
                [*tool, "verify", "-pub", "release.pub", str(manifest)]):
        result = subprocess.run(cmd, cwd=DESKTOP, capture_output=True, text=True)
        if result.returncode != 0:
            raise ReleaseError(f"{cmd[3]} 失败: {result.stderr.strip()}")


def _publish(repo: str, version: str, notes: str, files: list, out_dir: Path) -> None:
    notes_file = out_dir / "release-notes.md"
    notes_file.write_text(notes + "\n", encoding="utf-8")
    cmd = ["gh", "release", "create", f"v{version}", "--repo", repo, "--title", f"v{version}",
           "--notes-file", str(notes_file), "--latest", *map(str, files)]
    # 预发布号也按普通 release 发:/releases/latest 不返回 prerelease,标了外壳就查不到
    result = subprocess.run(cmd, cwd=REPO_ROOT, stdout=sys.stderr, stderr=sys.stderr)
    if result.returncode != 0:
        raise ReleaseError(f"gh release create 失败({result.returncode})")


def _project_version() -> str:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise ReleaseError("pyproject.toml 缺少 version")
    return match.group(1)


def _git(*args, cwd: Path = REPO_ROOT) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


if __name__ == "__main__":
    sys.exit(main())
