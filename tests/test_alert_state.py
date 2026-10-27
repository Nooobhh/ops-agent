from datetime import datetime, timedelta, timezone
import json

from lib.alerts.model import AlertEvent
from lib.alerts.state import AlertsState, acquire_lock, release_lock


def make_event(name="cpu_hot", *, source="prometheus", host="node1", severity="warning"):
    return AlertEvent(
        source=source,
        platform="alpha",
        alertname=name,
        severity=severity,
        host=host,
        host_raw=host or "",
        service="system",
        state="firing",
        detail="",
    )


def test_transition_new_still_resolved_and_resurface_suppression(tmp_path):
    path = tmp_path / "alerts.json"
    state = AlertsState.load(path)
    now = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)
    event = make_event()

    delta = state.transition([event], now)
    assert [item.key for item in delta.new] == [event.key]
    assert delta.new[0].resurfaced is False
    assert state.active[event.key]["occurrences"] == 1

    delta = state.transition([event], now + timedelta(minutes=1))
    assert [item.key for item in delta.still] == [event.key]
    assert state.active[event.key]["occurrences"] == 2

    delta = state.transition([], now + timedelta(minutes=2))
    assert [item.key for item in delta.resolved] == [event.key]
    assert event.key not in state.active
    assert event.key in state.resolved_recent

    state.resolved_recent[event.key]["entry"]["notified_initial_at"] = "2026-07-16T08:00:30+00:00"
    state.resolved_recent[event.key]["entry"]["triage"] = {
        "status": "done",
        "attempts": 1,
        "last_attempt_at": "2026-07-16T08:00:30+00:00",
        "run_id": "al-20260716-0800-abcd",
    }
    delta = state.transition([event], now + timedelta(minutes=20))
    assert delta.new[0].resurfaced is True
    assert state.active[event.key]["notified_initial_at"] == "2026-07-16T08:00:30+00:00"
    assert state.active[event.key]["triage"]["status"] == "done"

    state.transition([], now + timedelta(minutes=21))
    delta = state.transition([event], now + timedelta(minutes=60))
    assert delta.new[0].resurfaced is False


def test_select_triage_batch_orders_pending_and_retries_failed_next_tick(tmp_path):
    state = AlertsState.load(tmp_path / "alerts.json")
    now = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)
    events = [make_event(f"alert_{i}") for i in range(9)]
    state.transition(events, now)

    assert state.select_triage_batch(3) == [event.key for event in events[:3]]

    key = events[0].key
    state.mark_triage_attempt(key, ok=False, err="boom")
    assert key not in state.select_triage_batch(9)

    state.transition(events, now + timedelta(minutes=5))
    assert key in state.select_triage_batch(9)
    state.mark_triage_attempt(key, ok=False, err="boom2")
    state.transition(events, now + timedelta(minutes=10))
    state.mark_triage_attempt(key, ok=False, err="boom3")

    assert state.active[key]["triage"]["attempts"] == 3
    assert state.active[key]["triage"]["status"] == "failed"
    assert key not in state.select_triage_batch(9)


def test_patrol_active_missing_once_is_not_resolved_but_expires_after_24h(tmp_path):
    state = AlertsState.load(tmp_path / "alerts.json")
    now = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)
    event = make_event("patrol:node:disk", source="patrol")
    state.transition([event], now)

    delta = state.transition([], now + timedelta(hours=1))
    assert not delta.resolved
    assert event.key in state.active

    state.transition([], now + timedelta(hours=25, minutes=1))
    assert event.key not in state.active
    assert event.key not in state.resolved_recent


def test_lock_fresh_existing_blocks_and_stale_is_taken(tmp_path):
    lock_path = tmp_path / "alert.lock"
    assert acquire_lock(lock_path) is True
    assert acquire_lock(lock_path) is False

    lock_path.write_text(
        json.dumps({
            "pid": 999999,
            "started_at": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
        }),
        encoding="utf-8",
    )
    assert acquire_lock(lock_path, stale_minutes=15) is True
    release_lock(lock_path)
    assert not lock_path.exists()


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "alerts.json"
    now = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)
    state = AlertsState.load(path)
    state.patrol_last_scanned = "patrol-1"
    state.source_fail["prometheus"] = 2
    state.transition([make_event()], now)
    state.save()

    loaded = AlertsState.load(path)

    assert loaded.version == state.version
    assert loaded.active == state.active
    assert loaded.resolved_recent == state.resolved_recent
    assert loaded.patrol_last_scanned == "patrol-1"
    assert loaded.source_fail == {"prometheus": 2}
