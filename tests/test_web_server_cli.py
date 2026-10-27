import json
import logging
import os
import socket

import pytest

from lib.file_lock import FileLock
from scripts import web_server


def test_parse_args_defaults_and_overrides():
    defaults = web_server._parse_args([])
    assert defaults.host == "127.0.0.1"
    assert defaults.port == 8420
    assert defaults.state_dir == "state"
    assert defaults.chronic_hours == 24.0

    custom = web_server._parse_args([
        "--host", "127.0.0.2", "--port", "9000", "--state-dir", "runtime",
        "--chronic-hours", "6",
    ])
    assert custom.host == "127.0.0.2"
    assert custom.port == 9000
    assert custom.state_dir == "runtime"
    assert custom.chronic_hours == 6.0


def test_main_configures_direct_network_before_run(monkeypatch):
    for key in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
    ):
        monkeypatch.setenv(key, "http://proxy:7890")
    captured = {}

    def fake_run():
        captured.update(os.environ)
        return 0

    monkeypatch.setattr(web_server, "run", fake_run)

    assert web_server.main() == 0
    assert captured["NO_PROXY"] == "*"
    # Windows 环境变量不分大小写,no_proxy 与 NO_PROXY 是同一个键
    assert captured.get("no_proxy", captured["NO_PROXY"]) == "*"
    assert not any(
        key in captured
        for key in (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "all_proxy",
        )
    )


@pytest.mark.parametrize("value", ["0", "-1"])
def test_non_positive_chronic_hours_returns_readable_json(value, tmp_path, capsys):
    rc = web_server.run([
        "--state-dir", str(tmp_path / "state"), "--chronic-hours", value,
    ])

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert rc != 0
    assert output["error"] == "invalid_chronic_hours"
    assert "常驻" in output["message"]
    assert "大于 0" in output["message"]
    assert "Traceback" not in captured.err


def test_chronic_hours_is_passed_to_application_factory(tmp_path, monkeypatch):
    captured = {}

    def fake_create_app(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(web_server, "create_app", fake_create_app)
    monkeypatch.setattr(web_server, "_port_available", lambda _host, _port: True)
    monkeypatch.setattr(web_server, "_serve", lambda *_args, **_kwargs: True)

    rc = web_server.run([
        "--state-dir", str(tmp_path / "state"), "--chronic-hours", "6",
    ])

    assert rc == 0
    assert captured["chronic_hours"] == 6.0


def test_triage_supervisor_is_always_built(tmp_path, monkeypatch):
    created = []

    class FakeSupervisor:
        def __init__(self, **kwargs):
            created.append(kwargs)

    monkeypatch.setattr(web_server, "LocalProcessSupervisor", FakeSupervisor)

    supervisor = web_server._build_triage_supervisor(
        tmp_path,
        tmp_path / "state",
    )

    assert isinstance(supervisor, FakeSupervisor)
    assert created == [{
        "repo_root": tmp_path,
        "state_dir": tmp_path / "state",
    }]


def test_second_instance_reports_holder_and_preserves_first_lock(tmp_path, capsys):
    state_dir = tmp_path / "state"
    holder_path = state_dir / "web_server.holder.json"
    state_dir.mkdir()
    holder = {"pid": 4321, "host": "127.0.0.1", "port": 8420}
    holder_path.write_text(json.dumps(holder), encoding="utf-8")

    with FileLock(str(state_dir / "web_server.lock"), timeout=0):
        rc = web_server.run([
            "--state-dir", str(state_dir), "--port", "8421",
        ])
        assert json.loads(holder_path.read_text(encoding="utf-8")) == holder

    output = json.loads(capsys.readouterr().out)
    assert rc != 0
    assert output["error"] == "already_running"
    assert output["pid"] == 4321
    assert output["port"] == 8420


def test_running_instance_never_rewrites_the_lock_file(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    seen = {}

    def fake_serve(*_args, **_kwargs):
        # Windows 字节锁是强制锁,持锁期间连本进程也读不了被锁字节,只看大小
        seen["lock_size"] = (state_dir / "web_server.lock").stat().st_size
        seen["holder"] = json.loads(
            (state_dir / "web_server.holder.json").read_text(encoding="utf-8")
        )
        return True

    monkeypatch.setattr(web_server, "create_app", lambda **_kwargs: object())
    monkeypatch.setattr(web_server, "_port_available", lambda _host, _port: True)
    monkeypatch.setattr(web_server, "_serve", fake_serve)

    assert web_server.run(["--state-dir", str(state_dir)]) == 0
    assert seen["lock_size"] in (0, 1)  # Windows 下 FileLock 只写一个占位字节
    assert seen["holder"]["pid"] == os.getpid()


def test_port_owned_by_other_process_returns_readable_json(tmp_path, capsys):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    try:
        rc = web_server.run([
            "--state-dir", str(tmp_path / "state"), "--port", str(port),
        ])
    finally:
        listener.close()

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert rc != 0
    assert output["error"] == "port_in_use"
    assert output["port"] == port
    assert str(port) in output["message"]
    assert "更换" in output["message"] or "停止" in output["message"]
    assert "Traceback" not in captured.err


def test_stale_stop_request_is_cleared_before_serving(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    stop_path = state_dir / web_server.STOP_REQUEST_NAME
    stop_path.write_text("", encoding="utf-8")
    old = web_server._PROCESS_STARTED_AT - 60
    os.utime(stop_path, (old, old))
    seen = {}

    def fake_serve(_app, _host, _port, path):
        seen["path"] = path
        seen["exists"] = path.exists()
        return True

    monkeypatch.setattr(web_server, "create_app", lambda **_kwargs: object())
    monkeypatch.setattr(web_server, "_port_available", lambda _host, _port: True)
    monkeypatch.setattr(web_server, "_serve", fake_serve)

    assert web_server.run(["--state-dir", str(state_dir)]) == 0
    assert seen == {"path": stop_path, "exists": False}


def test_serve_that_never_started_returns_readable_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(web_server, "create_app", lambda **_kwargs: object())
    monkeypatch.setattr(web_server, "_port_available", lambda _host, _port: True)
    monkeypatch.setattr(web_server, "_serve", lambda *_args, **_kwargs: False)

    rc = web_server.run(["--state-dir", str(tmp_path / "state")])

    output = json.loads(capsys.readouterr().out)
    assert rc != 0
    assert output["error"] == "server_start_failed"


def test_stop_request_file_asks_server_to_exit(tmp_path):
    class FakeServer:
        should_exit = False

    server = FakeServer()
    stop_path = tmp_path / web_server.STOP_REQUEST_NAME
    thread = web_server._watch_stop_request(server, stop_path, interval=0.01)
    assert not server.should_exit

    stop_path.write_text("", encoding="utf-8")
    thread.join(timeout=2)

    assert server.should_exit
    assert not stop_path.exists()


def test_stop_request_written_during_startup_is_kept(tmp_path):
    stop_path = tmp_path / web_server.STOP_REQUEST_NAME
    stop_path.write_text("", encoding="utf-8")

    web_server._clear_stale_stop_request(stop_path, started_at=stop_path.stat().st_mtime - 1)

    assert stop_path.exists()


def test_healthy_probe_access_log_is_dropped():
    def access(path, status):
        return logging.LogRecord(
            "uvicorn.access", logging.INFO, __file__, 1,
            '%s - "%s %s HTTP/%s" %d', ("127.0.0.1:1", "GET", path, "1.1", status), None,
        )

    assert web_server._skip_healthy_probe(access("/api/health", 200)) is False
    assert web_server._skip_healthy_probe(access("/api/health", 500)) is True
    assert web_server._skip_healthy_probe(access("/api/alerts", 200)) is True
