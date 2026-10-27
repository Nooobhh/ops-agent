import io
import json
import os
import tarfile
from pathlib import Path

import pytest

from lib.apphost import upgrade as upgrade_module
from lib.apphost.controller import ServerState
from lib.apphost.upgrade import UpgradeError, current_version, switch_current, upgrade
from scripts import opsagent


def _json(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_run_rejects_unknown_or_unsafe_script(capsys):
    for name in ("../x", "nope_not_here", "opsagent", ""):
        assert opsagent.main(["run", name]) == 1
        assert _json(capsys)["error"] == "unknown_script"


def test_run_and_kb_pass_through_with_current_interpreter(monkeypatch):
    calls = []
    monkeypatch.setattr(opsagent.subprocess, "run",
                        lambda cmd, cwd: calls.append((cmd, cwd)) or type("R", (), {"returncode": 3})())
    assert opsagent.main(["run", "inventory", "show", "--cluster", "x"]) == 3
    assert opsagent.main(["kb", "lint", "--json"]) == 3
    assert opsagent.main(["kb", "rebuild"]) == 3
    (run_cmd, cwd), (lint_cmd, _), (rebuild_cmd, _) = calls
    assert run_cmd[0] == opsagent.sys.executable
    assert run_cmd[1:] == [str(opsagent.REPO_ROOT / "scripts" / "inventory.py"), "show", "--cluster", "x"]
    assert cwd == str(opsagent.REPO_ROOT)
    assert lint_cmd[1:] == ["-m", "lib.kb", "--json"]
    assert rebuild_cmd[1:] == [str(opsagent.REPO_ROOT / "scripts" / "build_wiki.py")]


def test_version_and_prepare_in_source_checkout(capsys):
    assert opsagent.main(["version"]) == 0
    payload = _json(capsys)
    assert payload["packaged"] is False and payload["version"]
    assert opsagent.main(["prepare"]) == 0
    assert _json(capsys) == {"ok": True}


class FakeController:
    def __init__(self, states, *, start_result=True):
        self.states = list(states)
        self.start_result = start_result
        self.port = 8420
        self.console_url = "http://127.0.0.1:8420/"
        self.log_path = Path("web_server.log")
        self.last_error = ""
        self.stopped = 0

    def start(self):
        return self.start_result

    def stop(self):
        self.stopped += 1

    def refresh(self):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    def server_pid(self):
        return 4242


def test_start_waits_until_running(monkeypatch, capsys):
    fake = FakeController([ServerState.STARTING, ServerState.RUNNING])
    monkeypatch.setattr(opsagent, "_controller", lambda args: fake)
    monkeypatch.setattr(opsagent.time, "sleep", lambda _s: None)
    assert opsagent.main(["start"]) == 0
    assert _json(capsys) == {"ok": True, "state": "running", "pid": 4242, "port": 8420,
                             "url": fake.console_url, "log": "web_server.log", "started": True}


def test_start_reports_failure_with_last_error(monkeypatch, capsys):
    fake = FakeController([ServerState.STARTING, ServerState.FAILED])
    fake.last_error = "端口被占用"
    monkeypatch.setattr(opsagent, "_controller", lambda args: fake)
    monkeypatch.setattr(opsagent.time, "sleep", lambda _s: None)
    assert opsagent.main(["start"]) == 1
    assert _json(capsys)["message"] == "端口被占用"


def test_status_on_free_port_is_stopped(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPS_AGENT_DATA_ROOT", str(tmp_path))
    assert opsagent.main(["status", "--port", "1"]) == 0
    payload = _json(capsys)
    assert payload["state"] == "stopped" and payload["pid"] is None


def test_upgrade_requires_installed_layout(monkeypatch, capsys):
    monkeypatch.delenv(opsagent.HOME_ENV, raising=False)
    assert opsagent.main(["upgrade", "x.tar.gz"]) == 1
    assert _json(capsys)["error"] == "not_installed"


# ---- lib.apphost.upgrade ----

# 离线升级只用于 Linux 安装形态(app/current 是符号链接),Windows CI 上不跑
linux_layout = pytest.mark.skipif(os.name == "nt", reason="Linux 安装形态专用(符号链接)")

def _install(home: Path, version: str) -> None:
    app = home / "app" / version
    (app / "packaging" / "linux").mkdir(parents=True)
    (app / "package-info.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    (app / "packaging" / "linux" / "opsagent").write_text(f"launcher {version}", encoding="utf-8")
    (home / "bin").mkdir(exist_ok=True)


def _tarball(tmp_path: Path, version: str, *, dir_name=None) -> Path:
    path = tmp_path / f"opsagent-{version}.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, text in {
            f"opsagent/app/{dir_name or version}/package-info.json": json.dumps({"version": version}),
            f"opsagent/app/{dir_name or version}/packaging/linux/opsagent": f"launcher {version}",
        }.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("opsagent/app/current")
        link.type, link.linkname = tarfile.SYMTYPE, version
        archive.addfile(link)
    return path


class UpgradeController:
    def __init__(self, app_dir, log, *, healthy, crashes=False):
        self.app_dir, self.log, self.healthy, self.crashes = app_dir, log, healthy, crashes
        self.log_path = Path(app_dir) / "web_server.log"

    def is_healthy(self):
        return self.healthy(self.app_dir.name, self.log)

    def refresh(self):
        if self.is_healthy():
            return ServerState.RUNNING
        return ServerState.FAILED if ("start", self.app_dir.name) in self.log and self.crashes else ServerState.STARTING

    def start(self):
        self.log.append(("start", self.app_dir.name))

    def stop(self):
        self.log.append(("stop", self.app_dir.name))


def _home(tmp_path):
    home = tmp_path / "home"
    for version in ("0.9.0", "1.0.0"):
        _install(home, version)
    switch_current(home, "1.0.0")
    return home


def _run_upgrade(tmp_path, home, tarball, healthy, *, crashes=False, wait=0.05):
    log = []
    result = upgrade(
        home, tarball, tmp_path / "data", wait_seconds=wait,
        controller_factory=lambda app_dir: UpgradeController(app_dir, log, healthy=healthy, crashes=crashes),
        sleep=lambda _s: None,
    )
    return result, log


@linux_layout
def test_upgrade_switches_verifies_and_keeps_previous(tmp_path):
    home = _home(tmp_path)
    result, log = _run_upgrade(
        tmp_path, home, _tarball(tmp_path, "1.1.0"),
        healthy=lambda version, log: version == "1.1.0" and ("start", "1.1.0") in log,
    )
    assert result == {"from": "1.0.0", "to": "1.1.0"}
    assert log == [("start", "1.1.0"), ("stop", "1.1.0")]
    assert current_version(home) == "1.1.0"
    assert sorted(p.name for p in (home / "app").iterdir()) == ["1.0.0", "1.1.0", "current"]
    assert (home / "bin" / "opsagent").read_text(encoding="utf-8") == "launcher 1.1.0"


@linux_layout
def test_upgrade_rolls_back_when_new_version_never_healthy(tmp_path):
    home = _home(tmp_path)
    with pytest.raises(UpgradeError, match="已切回 1.0.0"):
        _run_upgrade(tmp_path, home, _tarball(tmp_path, "1.1.0"), healthy=lambda v, log: False)
    assert current_version(home) == "1.0.0"
    assert not (home / "app" / "1.1.0").exists()
    assert sorted(p.name for p in (home / "app").iterdir()) == ["0.9.0", "1.0.0", "current"]


@linux_layout
def test_upgrade_stops_waiting_once_new_version_exits(tmp_path):
    home = _home(tmp_path)
    started = upgrade_module.time.monotonic()
    with pytest.raises(UpgradeError, match="已切回 1.0.0"):
        _run_upgrade(tmp_path, home, _tarball(tmp_path, "1.1.0"), healthy=lambda v, log: False,
                     crashes=True, wait=30)
    assert upgrade_module.time.monotonic() - started < 5


@linux_layout
def test_upgrade_refuses_while_running_and_rejects_bad_packages(tmp_path):
    home = _home(tmp_path)
    with pytest.raises(UpgradeError, match="正在运行"):
        _run_upgrade(tmp_path, home, _tarball(tmp_path, "1.1.0"), healthy=lambda v, log: True)
    with pytest.raises(UpgradeError, match="1.0.0 已安装"):
        _run_upgrade(tmp_path, home, _tarball(tmp_path, "1.0.0"), healthy=lambda v, log: False)
    with pytest.raises(UpgradeError, match="不一致"):
        _run_upgrade(tmp_path, home, _tarball(tmp_path, "1.2.0", dir_name="1.2"),
                     healthy=lambda v, log: False)
    bad = tmp_path / "bad.tar.gz"
    bad.write_bytes(b"not a tarball")
    with pytest.raises(UpgradeError, match="解包失败"):
        _run_upgrade(tmp_path, home, bad, healthy=lambda v, log: False)
    assert current_version(home) == "1.0.0"
    assert not [p for p in (home / "app").iterdir() if p.name.startswith(".")]


def test_upgrade_controller_uses_new_version_runtime(tmp_path):
    calls = []
    controller = upgrade_module._controller(
        tmp_path / "app" / "1.1.0", tmp_path / "data" / "state", 8420,
        lambda cmd, **kw: calls.append(cmd) or type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )
    assert controller.python == str(tmp_path / "app" / "1.1.0" / "runtime" / "bin" / "python3")
    assert controller._prepare() is None
    assert calls == [[controller.python, str(tmp_path / "app" / "1.1.0" / "scripts" / "opsagent.py"), "prepare"]]
