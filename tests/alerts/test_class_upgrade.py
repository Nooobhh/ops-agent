from datetime import datetime, timedelta, timezone

from lib.alerts.model import AlertEvent
from lib.alerts.profiles import ProfilesConfig
from lib.alerts.state import AlertsState
from scripts.alert_poll import _process_tick


NOW = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)


def event(name="LongWatch", host="nodeA"):
    return AlertEvent(
        source="prometheus", platform="alpha", alertname=name, severity="warning",
        host=host, host_raw=host, service="system", state="firing", detail="detail",
    )


def profiles(*, manual=False, ignored=False, flapping=False, baseline=None):
    rows = []
    if manual:
        row = {"match_name": "LongWatch", "class": "chronic", "reason": "manual"}
        if baseline is not None:
            row.update(baseline_host_count=baseline, escalate_host_delta=1)
        rows.append(row)
    if ignored:
        rows.append({"match_name": "LongWatch", "class": "ignore"})
    if flapping:
        rows.append({"match_name": "LongWatch", "class": "flapping"})
    return ProfilesConfig(
        version=1,
        default_class="watch",
        class_config={"watch": {"min_duration_seconds": 0}, "chronic": {}},
        profiles=rows,
    )


def tick(state, events, cfg=None, now=NOW, profile=None):
    return _process_tick(
        events, [], {"prometheus"}, state, profile or profiles(), {},
        cfg or {"poll": {"auto_triage_enabled": False}},
        now, notifier=None, dry_run=False, records_alerts_root=state.path.parent,
    )


def seed(state, events, first_seen):
    state.transition(events, first_seen)
    for item in state.active.values():
        item["class"] = "watch"
        item["class_reason"] = "default"


def test_watch_upgrades_to_chronic_after_24h(tmp_path):
    state = AlertsState(tmp_path / "alerts.json")
    ev = event()
    seed(state, [ev], NOW - timedelta(hours=25))
    _, _, rows = tick(state, [ev])
    group_key = "prometheus:alpha:LongWatch"

    assert state.active[ev.key]["class"] == "chronic"
    assert state.active[ev.key]["class_reason"] == "auto_chronic:duration"
    datetime.fromisoformat(state.chronic_groups[group_key]["notified_chronic_at"])
    assert [row["type"] for row in rows].count("class_upgraded") == 1


def test_watch_short_duration_stays_watch(tmp_path):
    state = AlertsState(tmp_path / "alerts.json")
    ev = event()
    seed(state, [ev], NOW - timedelta(hours=23))
    tick(state, [ev])
    assert state.active[ev.key]["class"] == "watch"
    assert state.chronic_groups == {}


def test_chronic_upgrade_no_repeat_next_tick(tmp_path):
    state = AlertsState(tmp_path / "alerts.json")
    ev = event()
    seed(state, [ev], NOW - timedelta(hours=25))
    tick(state, [ev])
    notified = state.chronic_groups["prometheus:alpha:LongWatch"]["notified_chronic_at"]
    _, _, rows = tick(state, [ev], now=NOW + timedelta(minutes=5))
    assert state.chronic_groups["prometheus:alpha:LongWatch"]["notified_chronic_at"] == notified
    assert not any(row["type"] == "class_upgraded" for row in rows)


def test_sustained_chronic_survives_watch_classify(tmp_path):
    state = AlertsState(tmp_path / "alerts.json")
    ev = event()
    seed(state, [ev], NOW - timedelta(hours=25))
    tick(state, [ev])
    _, _, rows = tick(state, [ev], now=NOW + timedelta(minutes=5))
    assert state.active[ev.key]["class"] == "chronic"
    assert state.active[ev.key]["class_reason"] == "sustained_chronic"
    assert not any(row["type"] == "class_upgraded" for row in rows)


def test_manual_chronic_no_transition_notify(tmp_path):
    state = AlertsState(tmp_path / "alerts.json")
    ev = event()
    tick(state, [ev], profile=profiles(manual=True))
    assert state.chronic_groups["prometheus:alpha:LongWatch"]["notified_chronic_at"] == "manual"
    _, _, rows = tick(
        state, [ev], profile=profiles(manual=True), now=NOW + timedelta(minutes=5),
    )
    assert not any(row["type"] == "class_upgraded" for row in rows)


def test_profile_chronic_still_escalates_on_delta(tmp_path):
    state = AlertsState(tmp_path / "alerts.json")
    events = [event(host=f"node{index}") for index in range(3)]
    tick(state, events, profile=profiles(manual=True, baseline=2))
    assert all(state.active[ev.key]["class"] == "watch" for ev in events)
    assert state.chronic_groups == {}


def test_ignore_and_flapping_not_upgraded(tmp_path):
    for profile in (profiles(ignored=True), profiles(flapping=True)):
        state = AlertsState(tmp_path / f"{id(profile)}.json")
        ev = event()
        seed(state, [ev], NOW - timedelta(hours=25))
        tick(state, [ev], profile=profile)
        assert state.active[ev.key]["class"] in {"ignore", "flapping"}
        assert state.chronic_groups == {}


def test_old_state_json_no_chronic_groups_ok(tmp_path):
    path = tmp_path / "alerts.json"
    path.write_text('{"version": 1, "active": {}}', encoding="utf-8")
    assert AlertsState.load(path).chronic_groups == {}


def test_legacy_chronic_entry_auto_backfill(tmp_path):
    state = AlertsState(tmp_path / "alerts.json")
    events = [event(host="nodeA"), event(host="nodeB")]
    seed(state, events, NOW - timedelta(hours=25))
    for entry in state.active.values():
        entry["class"] = "chronic"
    tick(state, events)
    group = state.chronic_groups["prometheus:alpha:LongWatch"]
    assert group["notified_chronic_at"] == "manual"
    assert set(group["chronic_host_history"]) == {"nodeA", "nodeB"}


def test_cleanup_chronic_groups_all_hosts_resolved(tmp_path):
    state = AlertsState(tmp_path / "alerts.json")
    ev = event()
    state.chronic_groups["prometheus:alpha:LongWatch"] = {}
    state.transition([ev], NOW - timedelta(hours=1))
    state.transition([], NOW - timedelta(minutes=40))
    state._cleanup_chronic_groups(NOW)
    assert state.chronic_groups == {}

    state.chronic_groups["prometheus:alpha:LongWatch"] = {}
    state.transition([ev], NOW - timedelta(minutes=10))
    state.transition([], NOW - timedelta(minutes=5))
    state._cleanup_chronic_groups(NOW)
    assert "prometheus:alpha:LongWatch" in state.chronic_groups


def test_chronic_groups_round_trip(tmp_path):
    state = AlertsState(tmp_path / "alerts.json")
    state.chronic_groups["prometheus:alpha:x"] = {"chronic_host_history": ["nodeA"]}
    assert "chronic_groups" in state.to_dict()
    state.save()
    assert AlertsState.load(state.path).chronic_groups == state.chronic_groups
