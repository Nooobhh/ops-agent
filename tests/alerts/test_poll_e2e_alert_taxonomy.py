from datetime import datetime, timedelta, timezone

from lib.alerts.model import AlertEvent
from lib.alerts.profiles import ProfilesConfig
from lib.alerts.state import AlertsState
from scripts.alert_poll import _process_tick


NOW = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
GROUP = "prometheus:alpha:E2E_ALERT"


class CapturingNotifier:
    def __init__(self):
        self.blocks = []

    def dispatch(self, blocks):
        self.blocks.extend(blocks)

    def flush_queue(self):
        pass


def event(host):
    return AlertEvent(
        source="prometheus", platform="alpha", alertname="E2E_ALERT",
        severity="warning", host=host, host_raw=host, service="HDFS",
        state="firing", detail="e2e",
    )


def tick(state, events, *, now=NOW, denominator=10):
    notifier = CapturingNotifier()
    _counts, _actions, rows = _process_tick(
        events, [], {"prometheus"}, state,
        ProfilesConfig(
            1, "watch", {"watch": {"min_duration_seconds": 0}, "chronic": {}}, [],
        ),
        {},
        {
            "poll": {"auto_triage_enabled": False},
            "_nodes_by_platform": {
                "alpha": [f"node{index}" for index in range(denominator)]
            },
        },
        now, notifier=notifier, dry_run=False,
        records_alerts_root=state.path.parent / "timeline",
    )
    return notifier, rows


def old_watch(state, events):
    state.transition(events, NOW - timedelta(hours=25))
    for entry in state.active.values():
        entry["class"] = "watch"
        entry["class_reason"] = "default"


def test_e2e_watch_to_chronic_full_tick(tmp_path):
    state = AlertsState(tmp_path / "state" / "alerts.json")
    events = [event("nodeA"), event("nodeB")]
    old_watch(state, events)

    notifier, rows = tick(state, events)
    group = state.chronic_groups[GROUP]

    assert all(state.active[item.key]["class"] == "chronic" for item in events)
    assert {
        "notified_chronic_at", "notified_cluster_wide_at",
        "chronic_host_history", "cluster_wide_triggered", "first_upgraded_at",
    } <= set(group)
    assert set(group["chronic_host_history"]) == {"nodeA", "nodeB"}
    assert sum(row["type"] == "class_upgraded" for row in rows) == 1
    assert sum("转为常驻" in block for block in notifier.blocks) == 1
    datetime.fromisoformat(group["notified_chronic_at"])


def test_e2e_chronic_new_host_flow(tmp_path):
    state = AlertsState(tmp_path / "state" / "alerts.json")
    first = [event("nodeA"), event("nodeB")]
    old_watch(state, first)
    tick(state, first)

    notifier, rows = tick(
        state, [*first, event("nodeC")], now=NOW + timedelta(minutes=5),
    )

    assert sum("新增受影响主机" in block for block in notifier.blocks) == 1
    assert set(state.chronic_groups[GROUP]["chronic_host_history"]) == {
        "nodeA", "nodeB", "nodeC",
    }
    assert not any(row["type"] == "class_upgraded" for row in rows)


def test_e2e_cluster_wide_threshold(tmp_path):
    state = AlertsState(tmp_path / "state" / "alerts.json")
    six = [event(f"node{index}") for index in range(6)]
    state.transition(six, NOW - timedelta(hours=1))
    for entry in state.active.values():
        entry["class"] = "chronic"
    state.chronic_groups[GROUP] = {
        "notified_chronic_at": NOW.isoformat(),
        "notified_cluster_wide_at": None,
        "chronic_host_history": [f"node{index}" for index in range(6)],
        "cluster_wide_triggered": False,
        "first_upgraded_at": NOW.isoformat(),
    }

    seven = [event(f"node{index}") for index in range(7)]
    eight = [event(f"node{index}") for index in range(8)]
    first, _ = tick(state, seven)
    steady, _ = tick(state, eight, now=NOW + timedelta(minutes=5))
    tick(state, six, now=NOW + timedelta(minutes=10))
    second, _ = tick(state, seven, now=NOW + timedelta(minutes=15))

    assert sum("CLUSTER-WIDE" in block for block in first.blocks) == 1
    assert not any("CLUSTER-WIDE" in block for block in steady.blocks)
    assert sum("CLUSTER-WIDE" in block for block in second.blocks) == 1
