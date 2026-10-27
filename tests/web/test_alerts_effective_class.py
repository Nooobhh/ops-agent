import json
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi.testclient import TestClient

from lib.alerts.model import AlertEvent
from lib.alerts.state import AlertsState
from lib.alerts.timeline import project
from web.server.app import create_app


def event(name="Muted"):
    return AlertEvent(
        source="prometheus", platform="alpha", alertname=name, severity="warning",
        host="node1", host_raw="node1", service="HDFS", state="firing", detail="",
    )


def test_muted_item_effective_class_is_ignore(tmp_path):
    state_dir = tmp_path / "state"
    ev = event()
    state = AlertsState(state_dir / "alerts.json")
    state.transition([ev], datetime.now(timezone.utc))
    state.active[ev.key]["class"] = "watch"
    state.active[ev.key]["muted"] = True
    state.save()
    item = TestClient(create_app(
        state_dir=state_dir, repo_root=tmp_path,
    )).get("/api/alerts").json()["items"][0]
    assert item["class"] == "watch"
    assert item["effective_class"] == "ignore"


def test_historical_muted_via_override_effective_class(tmp_path):
    state_dir = tmp_path / "state"
    key = "prometheus:alpha:MutedHistory:node1"
    project(state_dir / "timeline.db", [{
        "ts": datetime.now(timezone.utc).isoformat(), "type": "resolved",
        "key": key, "class": "chronic", "detail": {},
    }])
    state_dir.joinpath("alert-rule-overrides.json").write_text(json.dumps({
        "version": 1,
        "overrides": {"prometheus:MutedHistory": {"notify": False}},
    }), encoding="utf-8")
    item = TestClient(create_app(
        state_dir=state_dir, repo_root=tmp_path,
    )).get("/api/alerts").json()["items"][0]
    assert item["muted"] is True
    assert item["effective_class"] == "ignore"


def test_detail_current_effective_class(tmp_path):
    state_dir = tmp_path / "state"
    ev = event()
    state = AlertsState(state_dir / "alerts.json")
    state.transition([ev], datetime.now(timezone.utc))
    state.active[ev.key]["class"] = "chronic"
    state.active[ev.key]["muted"] = True
    state.save()
    current = TestClient(create_app(
        state_dir=state_dir, repo_root=tmp_path,
    )).get(f"/api/alerts/{quote(ev.key, safe='')}").json()["current"]
    assert current["class"] == "chronic"
    assert current["effective_class"] == "ignore"
