import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest.mock import Mock
from threading import Barrier

from fastapi.testclient import TestClient

from lib.alerts.state import AlertsState
from lib.alerts.timeline import project
from web.server.app import create_app
from web.server.context import RequestContext, request_context
from tests.envpack_helpers import as_env_pack


BOOT = datetime(2026, 7, 20, 1, 0, tzinfo=timezone.utc)
UPDATED = datetime(2026, 7, 20, 2, 0, tzinfo=timezone.utc)


def _client(tmp_path, monkeypatch):
    from lib.alerts import poll_control

    monkeypatch.setattr(poll_control, "system_boot_time", lambda: BOOT)
    as_env_pack(tmp_path)  # 告警源取临时环境包配置;调度器 tick 由 conftest 硬闸拦下
    (tmp_path / "config" / "alert-sources.yaml").write_text(
        "version: 1\nsources:\n"
        "  prometheus-central: {type: prometheus, plugin: prometheus}\n"
        "  manager-alpha: {type: demo, platform: alpha}\n"
        "  manager-beta: {type: demo, platform: beta}\n",
        encoding="utf-8",
    )
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_dir.joinpath("poll-enabled.json").write_text(json.dumps({
        "enabled": True,
        "updated_at": UPDATED.isoformat(),
        "updated_by": "web",
    }), encoding="utf-8")
    state = AlertsState(state_dir / "alerts.json")
    state.source_fail = {"demo:alpha": 2}
    state.last_tick_at = "2026-07-20T02:15:00+00:00"
    state.save()
    project(state_dir / "timeline.db", [
        {
            "ts": "2026-07-20T02:05:00+00:00", "type": "source_error",
            "key": "demo:alpha", "detail": "connection refused",
        },
        {
            "ts": "2026-07-20T02:10:00+00:00", "type": "new",
            "key": "prometheus:alpha:disk_full:node1", "detail": {},
        },
    ])
    return state_dir, TestClient(create_app(state_dir=state_dir, repo_root=tmp_path))


def test_poll_status_reports_gate_tick_and_three_sources(tmp_path, monkeypatch):
    _, client = _client(tmp_path, monkeypatch)

    response = client.get("/api/poll/status")

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["effective"] is True
    assert body["since_boot"] is True
    assert body["last_tick_at"] == "2026-07-20T02:15:00+00:00"
    assert set(body["sources"]) == {"prometheus", "demo:alpha", "demo:beta"}
    assert body["sources"]["demo:alpha"] == {
        "last_result": "failure",
        "consecutive_failures": 2,
        "last_error_at": "2026-07-20T02:05:00+00:00",
        "last_error": "connection refused",
        "notify": True,
    }
    assert body["sources"]["prometheus"]["last_result"] == "success"
    assert all(
        set(source_status) == {
            "last_result", "consecutive_failures", "last_error_at", "last_error", "notify",
        }
        for source_status in body["sources"].values()
    )


def test_poll_status_marks_stale_heartbeat_stalled_even_with_live_scheduler(
        tmp_path, monkeypatch):
    _, client = _client(tmp_path, monkeypatch)
    scheduler = Mock()
    scheduler.is_poll_running.return_value = True
    scheduler.poll_started_at.return_value = BOOT
    client.app.state.scheduler = scheduler

    response = client.get("/api/poll/status")

    assert response.status_code == 200
    assert response.json()["runtime_state"] == "stalled"


def test_poll_status_marks_new_scheduler_starting_until_first_tick(
        tmp_path, monkeypatch):
    _, client = _client(tmp_path, monkeypatch)
    scheduler = Mock()
    scheduler.is_poll_running.return_value = True
    scheduler.poll_started_at.return_value = datetime.now(timezone.utc)
    client.app.state.scheduler = scheduler

    response = client.get("/api/poll/status")

    assert response.status_code == 200
    assert response.json()["runtime_state"] == "starting"


def test_poll_status_marks_enabled_gate_stopped_without_scheduler(
        tmp_path, monkeypatch):
    _, client = _client(tmp_path, monkeypatch)
    client.app.state.scheduler = None

    response = client.get("/api/poll/status")

    assert response.status_code == 200
    assert response.json()["runtime_state"] == "stopped"


def test_poll_toggle_only_changes_marker_and_uses_request_context(tmp_path, monkeypatch):
    state_dir, client = _client(tmp_path, monkeypatch)
    protected = [state_dir / "alerts.json", state_dir / "timeline.db"]
    before = {
        path: (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in protected
    }
    app = client.app
    app.dependency_overrides[request_context] = lambda: RequestContext()

    response = client.post("/api/poll/toggle", json={"enabled": False})

    assert response.status_code == 200
    assert response.json()["enabled"] is False
    marker = json.loads(state_dir.joinpath("poll-enabled.json").read_text(encoding="utf-8"))
    assert marker["enabled"] is False
    assert marker["updated_by"] == "web"
    for path, snapshot in before.items():
        assert (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()) == snapshot
    route = next(route for route in app.routes if getattr(route, "path", None) == "/api/poll/toggle")
    assert [dependency.call for dependency in route.dependant.dependencies] == [request_context]


def test_poll_toggle_controls_embedded_scheduler(tmp_path, monkeypatch):
    _, client = _client(tmp_path, monkeypatch)
    scheduler = Mock()
    client.app.state.scheduler = scheduler

    enabled = client.post("/api/poll/toggle", json={"enabled": True})
    disabled = client.post("/api/poll/toggle", json={"enabled": False})

    assert enabled.status_code == 200
    assert disabled.status_code == 200
    scheduler.enable_poll.assert_called_once_with()
    scheduler.disable_poll.assert_called_once_with()


def test_poll_toggle_without_embedded_scheduler_still_writes_gate(
        tmp_path, monkeypatch):
    state_dir, client = _client(tmp_path, monkeypatch)
    client.app.state.scheduler = None

    response = client.post("/api/poll/toggle", json={"enabled": False})

    assert response.status_code == 200
    assert response.json()["enabled"] is False
    marker = json.loads(
        state_dir.joinpath("poll-enabled.json").read_text(encoding="utf-8")
    )
    assert marker["enabled"] is False


def test_concurrent_poll_toggles_never_return_5xx_and_leave_consistent_state(
        tmp_path, monkeypatch):
    from lib import atomic_file

    state_dir, client = _client(tmp_path, monkeypatch)
    concurrent_client = TestClient(client.app, raise_server_exceptions=False)
    original_replace = atomic_file.os.replace
    writers = Barrier(20)

    def synchronized_replace(source, destination):
        writers.wait(timeout=5)
        original_replace(source, destination)

    monkeypatch.setattr(atomic_file.os, "replace", synchronized_replace)
    with ThreadPoolExecutor(max_workers=20) as pool:
        responses = list(pool.map(
            lambda _index: concurrent_client.post(
                "/api/poll/toggle", json={"enabled": True},
            ),
            range(20),
        ))

    assert [response.status_code for response in responses] == [200] * 20
    assert all(response.json()["enabled"] is True for response in responses)
    marker = json.loads(
        state_dir.joinpath("poll-enabled.json").read_text(encoding="utf-8")
    )
    assert marker["enabled"] is True
    assert marker["updated_by"] == "web"
    assert not list(state_dir.glob(".poll-enabled.json.*.tmp"))
