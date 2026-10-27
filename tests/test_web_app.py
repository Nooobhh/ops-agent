import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from lib.alerts.timeline import rebuild
from web.server.app import create_app
from web.server.context import RequestContext, request_context
from web.server.data import timeline_reader


def test_health_returns_200_and_reports_available_sources(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_dir.joinpath("alerts.json").write_text(json.dumps({
        "version": 1, "active": {}, "source_fail": {},
    }), encoding="utf-8")
    jsonl = tmp_path / "jsonl"
    jsonl.mkdir()
    rebuild(state_dir / "timeline.db", jsonl)
    build = tmp_path / "build"
    build.mkdir()
    build.joinpath("routing.json").write_text(
        json.dumps({"alerts": {}, "docs": []}), encoding="utf-8",
    )
    build.joinpath("wiki.html").write_text("<html>wiki</html>", encoding="utf-8")

    response = TestClient(create_app(state_dir=state_dir, repo_root=tmp_path)).get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "data_sources": {
            "timeline_db": True,
            "state_json": True,
            "routing_json": True,
            "wiki_html": True,
            "frontend_built": False,
        },
        "timeline": {"status": "ok", "detail": None},
        "consistency": {"ok": True, "detail": None},
    }


def test_health_missing_sources_still_returns_200(tmp_path):
    response = TestClient(create_app(
        state_dir=tmp_path / "state", repo_root=tmp_path,
    )).get("/api/health")

    assert response.status_code == 200
    assert response.json()["data_sources"] == {
        "timeline_db": False,
        "state_json": False,
        "routing_json": False,
        "wiki_html": False,
        "frontend_built": False,
    }


@pytest.mark.parametrize(
    ("db_contents", "case"),
    [(b"", "zero-byte"), (b"not a sqlite database", "corrupt")],
)
def test_unreadable_timeline_degrades_all_api_endpoints(tmp_path, db_contents, case):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_dir.joinpath("timeline.db").write_bytes(db_contents)
    client = TestClient(
        create_app(state_dir=state_dir, repo_root=tmp_path),
        raise_server_exceptions=False,
    )

    health = client.get("/api/health")
    overview = client.get("/api/overview", params={"range": "24h"})
    alerts = client.get("/api/alerts")

    assert health.status_code == 200, case
    assert health.json()["data_sources"]["timeline_db"] is False
    assert overview.status_code == 200, case
    assert overview.json()["stats"] == {
        "active": 0, "active_non_chronic": 0, "new_today": 0,
        "resolved_today": 0, "suppressed_today": 0, "chronic": 0,
    }
    assert overview.json()["changes"] == {
        "range": "24h", "total": 0, "items": [],
    }
    assert alerts.status_code == 200, case
    assert alerts.json()["items"] == []
    assert alerts.json()["total"] == 0


def test_app_starts_when_frontend_artifacts_are_missing(tmp_path, monkeypatch):
    from web.server import app as app_module

    monkeypatch.setattr(app_module.sys, "platform", "linux")
    app = create_app(state_dir=tmp_path / "state", repo_root=tmp_path)

    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "前端尚未构建" in response.text
        assert "npm ci" in response.text
        assert "npm run build" in response.text
        assert client.get("/kb/").status_code == 404


def test_lifespan_starts_and_stops_scheduler_on_macos(tmp_path, monkeypatch):
    from web.server import app as app_module

    lifecycle = []
    scheduler = Mock()
    scheduler.start = AsyncMock(side_effect=lambda: lifecycle.append("scheduler"))
    scheduler.stop = AsyncMock()
    scheduler_class = Mock(return_value=scheduler)
    refresh = Mock(side_effect=lambda _argv: lifecycle.append("refresh") or 0)
    supervisor = Mock()
    supervisor.reconcile.side_effect = lambda _configured: lifecycle.append("reconcile")
    monkeypatch.setattr(app_module.sys, "platform", "darwin")
    monkeypatch.setattr(app_module, "Scheduler", scheduler_class)
    monkeypatch.setattr(app_module.node_credentials, "run", refresh)
    app = create_app(
        state_dir=tmp_path / "state",
        repo_root=tmp_path,
        triage_supervisor=supervisor,
    )

    with TestClient(app):
        assert app.state.scheduler is scheduler

    assert lifecycle == ["refresh", "scheduler", "reconcile"]
    refresh.assert_called_once_with(
        [
            "--credentials", str(tmp_path / "config" / "credentials.json"),
            "--profile", str(tmp_path / "config" / "auth-profiles.json"),
        ],
    )
    scheduler_class.assert_called_once_with(
        state_dir=tmp_path / "state", repo_root=tmp_path,
    )
    scheduler.start.assert_awaited_once_with()
    scheduler.stop.assert_awaited_once_with()


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_lifespan_starts_scheduler_cross_platform_without_node_credential_init(
        tmp_path, monkeypatch, platform):
    from web.server import app as app_module

    scheduler = Mock()
    scheduler.start = AsyncMock()
    scheduler.stop = AsyncMock()
    scheduler_class = Mock(return_value=scheduler)
    refresh = Mock()
    monkeypatch.setattr(app_module.sys, "platform", platform)
    monkeypatch.setattr(app_module, "Scheduler", scheduler_class)
    monkeypatch.setattr(app_module.node_credentials, "run", refresh)
    app = create_app(state_dir=tmp_path / "state", repo_root=tmp_path)

    with TestClient(app):
        assert app.state.scheduler is scheduler

    scheduler_class.assert_called_once_with(
        state_dir=tmp_path / "state", repo_root=tmp_path,
    )
    scheduler.start.assert_awaited_once_with()
    scheduler.stop.assert_awaited_once_with()
    refresh.assert_not_called()


def test_frontend_static_mount_falls_back_to_index_for_spa_routes(tmp_path):
    dist = tmp_path / "web" / "frontend" / "dist"
    dist.mkdir(parents=True)
    dist.joinpath("index.html").write_text("<html>console shell</html>", encoding="utf-8")
    client = TestClient(create_app(state_dir=tmp_path / "state", repo_root=tmp_path))

    response = client.get("/alerts/prometheus%3Ahd%3Adisk_full%3Anode1")

    assert response.status_code == 200
    assert "console shell" in response.text
    assert client.get("/api/health").json()["data_sources"]["frontend_built"] is True


def test_unregistered_api_path_404s_instead_of_serving_the_spa_shell(tmp_path):
    """前端比后端新时,缺失的接口必须是 404 JSON,不能是 200 + HTML。

    兜进 index.html 会让前端报「Unexpected token '<'」,把「进程没重启」
    这个真实病因藏起来——常驻进程不热重载,这类错配迟早会再发生。
    """
    dist = tmp_path / "web" / "frontend" / "dist"
    dist.mkdir(parents=True)
    dist.joinpath("index.html").write_text("<html>console shell</html>", encoding="utf-8")
    client = TestClient(create_app(state_dir=tmp_path / "state", repo_root=tmp_path))

    response = client.get("/api/does-not-exist")

    assert response.status_code == 404
    assert "console shell" not in response.text
    assert response.headers["content-type"].startswith("application/json")
    # SPA 路由的兜底不能被这条改动误伤
    assert client.get("/alerts").status_code == 200


def test_spa_fallback_keeps_404_for_windows_style_paths(tmp_path):
    """Windows 上 starlette 交给 get_response 的是反斜杠路径(api\\xxx),同样不得兜进 index.html。"""
    import asyncio

    from starlette.exceptions import HTTPException as StarletteHTTPException

    from web.server.app import _SpaStaticFiles

    tmp_path.joinpath("index.html").write_text("<html>console shell</html>", encoding="utf-8")
    static = _SpaStaticFiles(directory=str(tmp_path), html=True)
    scope = {"type": "http", "method": "GET", "headers": []}

    for path in ("api\\does-not-exist", "assets\\missing.js"):
        with pytest.raises(StarletteHTTPException) as exc:
            asyncio.run(static.get_response(path, scope))
        assert exc.value.status_code == 404
    assert asyncio.run(static.get_response("alerts\\detail", scope)).status_code == 200


def test_health_uses_single_request_context_dependency(tmp_path):
    app = create_app(state_dir=tmp_path / "state", repo_root=tmp_path)
    sentinel = RequestContext()
    app.dependency_overrides[request_context] = lambda: sentinel
    route = next(route for route in app.routes if getattr(route, "path", None) == "/api/health")

    response = TestClient(app).get("/api/health")

    assert response.status_code == 200
    assert [dependency.call for dependency in route.dependant.dependencies] == [request_context]


def test_health_reports_hot_journal_recovery_explicitly(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db = state_dir / "timeline.db"
    db.write_bytes(b"database placeholder")
    state_dir.joinpath("timeline.db-journal").write_bytes(b"hot journal")
    error = sqlite3.OperationalError("attempt to write a readonly database")
    error.sqlite_errorcode = sqlite3.SQLITE_READONLY_ROLLBACK
    error.sqlite_errorname = "SQLITE_READONLY_ROLLBACK"

    def recovering(_path):
        raise error

    monkeypatch.setattr(timeline_reader, "_connect_readonly", recovering)
    response = TestClient(
        create_app(state_dir=state_dir, repo_root=tmp_path),
        raise_server_exceptions=False,
    ).get("/api/health")

    assert response.status_code == 200
    assert response.json()["data_sources"]["timeline_db"] is False
    assert response.json()["timeline"]["status"] == "recovering"
    assert "时间线正在恢复中" in response.json()["timeline"]["detail"]
