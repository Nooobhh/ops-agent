import json
from datetime import datetime, timezone

from lib.alerts.model import AlertEvent
from lib.alerts.state import AlertsState


def _event() -> AlertEvent:
    return AlertEvent(
        source="prometheus",
        platform="alpha",
        alertname="disk_full",
        severity="warning",
        host="hd01",
        host_raw="hd01:9100",
        service="node",
        state="firing",
        detail="disk usage high",
    )


def test_alertevent_new_fields_default_none():
    event = _event()

    assert event.source_last_seen_at is None
    assert event.source_resolved_at is None


def test_alertsstate_last_tick_ms_default_empty(tmp_path):
    path = tmp_path / "alerts.json"
    path.write_text(json.dumps({"version": 1, "active": {}}), encoding="utf-8")

    assert AlertsState.load(path).last_tick_ms == {}


def test_alertsstate_upgraded_at_default_none(tmp_path):
    path = tmp_path / "alerts.json"
    path.write_text(json.dumps({"version": 1, "active": {}}), encoding="utf-8")

    assert AlertsState.load(path).upgraded_at is None


def test_alertsstate_last_tick_at_default_none(tmp_path):
    path = tmp_path / "alerts.json"
    path.write_text(json.dumps({"version": 1, "active": {}}), encoding="utf-8")

    assert AlertsState.load(path).last_tick_at is None


def test_alertsstate_transition_records_actual_tick_time(tmp_path):
    state = AlertsState(tmp_path / "alerts.json")
    now = datetime(2026, 7, 24, 10, 36, tzinfo=timezone.utc)

    state.transition([_event()], now)

    assert state.last_tick_at == now.isoformat()


def test_alertsstate_to_dict_roundtrip(tmp_path):
    path = tmp_path / "alerts.json"
    state = AlertsState(path)
    state.last_tick_ms = {"prometheus": 1_721_816_100_000, "demo:alpha": 1_721_816_099_500}
    state.upgraded_at = "2026-07-24T10:35:00+00:00"
    state.last_tick_at = "2026-07-24T10:36:00+00:00"

    path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
    loaded = AlertsState.load(path)

    assert loaded.last_tick_ms == state.last_tick_ms
    assert loaded.upgraded_at == state.upgraded_at
    assert loaded.last_tick_at == state.last_tick_at


def test_dedup_window_seconds_not_in_to_dict(tmp_path):
    state = AlertsState(tmp_path / "alerts.json")
    state.dedup_window_seconds = 60

    assert "dedup_window_seconds" not in state.to_dict()


def test_legacy_state_load_no_exception(tmp_path):
    path = tmp_path / "alerts.json"
    legacy = {
        "version": 1,
        "active": {},
        "resolved_recent": {},
        "chronic_groups": {
            "prometheus:alpha:disk_full": {
                "notified_chronic_at": "manual",
                "chronic_host_history": ["hd01"],
            }
        },
        "patrol_last_scanned": "",
        "source_fail": {"prometheus": 1},
    }
    path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = AlertsState.load(path)

    assert loaded.chronic_groups == legacy["chronic_groups"]
    assert loaded.last_tick_ms == {}
    assert loaded.upgraded_at is None
    assert loaded.last_tick_at is None
