import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import yaml
from fastapi.testclient import TestClient

from lib.alerts.model import AlertEvent
from lib.alerts.state import AlertsState
from lib.alerts.timeline import project
from lib.alerts import timeline as timeline_module
from web.server.app import create_app
from web.server.data import timeline_reader
from web.server.routes.overview import _render_change


def _entry_event(source, alertname, *, host_raw, service, severity):
    return AlertEvent(
        source=source, platform="alpha", alertname=alertname, severity=severity,
        host="node1", host_raw=host_raw, service=service, state="firing", detail={},
    )


def test_overview_returns_stats_distribution_changes_and_consistency(tmp_path):
    now = datetime.now(timezone.utc)
    demo = _entry_event(
        "demo", "zookeeper_server_process", host_raw="node1",
        service="ZOOKEEPER", severity="critical",
    )
    prometheus = _entry_event(
        "prometheus", "zookeeper_port", host_raw="node1:2181",
        service="blackbox", severity="warning",
    )
    state_dir = tmp_path / "state"
    state = AlertsState(state_dir / "alerts.json")
    state.transition([demo, prometheus], now - timedelta(hours=1))
    state.save()
    rows = []
    for event in (demo, prometheus):
        rows.append({
            "ts": (now - timedelta(hours=1)).isoformat(), "type": "new",
            "key": event.key, "source": event.source, "platform": event.platform,
            "alertname": event.alertname, "host": event.host, "class": "watch",
            "detail": {}, "severity": event.severity, "service": event.service,
            "suppressed_by": demo.key if event is prometheus else None,
        })
    project(state_dir / "timeline.db", rows)
    config = tmp_path / "config"
    config.mkdir()
    config.joinpath("alert-dedup.yaml").write_text(yaml.safe_dump({
        "version": 1,
        "rules": [{
            "match": {
                "demo": {"platform": "alpha", "alertname": "zookeeper_server_process"},
                "prometheus": {"port": 2181},
            },
            "winner": "demo", "reason": "service context",
        }],
    }), encoding="utf-8")

    response = TestClient(create_app(state_dir=state_dir, repo_root=tmp_path)).get(
        "/api/overview", params={"range": "24h"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["stats"] == {
        "active": 2, "active_non_chronic": 2, "new_today": 2,
        "resolved_today": 0, "suppressed_today": 1, "chronic": 0,
        "chronic_class": 0, "active_non_chronic_class": 2, "ignore_class": 0,
    }
    assert len(body["distribution"]) == 24
    assert sum(item["count"] for item in body["distribution"]) == 2
    assert body["changes"]["range"] == "24h"
    assert body["changes"]["total"] == 2
    changes = {item["key"]: item for item in body["changes"]["items"]}
    assert set(changes) == {demo.key, prometheus.key}
    assert changes[demo.key]["status"] == "active"
    assert changes[prometheus.key]["status"] == "suppressed"
    assert changes[prometheus.key]["change_type"] == "new"
    assert changes[prometheus.key]["change_count"] == 1
    assert changes[prometheus.key]["chronic"] is False
    assert all("first_seen" in item for item in changes.values())
    assert all(item["first_seen"] is not None for item in changes.values())
    for item in changes.values():
        datetime.fromisoformat(item["first_seen"])
    assert "recent" not in body
    assert body["consistency"] == {"ok": True, "detail": None}
    datetime.fromisoformat(body["generated_at"])


def test_overview_reports_ssh_credentials_expiring_within_seven_days(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "credentials.json").write_text(json.dumps({
        "192.0.2.11": {
            "user": "token", "password": "secret", "target": "node1",
            "target_ip": "192.0.2.11", "mode": 0, "valid_from": int(time.time()),
            "valid_to": int(time.time()) + 3 * 86400, "pty": True,
        },
        "192.0.2.12": {
            "user": "token2", "password": "secret2", "target": "node2",
            "target_ip": "192.0.2.12", "mode": 0, "valid_from": int(time.time()),
            "valid_to": int(time.time()) + 30 * 86400, "pty": True,
        },
    }), encoding="utf-8")

    body = TestClient(create_app(
        state_dir=tmp_path / "state", repo_root=tmp_path,
    )).get("/api/overview").json()

    warning = body["credential_warnings"]
    assert warning["available"] is True
    assert warning["threshold_days"] == 7
    assert warning["count"] == 1
    assert warning["expired_count"] == 0
    assert warning["items"][0]["host"] == "192.0.2.11"


def test_render_resolved_change_without_timeline_first_seen_uses_null():
    rendered = _render_change(
        {
            "key": "demo:alpha:resolved_alert:node1",
            "type": "resolved",
            "ts": "2026-07-24T01:00:00+00:00",
            "change_count": 2,
        },
        active_entry=None,
        suppressed=False,
        chronic=False,
    )

    assert "first_seen" in rendered
    assert rendered["first_seen"] is None


def test_overview_deduplicates_suppression_present_in_timeline_and_live(tmp_path):
    now = datetime.now(timezone.utc)
    winner = _entry_event(
        "demo", "zookeeper_server_process", host_raw="node1",
        service="ZOOKEEPER", severity="critical",
    )
    loser = _entry_event(
        "prometheus", "zookeeper_port", host_raw="node1:2181",
        service="blackbox", severity="warning",
    )
    state_dir = tmp_path / "state"
    state = AlertsState(state_dir / "alerts.json")
    state.transition([winner, loser], now - timedelta(minutes=30))
    state.save()
    project(state_dir / "timeline.db", [{
        "ts": (now - timedelta(minutes=30)).isoformat(),
        "type": "new", "key": loser.key, "detail": {},
        "suppressed_by": winner.key,
    }])
    config = tmp_path / "config"
    config.mkdir()
    config.joinpath("alert-dedup.yaml").write_text(yaml.safe_dump({
        "version": 1,
        "rules": [{
            "match": {
                "demo": {
                    "platform": "alpha",
                    "alertname": "zookeeper_server_process",
                },
                "prometheus": {"port": 2181},
            },
            "winner": "demo",
            "reason": "service context",
        }],
    }), encoding="utf-8")

    body = TestClient(create_app(
        state_dir=state_dir, repo_root=tmp_path,
    )).get("/api/overview").json()

    assert body["stats"]["suppressed_today"] == 1


def test_overview_rejects_unknown_range(tmp_path):
    response = TestClient(create_app(
        state_dir=tmp_path / "state", repo_root=tmp_path,
    )).get("/api/overview", params={"range": "1h"})

    assert response.status_code == 422


def test_overview_query_count_is_bounded_for_5201_events(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    # Keep the synthetic events inside the requested window as time advances.
    started = datetime.now(timezone.utc) - timedelta(days=1)
    project(state_dir / "timeline.db", [
        {
            "ts": (started + timedelta(seconds=index)).isoformat(),
            "type": "new",
            "key": f"prometheus:alpha:overview_{index}:node{index}",
            "detail": {},
        }
        for index in range(5201)
    ])
    selects = []
    original_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connection.set_trace_callback(
            lambda statement: selects.append(statement)
            if statement.lstrip().upper().startswith(("SELECT", "WITH")) else None
        )
        return connection

    monkeypatch.setattr(timeline_module.sqlite3, "connect", traced_connect)
    monkeypatch.setattr(timeline_reader.sqlite3, "connect", traced_connect)

    response = TestClient(create_app(
        state_dir=state_dir, repo_root=tmp_path,
    )).get("/api/overview", params={"range": "30d"})

    assert response.status_code == 200
    assert len(response.json()["changes"]["items"]) == 10
    assert len(selects) <= 8


def test_overview_changes_only_include_window_events_and_fold_by_key(tmp_path):
    now = datetime.now(timezone.utc)
    state_dir = tmp_path / "state"
    state = AlertsState(state_dir / "alerts.json")
    chronic_without_events = [
        _entry_event(
            "demo", f"long_running_{index}", host_raw=f"node{index}",
            service="HDFS", severity="warning",
        )
        for index in range(1, 4)
    ]
    state.transition(chronic_without_events, now - timedelta(hours=48))
    state.save()

    flapping = "prometheus:alpha:flapping:node8"
    recovered = "demo:alpha:recovered:node9"
    escalated = "demo:alpha:escalated:node10"
    rows = [
        {
            "ts": (now - timedelta(minutes=20 - index)).isoformat(),
            "type": "new" if index % 2 == 0 else "resolved",
            "key": flapping,
            "source": "prometheus", "platform": "alpha", "alertname": "flapping",
            "host": "node8", "detail": {}, "severity": "warning", "service": "HDFS",
        }
        for index in range(8)
    ]
    rows.extend([
        {
            "ts": (now - timedelta(minutes=10)).isoformat(), "type": "resolved",
            "key": recovered, "detail": {},
        },
        {
            "ts": (now - timedelta(minutes=5)).isoformat(), "type": "escalated",
            "key": escalated, "detail": {},
        },
    ])
    project(state_dir / "timeline.db", rows)

    body = TestClient(create_app(
        state_dir=state_dir, repo_root=tmp_path,
    )).get("/api/overview").json()

    assert body["stats"]["chronic"] == 3
    assert body["changes"]["total"] == 3
    changes = {item["key"]: item for item in body["changes"]["items"]}
    assert set(changes) == {flapping, recovered, escalated}
    assert changes[flapping]["change_count"] == 8
    assert changes[flapping]["occurrence_count"] == 4
    assert changes[flapping]["change_type"] == "resolved"
    assert changes[recovered]["status"] == "resolved"
    assert changes[recovered]["occurrence_count"] == 0
    assert changes[escalated]["occurrence_count"] == 0
    assert all(item["chronic"] is False for item in changes.values())


def test_overview_change_window_follows_requested_range(tmp_path):
    now = datetime.now(timezone.utc)
    state_dir = tmp_path / "state"
    key = "demo:alpha:three_days_ago:node1"
    project(state_dir / "timeline.db", [{
        "ts": (now - timedelta(days=3)).isoformat(),
        "type": "new", "key": key, "detail": {},
    }])
    client = TestClient(create_app(state_dir=state_dir, repo_root=tmp_path))

    assert client.get("/api/overview", params={"range": "24h"}).json()["changes"]["items"] == []
    assert [
        item["key"]
        for item in client.get("/api/overview", params={"range": "7d"}).json()["changes"]["items"]
    ] == [key]


def test_overview_chronic_threshold_boundary_and_app_override(tmp_path):
    now = datetime.now(timezone.utc)
    state_dir = tmp_path / "state"
    entries = [
        _entry_event(
            "demo", alertname, host_raw=f"node{index}", service="HDFS",
            severity="warning",
        )
        for index, alertname in enumerate(("boundary", "under", "missing"), start=1)
    ]
    state = AlertsState(state_dir / "alerts.json")
    state.transition(entries, now)
    state.active[entries[0].key]["first_seen"] = (now - timedelta(hours=24)).isoformat()
    state.active[entries[1].key]["first_seen"] = (now - timedelta(hours=23, minutes=59)).isoformat()
    state.active[entries[2].key].pop("first_seen", None)
    state.save()

    default_body = TestClient(create_app(
        state_dir=state_dir, repo_root=tmp_path,
    )).get("/api/overview").json()
    override_body = TestClient(create_app(
        state_dir=state_dir, repo_root=tmp_path, chronic_hours=1,
    )).get("/api/overview").json()

    assert default_body["stats"]["chronic"] == 1
    assert override_body["stats"]["chronic"] == 2


def test_overview_class_stats_use_effective_class_and_keep_legacy_counts(tmp_path):
    now = datetime.now(timezone.utc)
    state_dir = tmp_path / "state"
    watch, chronic, muted_chronic = [
        _entry_event(
            "prometheus", name, host_raw=f"node{index}", service="HDFS",
            severity="warning",
        )
        for index, name in enumerate(("watch", "chronic", "muted_chronic"))
    ]
    state = AlertsState(state_dir / "alerts.json")
    state.transition([watch, chronic, muted_chronic], now - timedelta(hours=25))
    state.active[watch.key]["class"] = "watch"
    state.active[chronic.key]["class"] = "chronic"
    state.active[muted_chronic.key]["class"] = "chronic"
    state.active[muted_chronic.key]["muted"] = True
    state.save()

    stats = TestClient(create_app(
        state_dir=state_dir, repo_root=tmp_path,
    )).get("/api/overview").json()["stats"]

    assert stats["chronic_class"] == 1
    assert stats["active_non_chronic_class"] == 2
    assert stats["ignore_class"] == 1
    assert stats["active"] == 3
    assert stats["chronic"] == 3
    assert stats["active_non_chronic"] == 0


def test_overview_changes_expose_muted_for_shared_tag_rendering(tmp_path):
    now = datetime.now(timezone.utc)
    state_dir = tmp_path / "state"
    event = _entry_event(
        "prometheus", "muted_flap", host_raw="node8",
        service="HDFS", severity="warning",
    )
    state = AlertsState(state_dir / "alerts.json")
    state.transition([event], now - timedelta(minutes=2))
    state.active[event.key]["class"] = "flapping"
    state.active[event.key]["muted"] = True
    state.save()
    project(state_dir / "timeline.db", [{
        "ts": (now - timedelta(minutes=2)).isoformat(),
        "type": "new",
        "key": event.key,
        "source": event.source,
        "platform": event.platform,
        "alertname": event.alertname,
        "host": event.host,
        "class": "flapping",
        "detail": {},
        "severity": event.severity,
        "service": event.service,
    }])

    item = TestClient(create_app(
        state_dir=state_dir, repo_root=tmp_path,
    )).get("/api/overview").json()["changes"]["items"][0]

    assert item["muted"] is True
    assert item["effective_class"] == "ignore"


def test_overview_corrupt_timeline_degrades_changes(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_dir.joinpath("timeline.db").write_bytes(b"not sqlite")

    response = TestClient(create_app(
        state_dir=state_dir, repo_root=tmp_path,
    )).get("/api/overview")

    assert response.status_code == 200
    assert response.json()["changes"] == {"range": "24h", "total": 0, "items": []}
