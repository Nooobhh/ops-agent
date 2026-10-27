import json
from datetime import datetime, timedelta, timezone

from lib.alerts.model import AlertEvent
from lib.alerts.state import AlertsState
from lib.alerts.timeline import rebuild
from web.server.data.consistency import check


def _alert(name):
    return AlertEvent(
        source="prometheus", platform="alpha", alertname=name, severity="warning",
        host="node1", host_raw="node1", service="system", state="firing", detail={},
    )


def _fixture(tmp_path, *, state_events, timeline_events, ts="2026-07-20T00:00:00+00:00"):
    state_dir = tmp_path / "state"
    state = AlertsState(state_dir / "alerts.json")
    state.transition(state_events, datetime.fromisoformat(ts))
    state.save()
    jsonl = tmp_path / "jsonl"
    jsonl.mkdir()
    rows = [{
        "ts": ts,
        "type": "new",
        "key": event.key,
        "source": event.source,
        "platform": event.platform,
        "alertname": event.alertname,
        "host": event.host,
        "class": "watch",
        "detail": {},
    } for event in timeline_events]
    jsonl.joinpath("events.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8",
    )
    db = state_dir / "timeline.db"
    rebuild(db, jsonl)
    return state_dir, db


def test_consistency_is_ok_when_active_sets_match(tmp_path):
    event = _alert("disk_full")
    state_dir, db = _fixture(
        tmp_path, state_events=[event], timeline_events=[event],
    )

    assert check(state_dir, db) == {"ok": True, "detail": None}


def test_consistency_reports_state_only_key_and_direction(tmp_path):
    event = _alert("disk_full")
    state_dir, db = _fixture(
        tmp_path, state_events=[event], timeline_events=[],
    )

    result = check(
        state_dir, db,
        now=datetime(2026, 7, 20, 1, 0, tzinfo=timezone.utc),
    )

    assert result["ok"] is False
    assert result["level"] == "warning"
    assert event.key in result["detail"]
    assert "状态机有而时间线无" in result["detail"]


def test_consistency_does_not_use_event_recency(tmp_path):
    event = _alert("old_but_open")
    state_dir, db = _fixture(
        tmp_path, state_events=[event], timeline_events=[event],
        ts="2026-07-19T01:00:00+00:00",
    )

    assert check(state_dir, db) == {"ok": True, "detail": None}


def test_consistency_downgrades_stale_unclosed_timeline_key_to_info(tmp_path):
    event = _alert("stale_unclosed")
    state_dir, db = _fixture(
        tmp_path, state_events=[], timeline_events=[event],
        ts="2026-01-01T00:00:00+00:00",
    )

    result = check(
        state_dir, db,
        now=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )

    assert result["ok"] is False
    assert result["level"] == "info"
    assert "陈旧" in result["detail"]
    assert event.key in result["detail"]


def test_consistency_uses_24_hour_recent_window(tmp_path):
    event = _alert("window_boundary")
    state_dir, db = _fixture(
        tmp_path, state_events=[], timeline_events=[event],
        ts="2026-07-19T00:00:01+00:00",
    )

    recent = check(
        state_dir, db,
        now=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    stale = check(
        state_dir, db,
        now=datetime(2026, 7, 20, tzinfo=timezone.utc) + timedelta(seconds=2),
    )

    assert recent["level"] == "warning"
    assert stale["level"] == "info"
