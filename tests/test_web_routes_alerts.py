import json
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import yaml
import pytest
from fastapi.testclient import TestClient

from lib.alerts.model import AlertEvent
from lib.alerts import rule_overrides
from lib.alerts.state import AlertsState
from lib.alerts.timeline import project
from lib.alerts import timeline as timeline_module
from web.server.app import create_app
from web.server.data import timeline_reader
from tests.envpack_helpers import as_env_pack


def _event(source, platform, alertname, host, severity, service, *, host_raw=None):
    return AlertEvent(
        source=source, platform=platform, alertname=alertname, severity=severity,
        host=host, host_raw=host_raw or host, service=service,
        state="firing", detail={"summary": alertname},
    )


def _fixture(tmp_path):
    now = datetime.now(timezone.utc)
    winner = _event(
        "demo", "alpha", "zookeeper_server_process", "node1", "critical", "ZOOKEEPER",
    )
    loser = _event(
        "prometheus", "alpha", "zookeeper_port", "node1", "warning", "blackbox",
        host_raw="node1:2181",
    )
    state_dir = tmp_path / "state"
    state = AlertsState(state_dir / "alerts.json")
    state.transition([winner, loser], now - timedelta(hours=1))
    for entry in state.active.values():
        entry["class"] = "watch"
        entry["triage"] = {"status": "pending", "attempts": 0}
    state.save()

    historical_key = "demo:beta:datanode_down:node9"
    ghost_key = "prometheus:alpha:ghost_port:node1"
    started = now - timedelta(hours=1)
    project(state_dir / "timeline.db", [
        {
            "ts": started.isoformat(), "type": "new", "key": winner.key,
            "class": "watch", "detail": {}, "severity": "critical", "service": "ZOOKEEPER",
        },
        {
            "ts": started.isoformat(), "type": "new", "key": loser.key,
            "class": "watch", "detail": {}, "severity": "warning", "service": "blackbox",
            "suppressed_by": winner.key,
        },
        {
            "ts": (now - timedelta(days=2)).isoformat(), "type": "new", "key": historical_key,
            "class": "chronic", "detail": {}, "suppressed_by": ghost_key,
        },
        {
            "ts": (now - timedelta(days=1)).isoformat(), "type": "resolved", "key": historical_key,
            "class": "chronic", "detail": {},
        },
    ])

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
    build = tmp_path / "build"
    build.mkdir()
    build.joinpath("routing.json").write_text(json.dumps({
        "alerts": {"alpha": {"zookeeper_server_process": {
            "doc": "alpha/zookeeper/process.md", "playbook": "playbooks/alpha/check_zk.sh",
            "platform": "alpha", "service": "zookeeper",
        }}},
        "docs": [{"path": "alpha/zookeeper/process.md", "title": "ZooKeeper 进程异常"}],
    }, ensure_ascii=False), encoding="utf-8")
    build.joinpath("wiki.html").write_text("<html>wiki</html>", encoding="utf-8")
    # 知识页直读源文件,doc_url 的开关是文档在不在,不再是 wiki.html 构建过没有
    routed_doc = tmp_path / "knowledge" / "alpha" / "zookeeper" / "process.md"
    routed_doc.parent.mkdir(parents=True, exist_ok=True)
    routed_doc.write_text("# ZooKeeper 进程异常\n", encoding="utf-8")
    client = TestClient(create_app(state_dir=state_dir, repo_root=tmp_path))
    return client, now, winner, loser, historical_key, ghost_key


def test_alert_list_filters_every_dimension_and_paginates(tmp_path):
    client, now, winner, loser, historical_key, _ = _fixture(tmp_path)

    baseline = {
        item["key"]: item for item in client.get("/api/alerts").json()["items"]
    }

    def assert_items_unchanged(params, expected_keys):
        actual = {
            item["key"]: item
            for item in client.get("/api/alerts", params=params).json()["items"]
        }
        assert actual == {key: baseline[key] for key in expected_keys}

    assert_items_unchanged({"source": "demo"}, {winner.key, historical_key})
    assert_items_unchanged({"platform": "alpha"}, {winner.key, loser.key})
    assert_items_unchanged({"host": "node1"}, {winner.key, loser.key})
    assert_items_unchanged({"severity": "critical"}, {winner.key})
    assert_items_unchanged({"service": "blackbox"}, {loser.key})
    assert_items_unchanged({"status": "suppressed"}, {loser.key})
    assert_items_unchanged({"status": "resolved"}, {historical_key})
    assert_items_unchanged({
        "from": (now - timedelta(hours=2)).isoformat(), "to": now.isoformat(),
    }, {winner.key, loser.key})
    page = client.get("/api/alerts", params={"limit": 1, "offset": 1}).json()
    assert page["total"] == 3
    assert len(page["items"]) == 1
    assert page["limit"] == 1 and page["offset"] == 1


def test_alert_list_accepts_multiple_status_values(tmp_path):
    client, _, winner, loser, historical_key, _ = _fixture(tmp_path)

    response = client.get("/api/alerts", params={"status": "active,suppressed"})

    assert response.status_code == 200
    body = response.json()
    assert {item["key"] for item in body["items"]} == {winner.key, loser.key}
    assert {item["status"] for item in body["items"]} == {"active", "suppressed"}
    assert historical_key not in {item["key"] for item in body["items"]}
    assert set(body) == {"items", "total", "limit", "offset", "filter_notice"}


def test_alert_list_rejects_invalid_status_value(tmp_path):
    client, *_ = _fixture(tmp_path)

    response = client.get("/api/alerts", params={"status": "active,invalid"})

    assert response.status_code == 422


def test_alert_list_filters_active_and_history_by_first_seen(tmp_path):
    client, now, winner, loser, historical_key, _ = _fixture(tmp_path)
    recent_history_key = "prometheus:alpha:recently_resolved:node2"
    project(tmp_path / "state" / "timeline.db", [
        {
            "ts": (now - timedelta(minutes=90)).isoformat(),
            "type": "new",
            "key": recent_history_key,
            "detail": {},
        },
        {
            "ts": (now - timedelta(minutes=80)).isoformat(),
            "type": "resolved",
            "key": recent_history_key,
            "detail": {},
        },
    ])

    response = client.get("/api/alerts", params={
        "first_seen_from": (now - timedelta(hours=2)).isoformat(),
    })

    assert response.status_code == 200
    assert {item["key"] for item in response.json()["items"]} == {
        winner.key, loser.key, recent_history_key,
    }
    assert historical_key not in {
        item["key"] for item in response.json()["items"]
    }
    invalid = client.get("/api/alerts", params={"first_seen_from": "not-a-time"})
    assert invalid.status_code == 422


def test_alert_list_filters_alerts_suppressed_today(tmp_path):
    client, _, _, loser, historical_key, _ = _fixture(tmp_path)

    response = client.get("/api/alerts", params={"suppressed_today": "true"})

    assert response.status_code == 200
    keys = {item["key"] for item in response.json()["items"]}
    assert loser.key in keys
    assert historical_key not in keys


def test_alert_list_uses_offset_aware_operator_local_window(tmp_path):
    state_dir = tmp_path / "state"
    intended_key = "demo:alpha:intended_window:node1"
    shifted_key = "demo:alpha:shifted_window:node2"
    project(state_dir / "timeline.db", [
        {
            "ts": "2026-07-20T02:30:00+00:00",
            "type": "new", "key": intended_key, "detail": {},
        },
        {
            "ts": "2026-07-20T10:30:00+00:00",
            "type": "new", "key": shifted_key, "detail": {},
        },
    ])
    client = TestClient(create_app(state_dir=state_dir, repo_root=tmp_path))

    response = client.get("/api/alerts", params={
        "from": "2026-07-20T10:00:00+08:00",
        "to": "2026-07-20T11:00:00+08:00",
    })

    assert response.status_code == 200
    assert [item["key"] for item in response.json()["items"]] == [intended_key]


@pytest.mark.parametrize("parameter", ["from", "to"])
def test_alert_list_rejects_invalid_time_with_readable_400(tmp_path, parameter):
    state_dir = tmp_path / "state"
    project(state_dir / "timeline.db", [{
        "ts": "2026-07-20T02:30:00+00:00",
        "type": "new", "key": "demo:alpha:test:node1", "detail": {},
    }])
    client = TestClient(create_app(state_dir=state_dir, repo_root=tmp_path))

    response = client.get("/api/alerts", params={parameter: "garbage"})

    assert response.status_code == 400
    assert parameter in response.json()["detail"]
    assert "ISO 8601" in response.json()["detail"]


def test_alert_list_explains_historical_severity_service_limit(tmp_path):
    client, now, _, _, _, _ = _fixture(tmp_path)

    body = client.get("/api/alerts", params={"severity": "does-not-match"}).json()

    assert body["items"] == []
    assert body["filter_notice"] == (
        "上线前的历史事件未记录 severity 字段；这部分数据不会出现在结果中。"
    )
    ranged = client.get("/api/alerts", params={
        "severity": "does-not-match",
        "from": (now - timedelta(hours=2)).isoformat(),
        "to": now.isoformat(),
    }).json()
    assert ranged["filter_notice"] is None


def test_resolved_alerts_participate_in_severity_and_service_filters(tmp_path):
    state_dir = tmp_path / "state"
    key = "demo:alpha:namenode_down:node1"
    project(state_dir / "timeline.db", [
        {
            "ts": "2026-07-20T01:00:00+00:00", "type": "new", "key": key,
            "severity": "critical", "service": "hdfs", "detail": {},
        },
        {
            "ts": "2026-07-20T02:00:00+00:00", "type": "resolved", "key": key,
            "severity": "critical", "service": "hdfs", "detail": {},
        },
    ])
    client = TestClient(create_app(state_dir=state_dir, repo_root=tmp_path))

    for params in (
        {"severity": "critical"},
        {"service": "hdfs"},
        {"status": "resolved", "severity": "critical"},
    ):
        body = client.get("/api/alerts", params=params).json()
        assert body["total"] == 1
        assert [item["key"] for item in body["items"]] == [key]
        assert body["filter_notice"] is None


def test_alert_list_excludes_source_error_rows(tmp_path):
    state_dir = tmp_path / "state"
    key = "prometheus:alpha:disk_full:node1"
    project(state_dir / "timeline.db", [
        {
            "ts": "2026-07-20T01:00:00+00:00", "type": "source_error",
            "key": "prometheus", "detail": "connection refused",
        },
        {
            "ts": "2026-07-20T01:05:00+00:00", "type": "new",
            "key": key, "detail": {},
        },
        {
            "ts": "2026-07-20T01:10:00+00:00", "type": "resolved",
            "key": key, "detail": {},
        },
    ])

    body = TestClient(create_app(
        state_dir=state_dir, repo_root=tmp_path,
    )).get("/api/alerts").json()

    assert body["total"] == 1
    assert [item["key"] for item in body["items"]] == [key]


def test_alert_list_chronic_filters_are_complementary_and_stack_with_status(tmp_path):
    client, now, winner, loser, historical_key, _ = _fixture(tmp_path)
    state_path = tmp_path / "state" / "alerts.json"
    state = AlertsState.load(state_path)
    state.active[winner.key]["first_seen"] = (now - timedelta(hours=1)).isoformat()
    state.active[loser.key]["first_seen"] = (now - timedelta(hours=48)).isoformat()
    state.save()

    all_items = client.get("/api/alerts").json()["items"]
    only = client.get("/api/alerts", params={"chronic": "only"}).json()["items"]
    excluded = client.get("/api/alerts", params={"chronic": "exclude"}).json()["items"]

    assert {item["key"] for item in only} == {loser.key}
    assert {item["key"] for item in excluded} == {winner.key, historical_key}
    assert {item["key"] for item in only + excluded} == {
        item["key"] for item in all_items
    }
    assert all(item["chronic"] is True for item in only)
    assert all(item["chronic"] is False for item in excluded)
    assert client.get("/api/alerts", params={
        "chronic": "only", "status": "suppressed",
    }).json()["items"][0]["key"] == loser.key
    historical = next(item for item in all_items if item["key"] == historical_key)
    assert historical["class"] == "chronic"
    assert historical["chronic"] is False


def test_historical_first_seen_is_latest_new_but_occurrences_stay_cumulative(tmp_path):
    state_dir = tmp_path / "state"
    key = "demo:alpha:reappeared:node1"
    project(state_dir / "timeline.db", [
        {"ts": "2026-07-01T01:00:00+00:00", "type": "new", "key": key, "detail": {}},
        {"ts": "2026-07-01T02:00:00+00:00", "type": "resolved", "key": key, "detail": {}},
        {"ts": "2026-07-20T03:00:00+00:00", "type": "new", "key": key, "detail": {}},
        {"ts": "2026-07-20T04:00:00+00:00", "type": "resolved", "key": key, "detail": {}},
    ])

    item = TestClient(create_app(
        state_dir=state_dir, repo_root=tmp_path,
    )).get("/api/alerts").json()["items"][0]

    assert item["first_seen"] == "2026-07-20T03:00:00+00:00"
    assert item["occurrences"] == 2
    assert item["chronic"] is False


def test_active_first_seen_resets_to_current_open_interval(tmp_path):
    now = datetime.now(timezone.utc)
    state_dir = tmp_path / "state"
    event = _event("demo", "alpha", "flappy", "node1", "warning", "HDFS")
    state = AlertsState(state_dir / "alerts.json")
    state.transition([event], now - timedelta(days=3))
    state.save()
    reopened = (now - timedelta(minutes=30)).isoformat()
    project(state_dir / "timeline.db", [
        {"ts": (now - timedelta(days=3)).isoformat(), "type": "new", "key": event.key, "detail": {}},
        {"ts": (now - timedelta(hours=1)).isoformat(), "type": "resolved", "key": event.key, "detail": {}},
        {"ts": reopened, "type": "new", "key": event.key, "detail": {}},
    ])
    client = TestClient(create_app(state_dir=state_dir, repo_root=tmp_path))

    item = client.get("/api/alerts").json()["items"][0]
    detail = client.get(f"/api/alerts/{quote(event.key, safe='')}").json()

    assert item["status"] == "active"
    assert item["first_seen"] == reopened
    assert item["chronic"] is True
    assert detail["current"]["first_seen"] == reopened
    assert detail["current"]["occurrences"] == 2


def test_detail_open_interval_prefers_source_active_at_without_crossing(tmp_path):
    now = datetime.now(timezone.utc)
    state_dir = tmp_path / "state"
    fresh = _event("prometheus", "alpha", "fresh_fire", "node1", "critical", "HDFS")
    flappy = _event("prometheus", "alpha", "flappy_fire", "node2", "critical", "HDFS")
    state = AlertsState(state_dir / "alerts.json")
    state.transition([fresh, flappy], now - timedelta(minutes=10))
    source_time = (now - timedelta(hours=2)).isoformat()
    state.active[fresh.key]["event"]["source_active_at"] = source_time
    state.active[fresh.key]["event"]["detail"] = "进程端口探活失败"
    state.active[flappy.key]["event"]["source_active_at"] = (
        now - timedelta(hours=2)
    ).isoformat()
    state.save()
    tick_start = (now - timedelta(minutes=10)).isoformat()
    project(state_dir / "timeline.db", [
        {"ts": tick_start, "type": "new", "key": fresh.key, "detail": {}},
        {"ts": (now - timedelta(hours=3)).isoformat(), "type": "new", "key": flappy.key, "detail": {}},
        {"ts": (now - timedelta(hours=1)).isoformat(), "type": "resolved", "key": flappy.key, "detail": {}},
        {"ts": tick_start, "type": "new", "key": flappy.key, "detail": {}},
    ])
    client = TestClient(create_app(state_dir=state_dir, repo_root=tmp_path))

    fresh_body = client.get(f"/api/alerts/{quote(fresh.key, safe='')}").json()
    flappy_body = client.get(f"/api/alerts/{quote(flappy.key, safe='')}").json()

    assert fresh_body["description"] == "进程端口探活失败"
    assert fresh_body["current"]["source_active_at"] == source_time
    assert fresh_body["current"]["first_seen"] == source_time
    assert fresh_body["occurrences"][-1]["started_at"] == source_time
    assert fresh_body["occurrences"][-1]["started_from"] == "source"
    assert fresh_body["occurrences"][-1]["duration_seconds"] >= 7100
    # 源端时间早于上一区间的恢复点,视为不可信,保持轮询首见
    assert flappy_body["current"]["first_seen"] == tick_start
    assert flappy_body["occurrences"][-1]["started_at"] == tick_start
    assert "started_from" not in flappy_body["occurrences"][-1]


def test_resolved_detail_falls_back_to_timeline_description_and_source_time(tmp_path):
    now = datetime.now(timezone.utc)
    state_dir = tmp_path / "state"
    key = "demo:alpha:short_blip:node3"
    source_started = (now - timedelta(minutes=48, seconds=30)).isoformat()
    tick_started = (now - timedelta(minutes=45)).isoformat()
    resolved_at = (now - timedelta(minutes=40)).isoformat()
    project(state_dir / "timeline.db", [
        {
            "ts": tick_started, "type": "new", "key": key,
            "detail": {
                "resurfaced": False,
                "description": "DataNode 进程健康度低于阈值",
                "source_active_at": source_started,
            },
        },
        {"ts": resolved_at, "type": "resolved", "key": key, "detail": {}},
    ])

    body = TestClient(create_app(
        state_dir=state_dir, repo_root=tmp_path,
    )).get(f"/api/alerts/{quote(key, safe='')}").json()

    assert body["current"]["status"] == "resolved"
    assert body["description"] == "DataNode 进程健康度低于阈值"
    assert body["current"]["source_active_at"] == source_started
    assert body["current"]["first_seen"] == source_started
    assert body["occurrences"][0]["started_at"] == source_started
    assert body["occurrences"][0]["started_from"] == "source"
    assert body["occurrences"][0]["resolved_at"] == resolved_at


def test_alert_config_catalog_override_roundtrip_and_validation(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_dir.joinpath("alert-catalog.json").write_text(json.dumps({
        "version": 1,
        "updated_at": "2026-07-22T10:00:00+00:00",
        "sources": {
            "prometheus": {
                "updated_at": "2026-07-22T10:00:00+00:00",
                "alerts": [{
                    "alertname": "Disk Full", "severity": "critical",
                    "platform": "alpha", "class": "watch",
                }],
            },
            "demo:alpha": {
                "updated_at": None,
                "alerts": [{
                    "alertname": "NameNode Health", "label": "NameNode",
                    "enabled": True, "class": "chronic",
                }],
            },
            "mtail": {
                "updated_at": None,
                "alerts": [{"alertname": "Retired", "class": "watch"}],
            },
        },
    }), encoding="utf-8")
    client = TestClient(create_app(state_dir=state_dir, repo_root=tmp_path))

    initial = client.get("/api/alert-config")
    assert initial.status_code == 200
    body = initial.json()
    assert body["catalog_available"] is True
    assert body["sources"]["prometheus"]["alerts"][0] == {
        "alertname": "Disk Full", "severity": "critical", "platform": "alpha",
        "class": "watch", "notify": True, "severity_override": None,
    }
    assert body["sources"]["demo:alpha"]["alerts"][0]["class"] == "chronic"
    assert "mtail" not in body["sources"]

    url = "/api/alert-config/prometheus/" + quote("Disk Full", safe="")
    updated = client.post(url, json={"notify": False, "severity": "warning"})
    assert updated.status_code == 200
    assert updated.json()["notify"] is False
    assert updated.json()["severity"] == "warning"
    saved = json.loads(state_dir.joinpath("alert-rule-overrides.json").read_text())
    assert saved["overrides"]["prometheus:Disk Full"]["notify"] is False
    merged = client.get("/api/alert-config").json()["sources"]["prometheus"]["alerts"][0]
    assert merged["notify"] is False
    assert merged["severity_override"] == "warning"

    real_lock = rule_overrides.FileLock
    lock_acquisitions = []

    class CountingLock:
        def __init__(self, *args, **kwargs):
            self.delegate = real_lock(*args, **kwargs)

        def __enter__(self):
            lock_acquisitions.append(1)
            return self.delegate.__enter__()

        def __exit__(self, *args):
            return self.delegate.__exit__(*args)

    monkeypatch.setattr(rule_overrides, "FileLock", CountingLock)
    cleared = client.post(url, json={"severity": None})
    assert cleared.status_code == 200
    assert len(lock_acquisitions) == 1
    assert cleared.json()["notify"] is False
    assert cleared.json()["severity"] is None
    merged = client.get("/api/alert-config").json()["sources"]["prometheus"]["alerts"][0]
    assert merged["notify"] is False
    assert merged["severity_override"] is None

    demo_url = "/api/alert-config/" + quote("demo:alpha", safe="") + "/" + quote("NameNode Health", safe="")
    assert client.post(demo_url, json={"notify": False}).status_code == 200
    assert client.post("/api/alert-config/mtail/Retired", json={"notify": False}).status_code == 422
    assert client.post("/api/alert-config/invalid/Rule", json={"notify": False}).status_code == 422
    assert client.post(url, json={"severity": "fatal"}).status_code == 422


def test_alert_config_uses_latest_runtime_class_from_alert_state(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_dir.joinpath("alert-catalog.json").write_text(json.dumps({
        "version": 1,
        "sources": {
            "demo:alpha": {
                "updated_at": "2026-07-22T10:00:00+00:00",
                "alerts": [{
                    "alertname": "DataNode Health",
                    "class": "watch",
                    "platform": "alpha",
                }],
            },
        },
    }), encoding="utf-8")
    event = _event("demo", "alpha", "DataNode Health", "node1", "warning", "HDFS")
    state = AlertsState(state_dir / "alerts.json")
    state.transition([event], datetime(2026, 7, 22, 10, 1, tzinfo=timezone.utc))
    state.active[event.key]["class"] = "flapping"
    state.save()

    item = TestClient(create_app(state_dir=state_dir, repo_root=tmp_path)) \
        .get("/api/alert-config").json()["sources"]["demo:alpha"]["alerts"][0]

    assert item["class"] == "flapping"
    assert item["baseline_class"] == "watch"


def test_alert_config_missing_catalog_is_available_empty_state(tmp_path):
    as_env_pack(tmp_path)  # 告警源取环境包配置:临时仓不放即无来源
    response = TestClient(create_app(
        state_dir=tmp_path / "state", repo_root=tmp_path,
    )).get("/api/alert-config")

    assert response.status_code == 200
    assert response.json() == {"sources": {}, "catalog_available": False, "source_notify": {}, "sources_meta": []}


def test_active_alert_list_and_detail_include_muted_without_old_mark_endpoint(tmp_path):
    now = datetime.now(timezone.utc)
    state_dir = tmp_path / "state"
    event = _event("prometheus", "alpha", "muted", "node1", "warning", "HDFS")
    state = AlertsState(state_dir / "alerts.json")
    state.transition([event], now)
    state.active[event.key]["muted"] = True
    state.save()
    client = TestClient(create_app(state_dir=state_dir, repo_root=tmp_path))

    item = client.get("/api/alerts").json()["items"][0]
    detail = client.get(f"/api/alerts/{quote(event.key, safe='')}").json()

    assert item["muted"] is True
    assert "severity_override" not in item
    assert detail["current"]["muted"] is True
    assert "severity_override" not in detail["current"]
    assert client.post(
        f"/api/alerts/{quote(event.key, safe='')}/severity",
        json={"severity": "critical"},
    ).status_code == 404


def test_alert_detail_prefers_current_notify_override_over_entry_muted(tmp_path):
    now = datetime.now(timezone.utc)
    state_dir = tmp_path / "state"
    event = _event(
        "prometheus", "alpha", "override_muted", "node1", "warning", "HDFS",
    )
    state = AlertsState(state_dir / "alerts.json")
    state.transition([event], now)
    state.save()
    override_path = state_dir / "alert-rule-overrides.json"
    override_key = f"prometheus:{event.alertname}"
    client = TestClient(create_app(state_dir=state_dir, repo_root=tmp_path))

    override_path.write_text(json.dumps({
        "version": 1,
        "overrides": {override_key: {"notify": False}},
    }), encoding="utf-8")
    muted = client.get(f"/api/alerts/{quote(event.key, safe='')}").json()
    assert muted["current"]["muted"] is True

    state.active[event.key]["muted"] = True
    state.save()
    override_path.write_text(json.dumps({
        "version": 1,
        "overrides": {override_key: {"notify": True}},
    }), encoding="utf-8")
    unmuted = client.get(f"/api/alerts/{quote(event.key, safe='')}").json()
    assert unmuted["current"]["muted"] is False


def test_historical_pagination_uses_same_first_seen_order_as_python_merge(tmp_path):
    state_dir = tmp_path / "state"
    keys = {
        "a": "demo:alpha:latest_event_first:node1",
        "b": "demo:alpha:last_new_first:node2",
        "c": "demo:alpha:last_new_second:node3",
    }
    project(state_dir / "timeline.db", [
        {"ts": "2026-07-01T01:00:00+00:00", "type": "new", "key": keys["a"], "detail": {}},
        {"ts": "2026-07-06T00:00:00+00:00", "type": "resolved", "key": keys["a"], "detail": {}},
        {"ts": "2026-07-03T01:00:00+00:00", "type": "new", "key": keys["b"], "detail": {}},
        {"ts": "2026-07-04T00:00:00+00:00", "type": "resolved", "key": keys["b"], "detail": {}},
        {"ts": "2026-07-02T01:00:00+00:00", "type": "new", "key": keys["c"], "detail": {}},
        {"ts": "2026-07-05T00:00:00+00:00", "type": "resolved", "key": keys["c"], "detail": {}},
    ])
    client = TestClient(create_app(state_dir=state_dir, repo_root=tmp_path))

    full = client.get("/api/alerts").json()["items"]
    first_page = client.get("/api/alerts", params={"limit": 2, "offset": 0}).json()["items"]
    second_page = client.get("/api/alerts", params={"limit": 2, "offset": 2}).json()["items"]

    expected = [keys["b"], keys["c"], keys["a"]]
    assert [item["key"] for item in full] == expected
    assert [item["key"] for item in first_page + second_page] == expected


def test_active_and_historical_items_sort_together_by_first_seen(tmp_path):
    now = datetime.now(timezone.utc)
    state_dir = tmp_path / "state"
    active_event = _event("demo", "alpha", "active", "node1", "warning", "HDFS")
    state = AlertsState(state_dir / "alerts.json")
    state.transition([active_event], now - timedelta(days=1))
    state.save()
    newer_history = "demo:alpha:newer_history:node2"
    older_history = "demo:alpha:older_history:node3"
    project(state_dir / "timeline.db", [
        {"ts": (now - timedelta(hours=1)).isoformat(), "type": "new", "key": newer_history, "detail": {}},
        {"ts": now.isoformat(), "type": "resolved", "key": newer_history, "detail": {}},
        {"ts": (now - timedelta(days=2)).isoformat(), "type": "new", "key": older_history, "detail": {}},
        {"ts": (now - timedelta(hours=2)).isoformat(), "type": "resolved", "key": older_history, "detail": {}},
    ])

    items = TestClient(create_app(
        state_dir=state_dir, repo_root=tmp_path,
    )).get("/api/alerts").json()["items"]

    assert [item["key"] for item in items] == [
        newer_history, active_event.key, older_history,
    ]


def test_malformed_active_first_seen_is_sorted_last_without_breaking_list(tmp_path):
    now = datetime.now(timezone.utc)
    state_dir = tmp_path / "state"
    valid = _event("demo", "alpha", "valid_time", "node1", "warning", "HDFS")
    malformed = _event(
        "prometheus", "alpha", "malformed_time", "node2", "warning", "HDFS",
    )
    state = AlertsState(state_dir / "alerts.json")
    state.transition([valid, malformed], now - timedelta(hours=1))
    state.active[malformed.key]["first_seen"] = "not-an-iso-time"
    state.save()
    client = TestClient(
        create_app(state_dir=state_dir, repo_root=tmp_path),
        raise_server_exceptions=False,
    )

    response = client.get("/api/alerts")

    assert response.status_code == 200
    assert [item["key"] for item in response.json()["items"]] == [
        valid.key,
        malformed.key,
    ]


def test_alert_facets_union_active_and_recent_history_with_stable_sorting(tmp_path):
    now = datetime.now(timezone.utc)
    state_dir = tmp_path / "state"
    active_event = _event(
        "prometheus", "alpha", "active", "node1", "critical", "HDFS",
    )
    state = AlertsState(state_dir / "alerts.json")
    state.transition([active_event], now)
    state.save()
    project(state_dir / "timeline.db", [
        {
            "ts": (now - timedelta(days=2)).isoformat(), "type": "new",
            "key": "demo:beta:recent:node2", "source": "demo", "platform": "beta",
            "severity": "warning", "service": "YARN", "detail": {},
        },
        {
            "ts": (now - timedelta(days=3)).isoformat(), "type": "new",
            "key": "prometheus:alpha:info:node3", "source": "prometheus", "platform": "alpha",
            "severity": "info", "service": "HDFS", "detail": {},
        },
        {
            "ts": (now - timedelta(days=4)).isoformat(), "type": "new",
            "key": "custom:alpha:unknown:node4", "source": "custom", "platform": "alpha",
            "severity": "notice", "service": "  ", "detail": {},
        },
        {
            "ts": (now - timedelta(days=31)).isoformat(), "type": "new",
            "key": "legacy:old:stale:node5", "source": "legacy", "platform": "old",
            "severity": "stale", "service": "MAPREDUCE", "detail": {},
        },
        {
            "ts": (now - timedelta(days=1)).isoformat(), "type": "new",
            "key": "empty:alpha:empty:node6", "source": "", "platform": " ",
            "severity": None, "service": None, "detail": {},
        },
        {  # 源端时钟超前产生的未来事件不得贡献候选值
            "ts": (now + timedelta(days=3)).isoformat(), "type": "new",
            "key": "skewed:future:clock:node7", "source": "skewed", "platform": "future",
            "severity": "skewed", "service": "CLOCK", "detail": {},
        },
    ])
    client = TestClient(create_app(state_dir=state_dir, repo_root=tmp_path))

    response = client.get("/api/alerts/facets")

    assert response.status_code == 200
    assert response.json() == {
        "source": ["custom", "demo", "empty", "prometheus"],
        "platform": ["alpha", "beta"],
        "severity": ["critical", "warning", "info", "notice"],
        "service": ["HDFS", "YARN"],
    }
    assert "host" not in response.json()


def test_alert_facets_corrupt_timeline_keeps_active_values_and_wins_route(tmp_path):
    now = datetime.now(timezone.utc)
    state_dir = tmp_path / "state"
    active_event = _event(
        "prometheus", "alpha", "active", "node1", "critical", "HDFS",
    )
    state = AlertsState(state_dir / "alerts.json")
    state.transition([active_event], now)
    state.save()
    state_dir.joinpath("timeline.db").write_bytes(b"not sqlite")

    response = TestClient(create_app(
        state_dir=state_dir, repo_root=tmp_path,
    )).get("/api/alerts/facets")

    assert response.status_code == 200
    assert response.json() == {
        "source": ["prometheus"],
        "platform": ["alpha"],
        "severity": ["critical"],
        "service": ["HDFS"],
    }


def test_detail_open_occurrence_duration_payload_routing_and_dedup(tmp_path):
    client, _, winner, loser, _, _ = _fixture(tmp_path)

    response = client.get(f"/api/alerts/{quote(loser.key, safe='')}")

    assert response.status_code == 200
    body = response.json()
    assert body["current"]["status"] == "active"
    assert body["payload"] == {
        "available": True, "source_of": "state", "data": body["payload"]["data"],
    }
    assert body["occurrences"][-1]["open"] is True
    assert 3500 <= body["occurrences"][-1]["duration_seconds"] <= 3700
    assert body["dedup"]["live"]["suppressed_by"] == winner.key
    assert body["dedup"]["historical"]["incomplete"] is True
    assert body["routing"] is None
    assert [(peer["key"], peer["relation"]) for peer in body["peers"]] == [
        (winner.key, "suppressed_by"),
    ]
    assert body["peers"][0]["payload"]["alertname"] == "zookeeper_server_process"


def test_detail_peers_are_bidirectional(tmp_path):
    client, _, winner, loser, _, _ = _fixture(tmp_path)

    body = client.get(f"/api/alerts/{quote(winner.key, safe='')}").json()

    assert body["routing"]["doc_url"] == "/knowledge/alpha/zookeeper/process.md"
    assert [(peer["key"], peer["relation"]) for peer in body["peers"]] == [
        (loser.key, "suppresses"),
    ]


def test_historical_detail_keeps_fields_when_payload_and_peer_payload_are_missing(tmp_path):
    client, _, _, _, historical_key, ghost_key = _fixture(tmp_path)

    body = client.get(f"/api/alerts/{quote(historical_key, safe='')}").json()

    assert body["current"]["status"] == "resolved"
    assert body["payload"] == {"available": False, "source_of": None, "data": None}
    assert body["events"]
    assert body["occurrences"][0]["open"] is False
    assert body["dedup"]["historical"] == {
        "suppressed_by": ghost_key, "incomplete": True,
    }
    assert any(peer["key"] == ghost_key and peer["payload"] is None for peer in body["peers"])


def test_detail_returns_404_for_unknown_key(tmp_path):
    client, _, _, _, _, _ = _fixture(tmp_path)

    assert client.get("/api/alerts/missing%3Ahd%3Aalert%3Anode1").status_code == 404


def _trace_selects(monkeypatch):
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
    return selects


def test_alert_list_query_count_is_bounded_for_5201_filtered_events(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    started = datetime(2026, 7, 1, tzinfo=timezone.utc)
    project(state_dir / "timeline.db", [
        {
            "ts": (started + timedelta(seconds=index)).isoformat(),
            "type": "new",
            "key": f"prometheus:alpha:bulk_{index}:node{index}",
            "severity": "critical", "service": "hdfs",
            "detail": {},
        }
        for index in range(5201)
    ])
    selects = _trace_selects(monkeypatch)

    response = TestClient(create_app(
        state_dir=state_dir, repo_root=tmp_path,
    )).get("/api/alerts", params={"severity": "critical", "limit": 50})

    assert response.status_code == 200
    assert response.json()["total"] == 5201
    assert len(response.json()["items"]) == 50
    assert len(selects) <= 8


def test_alert_detail_batches_historical_peer_queries(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    winner = "demo:alpha:shared_failure:node1"
    rows = [{
        "ts": "2026-07-20T00:00:00+00:00",
        "type": "new", "key": winner, "detail": {},
    }]
    rows.extend({
        "ts": "2026-07-20T00:00:00+00:00",
        "type": "new",
        "key": f"prometheus:alpha:peer_{index}:node1",
        "suppressed_by": winner,
        "detail": {},
    } for index in range(100))
    project(state_dir / "timeline.db", rows)
    selects = _trace_selects(monkeypatch)

    response = TestClient(create_app(
        state_dir=state_dir, repo_root=tmp_path,
    )).get(f"/api/alerts/{quote(winner, safe='')}")

    assert response.status_code == 200
    assert len(response.json()["peers"]) == 100
    assert len(selects) <= 8


def test_alert_list_and_detail_carry_derived_host_ref(tmp_path):
    client, _now, winner, _loser, historical_key, _ = _fixture(tmp_path)
    # 清单按请求加载,fixture 建好 app 后再铺拓扑即可;临时仓兼作环境包,只登记 alpha
    as_env_pack(tmp_path)
    (tmp_path / "config" / "platform-alpha.yaml").write_text(
        "platform: alpha\nnodes:\n  node1: {host: 198.51.100.101}\n", encoding="utf-8",
    )

    items = {item["key"]: item for item in client.get("/api/alerts").json()["items"]}
    assert items[winner.key]["host_ref"] == "example/alpha/host/node1"
    assert items[historical_key]["host_ref"] is None      # beta 拓扑未登记 → 不臆造
    detail = client.get(f"/api/alerts/{quote(winner.key, safe='')}").json()
    assert detail["host_ref"] == "example/alpha/host/node1"


def test_alert_config_source_tabs_follow_alert_source_config(tmp_path):
    """来源 tab 随告警源配置:全局源为类型,按集群的源为「类型:集群」,标签缺省「类型-集群短名」。"""
    as_env_pack(tmp_path)
    (tmp_path / "config" / "alert-sources.yaml").write_text(
        "version: 1\nsources:\n"
        "  central: {type: prometheus, plugin: prometheus}\n"
        "  demo-hd: {type: demo, platform: alpha}\n"
        "  demo-iot: {type: demo, platform: beta, label: 演示 Beta}\n",
        encoding="utf-8",
    )
    body = TestClient(create_app(state_dir=tmp_path / "state", repo_root=tmp_path)).get("/api/alert-config").json()
    assert body["sources_meta"] == [
        {"id": "prometheus", "label": "Prometheus"},
        {"id": "demo:alpha", "label": "Demo-Alpha"},
        {"id": "demo:beta", "label": "演示 Beta"},
    ]
    assert set(body["source_notify"]) == {"prometheus", "demo:alpha", "demo:beta"}
