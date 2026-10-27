from datetime import datetime, timedelta, timezone

from lib.alerts.model import AlertEvent
from lib.alerts.profiles import ProfilesConfig
from lib.alerts.state import AlertsState
from scripts.alert_poll import _process_tick


NOW = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)


class CapturingNotifier:
    def __init__(self):
        self.blocks = []

    def dispatch(self, blocks):
        self.blocks.extend(blocks)

    def flush_queue(self):
        pass


def event(
    *,
    host="node1",
    state="firing",
    started_at=None,
    resolved_at=None,
    name="ShortWatch",
):
    return AlertEvent(
        source="prometheus",
        platform="alpha",
        alertname=name,
        severity="warning",
        host=host,
        host_raw=host,
        service="HDFS",
        state=state,
        detail="short watch",
        source_active_at=(started_at or NOW - timedelta(minutes=3)).isoformat(),
        source_resolved_at=resolved_at.isoformat() if resolved_at else None,
    )


def profiles(*, rows=None):
    return ProfilesConfig(
        version=1,
        default_class="watch",
        class_config={
            "watch": {"min_duration_seconds": 0},
            "chronic": {},
            "flapping": {"flap_max_seconds": 300},
        },
        profiles=list(rows or []),
    )


def tick(tmp_path, state, events, *, now=NOW, profile=None):
    notifier = CapturingNotifier()
    counts, actions, rows = _process_tick(
        events,
        [],
        {"prometheus"},
        state,
        profile or profiles(),
        {},
        {"poll": {"auto_triage_enabled": False}},
        now,
        notifier=notifier,
        dry_run=False,
        records_alerts_root=tmp_path / "timeline",
    )
    return counts, actions, rows, notifier


def test_closed_segment_within_threshold_is_flapping_and_silent(tmp_path):
    state = AlertsState(tmp_path / "alerts.json")
    started_at = NOW - timedelta(minutes=3)
    firing = event(started_at=started_at)
    resolved = event(
        state="ok",
        started_at=started_at,
        resolved_at=NOW,
    )

    counts, _actions, rows, notifier = tick(
        tmp_path, state, [firing, resolved],
    )

    lifecycle = [row for row in rows if row["type"] in {"new", "resolved"}]
    assert [row["class"] for row in lifecycle] == ["flapping", "flapping"]
    assert counts["notified"] == 0
    assert notifier.blocks == []


def test_active_segment_within_threshold_is_flapping(tmp_path):
    state = AlertsState(tmp_path / "alerts.json")
    firing = event(started_at=NOW - timedelta(minutes=3))

    counts, _actions, rows, notifier = tick(tmp_path, state, [firing])

    entry = state.active[firing.key]
    assert entry["class"] == "flapping"
    assert entry["class_reason"] == "auto_flapping:duration"
    assert entry["triage"] == {
        "status": "not_applicable",
        "attempts": 0,
        "last_attempt_at": None,
        "run_id": None,
        "defer_reason": "flapping",
    }
    assert next(row for row in rows if row["type"] == "new")["class"] == "flapping"
    assert counts["notified"] == 0
    assert notifier.blocks == []


def test_sustained_segment_escalates_once_to_watch(tmp_path):
    state = AlertsState(tmp_path / "alerts.json")
    started_at = NOW - timedelta(minutes=3)
    firing = event(started_at=started_at)
    tick(tmp_path, state, [firing])

    later = NOW + timedelta(minutes=3)
    _counts, _actions, rows, notifier = tick(
        tmp_path,
        state,
        [event(started_at=started_at)],
        now=later,
    )

    assert state.active[firing.key]["class"] == "watch"
    escalations = [
        row for row in rows
        if row["type"] == "escalated"
        and row["detail"].get("from") == "flapping"
    ]
    assert len(escalations) == 1
    assert len(notifier.blocks) == 1

    _counts, _actions, steady_rows, steady_notifier = tick(
        tmp_path,
        state,
        [event(started_at=started_at)],
        now=later + timedelta(minutes=1),
    )
    assert not any(
        row["type"] == "escalated"
        and isinstance(row["detail"], dict)
        and row["detail"].get("from") == "flapping"
        for row in steady_rows
    )
    assert steady_notifier.blocks == []


def test_flap_duration_is_calculated_per_node_key(tmp_path):
    state = AlertsState(tmp_path / "alerts.json")
    node1 = event(host="node1", started_at=NOW - timedelta(minutes=3))
    node2 = event(host="node2", started_at=NOW - timedelta(seconds=301))

    tick(tmp_path, state, [node1, node2])

    assert state.active[node1.key]["class"] == "flapping"
    assert state.active[node2.key]["class"] == "watch"


def test_static_non_watch_classes_keep_existing_semantics(tmp_path):
    rows = [
        {"match_name": "Ignored", "class": "ignore"},
        {"match_name": "KnownFlap", "class": "flapping"},
        {"match_name": "KnownChronic", "class": "chronic"},
    ]
    state = AlertsState(tmp_path / "alerts.json")
    events = [
        event(name="Ignored"),
        event(name="KnownFlap"),
        event(name="KnownChronic"),
    ]

    tick(tmp_path, state, events, profile=profiles(rows=rows))

    assert state.active[events[0].key]["class"] == "ignore"
    assert state.active[events[1].key]["class"] == "flapping"
    assert state.active[events[2].key]["class"] == "chronic"
