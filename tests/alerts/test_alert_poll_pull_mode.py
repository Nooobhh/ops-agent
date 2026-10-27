import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from lib.alerts import timeline
from lib.alerts.model import AlertEvent
from lib.alerts.sources import SourceError
from lib.alerts.state import AlertsState
from scripts import alert_poll


NOW = datetime(2026, 7, 24, 10, 35, tzinfo=timezone.utc)


def _profiles():
    return SimpleNamespace(
        default_class="watch",
        profiles=[],
        class_config={"watch": {"min_duration_seconds": 0}},
    )


def _event(
    *,
    source="prometheus",
    alertname="disk_full",
    host="hd01",
    state="firing",
    active_at="2026-07-24T10:32:00+00:00",
    resolved_at=None,
    host_raw=None,
):
    return AlertEvent(
        source=source,
        platform="alpha",
        alertname=alertname,
        severity="critical",
        host=host,
        host_raw=host_raw or host,
        service="HDFS",
        state=state,
        detail="disk high",
        source_active_at=active_at,
        source_last_seen_at=active_at if state == "firing" else None,
        source_resolved_at=resolved_at,
    )


def _process(events, state, tmp_path, monkeypatch, *, config=None, notifier=None):
    monkeypatch.setattr(alert_poll, "load_dedup_rules", lambda path: SimpleNamespace(rules=()))
    return alert_poll._process_tick(
        events,
        [],
        {_source_id(event) for event in events},
        state,
        _profiles(),
        {},
        config or {"poll": {"auto_triage_enabled": False}},
        NOW,
        notifier=notifier,
        dry_run=False,
        records_alerts_root=tmp_path / "records" / "alerts",
    )


def _source_id(event):
    return f"demo:{event.platform}" if event.source == "demo" else event.source


def _collect_config(now):
    return {
        "_tick_now": now,
        "sources": {
            "prometheus-central": {"type": "prometheus", "plugin": "prometheus"},
        },
    }


def test_last_tick_ms_advances_to_now_even_no_events(monkeypatch, tmp_path):
    class EmptySource:
        def __init__(self, *args, **kwargs):
            pass

        def fetch(self):
            return []

    monkeypatch.setattr(alert_poll, "PrometheusSource", EmptySource)
    monkeypatch.setattr(alert_poll, "_load_platforms", lambda: {})
    state = AlertsState(tmp_path / "alerts.json")

    alert_poll.collect_events(_collect_config(NOW), state)
    first = state.last_tick_ms["prometheus"]
    alert_poll.collect_events(_collect_config(NOW + timedelta(minutes=5)), state)

    assert first == int(NOW.timestamp() * 1000)
    assert state.last_tick_ms["prometheus"] == int(
        (NOW + timedelta(minutes=5)).timestamp() * 1000
    )


def test_last_tick_ms_stays_on_source_error(monkeypatch, tmp_path):
    class FailingSource:
        def __init__(self, *args, **kwargs):
            pass

        def fetch(self):
            raise SourceError("prometheus", "down")

    monkeypatch.setattr(alert_poll, "PrometheusSource", FailingSource)
    monkeypatch.setattr(alert_poll, "_load_platforms", lambda: {})
    state = AlertsState(tmp_path / "alerts.json")
    state.last_tick_ms["prometheus"] = 123_456

    _events, errors, _successes = alert_poll.collect_events(_collect_config(NOW), state)

    assert errors[0].source == "prometheus"
    assert state.last_tick_ms["prometheus"] == 123_456


def test_prometheus_overlap_query_start_calculation(monkeypatch, tmp_path):
    windows = []

    class EmptySource:
        def __init__(self, *args, **kwargs):
            windows.append((kwargs["query_start_ms"], kwargs["query_end_ms"]))

        def fetch(self):
            return []

    monkeypatch.setattr(alert_poll, "PrometheusSource", EmptySource)
    monkeypatch.setattr(alert_poll, "_load_platforms", lambda: {})
    state = AlertsState(tmp_path / "alerts.json")

    alert_poll.collect_events(_collect_config(NOW), state)
    alert_poll.collect_events(_collect_config(NOW + timedelta(minutes=5)), state)

    first_end = int(NOW.timestamp() * 1000)
    assert windows[1][0] == first_end - alert_poll.PROMETHEUS_OVERLAP_MS, "须盖住插件 2 分钟内仍算未收口的时段"
    assert windows[1][1] == int((NOW + timedelta(minutes=5)).timestamp() * 1000)


def test_overlap_duplicates_deduped_by_event_id(monkeypatch, tmp_path):
    firing = _event()
    resolved = _event(
        state="ok",
        resolved_at="2026-07-24T10:33:00+00:00",
    )
    state = AlertsState(tmp_path / "alerts.json")
    state.upgraded_at = "2026-07-24T10:00:00+00:00"
    db = tmp_path / "timeline.db"

    first_rows = _process([firing, resolved], state, tmp_path, monkeypatch)[2]
    second_rows = _process([firing, resolved], state, tmp_path, monkeypatch)[2]
    timeline.project(db, first_rows)
    timeline.project(db, second_rows)

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2


def test_upgraded_at_set_on_first_run(monkeypatch, tmp_path):
    state = AlertsState(tmp_path / "alerts.json")

    _process([], state, tmp_path, monkeypatch)

    assert state.upgraded_at == NOW.isoformat()


def test_upgraded_at_preserved_after_first_run(monkeypatch, tmp_path):
    state = AlertsState(tmp_path / "alerts.json")
    state.upgraded_at = "2026-07-24T09:00:00+00:00"

    _process([], state, tmp_path, monkeypatch)

    assert state.upgraded_at == "2026-07-24T09:00:00+00:00"


def test_old_active_event_skipped(monkeypatch, tmp_path):
    state = AlertsState(tmp_path / "alerts.json")
    old = _event(active_at="2026-07-24T09:00:00+00:00")
    state.transition([old], NOW - timedelta(minutes=10))
    state.upgraded_at = None

    rows = _process([old], state, tmp_path, monkeypatch)[2]

    assert not [row for row in rows if row["type"] in {"new", "resolved"}]


def test_new_event_after_upgrade_logged(monkeypatch, tmp_path):
    state = AlertsState(tmp_path / "alerts.json")
    state.upgraded_at = "2026-07-24T10:00:00+00:00"
    event = _event(active_at="2026-07-24T10:32:00+00:00")

    rows = _process([event], state, tmp_path, monkeypatch)[2]

    assert [(row["type"], row["key"]) for row in rows] == [("new", event.key)]


def test_event_log_ts_uses_source_active_at_for_new(monkeypatch, tmp_path):
    state = AlertsState(tmp_path / "alerts.json")
    event = _event(active_at="2026-07-24T10:32:00+00:00")

    rows = _process([event], state, tmp_path, monkeypatch)[2]

    assert next(row for row in rows if row["type"] == "new")["ts"] == event.source_active_at


def test_event_log_ts_uses_source_resolved_at_for_resolved(monkeypatch, tmp_path):
    state = AlertsState(tmp_path / "alerts.json")
    firing = _event()
    resolved = _event(
        state="ok",
        resolved_at="2026-07-24T10:33:00+00:00",
    )

    rows = _process([firing, resolved], state, tmp_path, monkeypatch)[2]

    assert next(row for row in rows if row["type"] == "resolved")["ts"] == \
        resolved.source_resolved_at


def test_multiple_segments_generate_multiple_new_resolved_pairs(monkeypatch, tmp_path):
    events = []
    for minute in (0, 1, 2):
        active = f"2026-07-24T10:3{minute}:00+00:00"
        resolved = f"2026-07-24T10:3{minute}:30+00:00"
        events.extend([
            _event(active_at=active),
            _event(state="ok", active_at=active, resolved_at=resolved),
        ])
    state = AlertsState(tmp_path / "alerts.json")

    rows = _process(events, state, tmp_path, monkeypatch)[2]

    assert [row["type"] for row in rows if row["type"] in {"new", "resolved"}] == [
        "new", "resolved", "new", "resolved", "new", "resolved",
    ]
    assert state.active == {}


def test_dedup_input_includes_ok_events(monkeypatch, tmp_path):
    captured = {}
    demo_firing = _event(
        source="demo",
        alertname="zookeeper_server_process",
        active_at="2026-07-24T10:32:08+00:00",
    )
    demo_ok = _event(
        source="demo",
        alertname="zookeeper_server_process",
        state="ok",
        active_at=demo_firing.source_active_at,
        resolved_at="2026-07-24T10:32:35+00:00",
    )
    prom_firing = _event(
        alertname="zookeeper_port",
        active_at="2026-07-24T10:32:00+00:00",
        host_raw="hd01:2181",
    )
    prom_ok = _event(
        alertname="zookeeper_port",
        state="ok",
        active_at=prom_firing.source_active_at,
        resolved_at="2026-07-24T10:32:30+00:00",
        host_raw="hd01:2181",
    )
    state = AlertsState(tmp_path / "alerts.json")

    def fake_apply(rules, entries, *, window_seconds):
        captured["entries"] = list(entries)
        captured["window_seconds"] = window_seconds
        return {prom_firing.key: demo_firing.key}

    monkeypatch.setattr(alert_poll, "apply_dedup", fake_apply)
    monkeypatch.setattr(alert_poll, "load_dedup_rules", lambda path: object())
    notifier = SimpleNamespace(
        blocks=[],
        dispatch=lambda blocks: notifier.blocks.extend(blocks),
        flush_queue=lambda: None,
    )

    rows = alert_poll._process_tick(
        [prom_firing, prom_ok, demo_firing, demo_ok],
        [],
        {"prometheus", "demo:alpha"},
        state,
        _profiles(),
        {},
        {"poll": {"auto_triage_enabled": False, "dedup_window_seconds": 60}},
        NOW,
        notifier=notifier,
        dry_run=False,
        records_alerts_root=tmp_path / "records" / "alerts",
    )[2]

    assert len(captured["entries"]) == 4
    assert {event.state for event in captured["entries"]} == {"firing", "ok"}
    assert captured["window_seconds"] == 60
    assert all(
        row.get("suppressed_by") == demo_firing.key
        for row in rows
        if row["key"] == prom_firing.key and row["type"] in {"new", "resolved"}
    )
    assert len(notifier.blocks) == 1


def test_dedup_window_from_config_not_state(monkeypatch, tmp_path):
    captured = {}
    state = AlertsState(tmp_path / "alerts.json")
    state.dedup_window_seconds = 999

    def fake_apply(rules, entries, *, window_seconds):
        captured["window_seconds"] = window_seconds
        return {}

    monkeypatch.setattr(alert_poll, "apply_dedup", fake_apply)
    monkeypatch.setattr(alert_poll, "load_dedup_rules", lambda path: object())

    alert_poll._process_tick(
        [_event()],
        [],
        {"prometheus"},
        state,
        _profiles(),
        {},
        {"poll": {"auto_triage_enabled": False, "dedup_window_seconds": 60}},
        NOW,
        notifier=None,
        dry_run=False,
        records_alerts_root=tmp_path / "records" / "alerts",
    )

    assert captured["window_seconds"] == 60
    assert "dedup_window_seconds" not in state.to_dict()
