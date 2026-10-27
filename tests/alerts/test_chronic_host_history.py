from datetime import datetime, timedelta, timezone

from lib.alerts.model import AlertEvent
from lib.alerts.profiles import ProfilesConfig
from lib.alerts.state import AlertsState
from scripts.alert_poll import _process_tick


NOW = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
GROUP = "prometheus:alpha:Spread"


class Notifier:
    def __init__(self):
        self.blocks = []

    def dispatch(self, blocks):
        self.blocks.extend(blocks)

    def flush_queue(self):
        pass


def event(host, name="Spread"):
    return AlertEvent(
        source="prometheus", platform="alpha", alertname=name, severity="warning",
        host=host, host_raw=host, service="system", state="firing", detail="",
    )


def profile(manual=False, baseline=None):
    rows = []
    if manual:
        row = {"match_name": "Spread", "class": "chronic", "reason": "manual"}
        if baseline is not None:
            row["baseline_host_count"] = baseline
        rows.append(row)
    return ProfilesConfig(
        1, "watch", {"watch": {"min_duration_seconds": 0}, "chronic": {}}, rows,
    )


def tick(state, events, *, now=NOW, profiles=None, notifier=None, config=None):
    notifier = notifier or Notifier()
    _process_tick(
        events, [], {"prometheus"}, state, profiles or profile(), {},
        config or {
            "poll": {"auto_triage_enabled": False},
            "_nodes_by_platform": {"alpha": [f"node{i}" for i in range(10)]},
        },
        now, notifier=notifier, dry_run=False,
        records_alerts_root=state.path.parent,
    )
    return notifier


def seed_old_watch(state, events):
    state.transition(events, NOW - timedelta(hours=25))
    for entry in state.active.values():
        entry["class"] = "watch"


def seed_chronic(state, events, *, history=None, triggered=False):
    state.transition(events, NOW - timedelta(hours=1))
    for entry in state.active.values():
        entry["class"] = "chronic"
    state.chronic_groups[GROUP] = {
        "notified_chronic_at": NOW.isoformat(),
        "notified_cluster_wide_at": None,
        "chronic_host_history": list(history or []),
        "cluster_wide_triggered": triggered,
        "first_upgraded_at": NOW.isoformat(),
    }


def test_first_upgrade_no_host_added_notify(tmp_path):
    state = AlertsState(tmp_path / "alerts.json")
    events = [event("nodeA"), event("nodeB")]
    seed_old_watch(state, events)
    notifier = tick(state, events)
    assert len([b for b in notifier.blocks if "转为常驻" in b]) == 1
    assert not any("新增受影响主机" in b for b in notifier.blocks)
    assert set(state.chronic_groups[GROUP]["chronic_host_history"]) == {"nodeA", "nodeB"}


def test_group_host_history_shared_across_hosts(tmp_path):
    state = AlertsState(tmp_path / "alerts.json")
    first = [event("nodeA"), event("nodeB")]
    seed_old_watch(state, first)
    tick(state, first)
    notifier = tick(state, [*first, event("nodeC")], now=NOW + timedelta(minutes=5))
    assert len([b for b in notifier.blocks if "新增受影响主机" in b]) == 1
    assert "`nodeC`" in notifier.blocks[0]
    assert set(state.chronic_groups[GROUP]["chronic_host_history"]) == {
        "nodeA", "nodeB", "nodeC",
    }


def test_new_host_triggers_added_notify(tmp_path):
    state = AlertsState(tmp_path / "alerts.json")
    seed_chronic(state, [event("nodeA")], history=["nodeA"])
    notifier = tick(state, [event("nodeA"), event("nodeB")])
    assert sum("新增受影响主机" in block for block in notifier.blocks) == 1
    assert state.chronic_groups[GROUP]["chronic_host_history"] == ["nodeA", "nodeB"]


def test_disappeared_host_reappears_no_duplicate(tmp_path):
    state = AlertsState(tmp_path / "alerts.json")
    events = [event("nodeA"), event("nodeB")]
    seed_chronic(state, events, history=["nodeA", "nodeB"])
    tick(state, [event("nodeA")])
    notifier = tick(state, events, now=NOW + timedelta(minutes=5))
    assert not any("新增受影响主机" in block for block in notifier.blocks)
    assert state.chronic_groups[GROUP]["chronic_host_history"] == ["nodeA", "nodeB"]


def test_muted_group_no_host_notify(tmp_path):
    state = AlertsState(tmp_path / "alerts.json")
    seed_chronic(state, [event("nodeA")], history=["nodeA"])
    new_events = [event("nodeA"), event("nodeB")]
    state.transition(new_events, NOW - timedelta(minutes=1))
    for entry in state.active.values():
        entry["class"] = "chronic"
        entry["muted"] = True
    notifier = tick(state, new_events)
    assert not any("新增受影响主机" in block for block in notifier.blocks)
    assert "nodeB" in state.chronic_groups[GROUP]["chronic_host_history"]


def test_manual_chronic_new_host_still_notified(tmp_path):
    state = AlertsState(tmp_path / "alerts.json")
    tick(state, [event("nodeA")], profiles=profile(manual=True))
    assert state.chronic_groups[GROUP]["notified_chronic_at"] == "manual"
    notifier = tick(
        state, [event("nodeA"), event("nodeB")],
        profiles=profile(manual=True), now=NOW + timedelta(minutes=5),
    )
    assert sum("新增受影响主机" in block for block in notifier.blocks) == 1
