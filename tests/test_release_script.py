import json
import subprocess
from types import SimpleNamespace

import pytest

from scripts import build_package, release

CHANGELOG = """# Changelog

## [Unreleased]

- 还没发

## [1.0.0] - 2026-11-01

主题:通用 OPS 中台

- 新增 A

## [0.20.0] - 2026-10-13

- 旧的
"""


def test_changelog_notes_takes_only_that_version_block():
    assert release.changelog_notes(CHANGELOG, "1.0.0") == "主题:通用 OPS 中台\n\n- 新增 A"
    assert release.changelog_notes(CHANGELOG, "0.20.0") == "- 旧的"
    with pytest.raises(release.ReleaseError, match="没有 \\[9.9.9\\]"):
        release.changelog_notes(CHANGELOG, "9.9.9")


def _artifacts(tmp_path):
    out = []
    for kind in ("shell", "app", "install", "linux"):
        path = tmp_path / f"{kind}.bin"
        path.write_bytes(kind.encode())
        out.append({"name": path.name, "kind": kind, "path": str(path), "size": len(kind), "sha256": kind * 4})
    return out


def test_manifest_lists_assets_and_shell_hash(tmp_path):
    path = release.write_manifest(tmp_path, "1.0.0-m9.1", "说明", _artifacts(tmp_path), released_at="t")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == "1.0.0-m9.1" and data["notes"] == "说明" and data["released_at"] == "t"
    assert data["shell_sha256"] == "shell" * 4
    assert [a["kind"] for a in data["assets"]] == ["shell", "app", "install", "linux"]
    assert all(set(a) == {"name", "kind", "size", "sha256"} for a in data["assets"])
    with pytest.raises(release.ReleaseError, match="缺少 linux"):
        release.write_manifest(tmp_path, "1.0.0", "", _artifacts(tmp_path)[:3], released_at="t")


def test_sign_checks_against_embedded_public_key(tmp_path, monkeypatch):
    calls = []

    def fake_run(cmd, cwd, capture_output, text):
        calls.append(cmd)
        failed = cmd[3] == "verify"
        return SimpleNamespace(returncode=1 if failed else 0, stderr="签名校验失败")

    monkeypatch.setattr(release.subprocess, "run", fake_run)
    with pytest.raises(release.ReleaseError, match="verify 失败: 签名校验失败"):
        release._sign(tmp_path / "manifest.json", tmp_path / "key")
    assert calls[1][-3:] == ["-pub", "release.pub", str(tmp_path / "manifest.json")]


def test_publish_uses_latest_not_prerelease(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(release.subprocess, "run",
                        lambda cmd, **kw: seen.setdefault("cmd", cmd) and SimpleNamespace(returncode=0))
    release._publish("o/r", "1.0.0-m9.2", "说明", [tmp_path / "a"], tmp_path)
    cmd = seen["cmd"]
    assert cmd[:4] == ["gh", "release", "create", "v1.0.0-m9.2"]
    assert "--latest" in cmd and "--prerelease" not in cmd
    assert (tmp_path / "release-notes.md").read_text(encoding="utf-8") == "说明\n"


def test_release_guards(tmp_path, monkeypatch):
    key = tmp_path / "key"
    key.write_text("x", encoding="utf-8")
    args = release._parse_args(["--repo", "o/r", "--key", str(key), "--version", "1.0.0"])
    with pytest.raises(release.ReleaseError, match="预发布号"):
        release.release(args)
    with pytest.raises(release.ReleaseError, match="缺 --repo"):
        release.release(release._parse_args(["--key", str(key)]))
    with pytest.raises(release.ReleaseError, match="找不到签名私钥"):
        release.release(release._parse_args(["--dry-run", "--key", str(tmp_path / "missing")]))


def test_build_package_rejects_bad_version_and_lists_targets():
    assert set(build_package.TARGETS) == {"windows", "linux"}
    args = build_package._parse_args(["--version", "1.0", "--allow-dirty"])
    with pytest.raises(build_package.BuildError, match="不是 SemVer"):
        build_package.build(args)


def test_release_script_cli_reports_json(tmp_path):
    result = subprocess.run(
        [release.sys.executable, str(release.REPO_ROOT / "scripts" / "release.py"),
         "--dry-run", "--key", str(tmp_path / "missing")],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["error"] == "release_failed"


def test_package_keeps_only_the_example_env_pack():
    """发布包只装程序:拆仓前核心仓 envs/ 下的私有环境包不进包,示例包随代码进。"""
    keep = build_package._packaged
    assert all(keep(name) for name in ("lib/envpack/__init__.py", "envs/", "envs/example/", "envs/example/env.yaml"))
    assert not any(keep(name) for name in ("envs/lab/", "envs/lab/env.yaml", "envs/examples/x", "tests/x.py",
                                           "desktop/go.mod", ".github/workflows/ci.yml"))


def test_verify_rejects_environment_data_in_the_package(tmp_path):
    target = build_package.TARGETS["linux"]
    app = tmp_path / "app"
    for rel in (*build_package.COMMON_REQUIRED, target.python, "envs/example/env.yaml",
                *(f"{target.site_packages}/{pkg}" for pkg in ("fastapi", "uvicorn", "paramiko", "yaml", "tzdata"))):
        (app / rel).parent.mkdir(parents=True, exist_ok=True)
        (app / rel).write_text("x", encoding="utf-8")
    build_package._verify(app, target)
    for leak in ("envs/lab", "seed", "env", "knowledge"):
        (app / leak).mkdir()
        with pytest.raises(build_package.BuildError, match="不应出现"):
            build_package._verify(app, target)
        (app / leak).rmdir()
    (app / "envs" / "example" / "env.yaml").unlink()
    with pytest.raises(build_package.BuildError, match="envs/example/env.yaml"):
        build_package._verify(app, target)


def test_build_and_release_take_no_env_pack_or_knowledge_seed():
    for flag in ("--env-pack", "--no-knowledge-seed"):
        with pytest.raises(SystemExit):
            build_package._parse_args([flag, "x"] if flag == "--env-pack" else [flag])
    with pytest.raises(SystemExit):
        release._parse_args(["--env-pack", "x"])
