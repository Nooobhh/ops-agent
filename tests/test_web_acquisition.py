import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from lib.acquisition.settings import load_settings
from lib.acquisition.store import append_snapshot
from web.server.app import create_app


def test_acquisition_settings_roundtrip_and_resource_latest(tmp_path):
    state = tmp_path / "state"
    root = tmp_path / "records" / "telemetry" / "resources"
    settings = load_settings(state)
    append_snapshot(root, {
        "schema_version": 1,
        "snapshot_id": "snap-hd",
        "scheduled_at": "2026-08-28T08:00:00+08:00",
        "collected_at": "2026-08-28T08:00:01+08:00",
        "platform": "alpha",
        "status": "ok",
        "coverage": {"expected_nodes": 17, "observed_nodes": 17,
                     "expected_services": 2, "observed_services": 1},
        "nodes": [], "services": [], "missing": [], "errors": [],
    })
    client = TestClient(create_app(state_dir=state, repo_root=tmp_path))

    response = client.get("/api/acquisition")
    assert response.status_code == 200
    assert response.json()["settings"] == settings
    assert response.json()["storage"]["resources"]["file_count"] == 1

    settings["resources"]["retention_days"] = 120
    saved = client.put("/api/acquisition/settings", json=settings)
    assert saved.status_code == 200
    assert saved.json()["settings"]["resources"]["retention_days"] == 120
    assert state.joinpath("acquisition-settings.json").is_file()

    latest = client.get("/api/resources/latest")
    assert latest.status_code == 200
    assert latest.json()["items"][0]["snapshot_id"] == "snap-hd"

    summary = client.get("/api/resources/summary")
    assert summary.status_code == 200
    assert summary.json()["stats"] == {
        "expected_nodes": 17,
        "observed_nodes": 17,
        "expected_services": 2,
        "observed_services": 1,
        "local_mounts": 0,
        "risk_count": 0,
        "snapshot_count": 0,
    }
    assert summary.json()["platforms"][0]["platform"] == "alpha"


def test_acquisition_rejects_unsafe_interval(tmp_path):
    client = TestClient(create_app(
        state_dir=tmp_path / "state", repo_root=tmp_path,
    ))
    settings = load_settings(tmp_path / "state")
    settings["alerts"]["interval_seconds"] = 10

    response = client.put("/api/acquisition/settings", json=settings)

    assert response.status_code == 422


def test_resource_endpoints_accept_dynamically_discovered_platform(tmp_path):
    state = tmp_path / "state"
    root = tmp_path / "records" / "telemetry" / "resources"
    recent = datetime.now(timezone.utc).isoformat()
    append_snapshot(root, {
        "schema_version": 1,
        "snapshot_id": "snap-mysql",
        "collected_at": recent,
        "platform": "mysql",
        "status": "ok",
        "coverage": {"expected_nodes": 3, "observed_nodes": 3,
                     "expected_services": 0, "observed_services": 0},
        "nodes": [], "services": [], "missing": [], "errors": [],
        "business_metrics": [{
            "platform": "mysql", "node": "192.0.2.113", "service": "MySQL",
            "job": "mysql_exporter1", "metric": "replication_lag_seconds",
            "label": "主从延迟", "unit": "seconds", "value": 38,
            "status": "ok", "severity": "critical",
            "sample_at": "2026-08-31T07:00:00+00:00",
            "details": {"master_host": "192.0.2.111"},
        }],
    })
    client = TestClient(create_app(state_dir=state, repo_root=tmp_path))

    latest = client.get("/api/resources/latest?platform=mysql")
    history = client.get("/api/resources/history?platform=mysql&hours=24")

    assert latest.status_code == 200
    assert latest.json()["items"][0]["platform"] == "mysql"
    assert history.status_code == 200
    assert history.json()["platform"] == "mysql"
    assert history.json()["points"][0]["business_metrics"][0]["value"] == 38


def test_excluded_platform_is_hidden_from_latest_summary_and_risks(tmp_path):
    state = tmp_path / "state"
    root = tmp_path / "records" / "telemetry" / "resources"
    config = tmp_path / "config"
    config.mkdir()
    config.joinpath("resource-metrics.yaml").write_text(
        "node:\n  excluded_platforms: [ai]\n", encoding="utf-8",
    )
    for platform in ("infra", "alpha"):
        append_snapshot(root, {
            "schema_version": 1, "snapshot_id": f"snap-{platform}",
            "collected_at": "2026-08-31T15:00:00+08:00", "platform": platform,
            "status": "ok", "coverage": {
                "expected_nodes": 1, "observed_nodes": 1,
                "expected_services": 0, "observed_services": 0,
            }, "nodes": [], "services": [], "missing": [], "errors": [],
        })
    state.mkdir(parents=True)
    state.joinpath("resource-risks.json").write_text(json.dumps({
        "snapshot_count": 2,
        "findings": [{"platform": "infra", "severity": "warning"}],
        "forecast_status": [{"platform": "infra"}, {"platform": "alpha"}],
    }), encoding="utf-8")
    client = TestClient(create_app(state_dir=state, repo_root=tmp_path))

    latest = client.get("/api/resources/latest").json()
    summary = client.get("/api/resources/summary").json()
    risks = client.get("/api/resource-risks").json()

    assert [row["platform"] for row in latest["items"]] == ["alpha"]
    assert [row["platform"] for row in summary["platforms"]] == ["alpha"]
    assert risks["findings"] == []
    assert [row["platform"] for row in risks["forecast_status"]] == ["alpha"]


def test_resource_alert_settings_roundtrip(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    config.joinpath("resource-metrics.yaml").write_text(
        "risk_defaults:\n  cpu: {warning_pct: 80, critical_pct: 90}\n",
        encoding="utf-8",
    )
    client = TestClient(create_app(
        state_dir=tmp_path / "state", repo_root=tmp_path,
    ))

    initial = client.get("/api/resource-alert-settings")
    assert initial.status_code == 200
    cpu = next(item for item in initial.json()["groups"] if item["key"] == "cpu")
    assert next(item for item in cpu["fields"] if item["key"] == "warning_pct")["default"] == 80

    saved = client.put("/api/resource-alert-settings", json={
        "schema_version": 1,
        "overrides": {"risk_defaults": {"cpu": {"warning_pct": 75}}},
    })
    assert saved.status_code == 200
    cpu = next(item for item in saved.json()["groups"] if item["key"] == "cpu")
    warning = next(item for item in cpu["fields"] if item["key"] == "warning_pct")
    assert warning["override"] == 75
    assert warning["effective"] == 75

    invalid = client.put("/api/resource-alert-settings", json={
        "schema_version": 1,
        "overrides": {"risk_defaults": {"cpu": {"warning_pct": 101}}},
    })
    assert invalid.status_code == 422


def test_resource_latest_and_risks_carry_derived_host_ref(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "platform-alpha.yaml").write_text(
        "platform: alpha\nnodes:\n  node3: {host: 198.51.100.103}\n", encoding="utf-8",
    )
    state = tmp_path / "state"
    append_snapshot(tmp_path / "records" / "telemetry" / "resources", {
        "schema_version": 1, "snapshot_id": "snap-hd",
        "scheduled_at": "2026-08-28T08:00:00+08:00", "collected_at": "2026-08-28T08:00:01+08:00",
        "platform": "alpha", "status": "ok", "coverage": {},
        "nodes": [{"platform": "alpha", "node": "node3"}, {"platform": "alpha", "node": "203.0.113.99"}],
        "services": [{"platform": "alpha", "node": "node3", "service": "hdfs"}],
        "missing": [], "errors": [],
    })
    state.mkdir(parents=True, exist_ok=True)
    state.joinpath("resource-risks.json").write_text(json.dumps({
        "schema_version": 1, "findings": [{"key": "k", "platform": "alpha", "node": "node3"}],
    }), encoding="utf-8")
    client = TestClient(create_app(state_dir=state, repo_root=tmp_path))

    item = client.get("/api/resources/latest").json()["items"][0]
    assert [row["host_ref"] for row in item["nodes"]] == ["example/alpha/host/node3", None]
    assert item["services"][0]["host_ref"] == "example/alpha/host/node3"
    finding = client.get("/api/resource-risks").json()["findings"][0]
    assert finding["host_ref"] == "example/alpha/host/node3"
