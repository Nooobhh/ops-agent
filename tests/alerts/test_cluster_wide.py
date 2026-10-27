from datetime import timedelta

from lib.alerts.state import AlertsState

from tests.alerts.test_chronic_host_history import (
    GROUP, NOW, event, profile, seed_chronic, tick,
)


def config(nodes=10):
    return {
        "poll": {"auto_triage_enabled": False},
        "_nodes_by_platform": {"alpha": [f"node{i}" for i in range(nodes)]},
    }


def events(count):
    return [event(f"node{index}") for index in range(count)]


def test_reaches_70pct_triggers_cluster_wide(tmp_path):
    state = AlertsState(tmp_path / "alerts.json")
    seed_chronic(state, events(6), history=[f"node{i}" for i in range(6)])
    notifier = tick(state, events(7), config=config())
    assert sum("CLUSTER-WIDE" in block for block in notifier.blocks) == 1
    assert state.chronic_groups[GROUP]["cluster_wide_triggered"] is True
    assert state.chronic_groups[GROUP]["notified_cluster_wide_at"]


def test_reaches_70pct_uses_floor_for_fractional_denominators(tmp_path):
    state = AlertsState(tmp_path / "alerts.json")
    ten = events(10)
    seed_chronic(state, ten, history=[f"node{i}" for i in range(10)])

    below = tick(state, ten, config=config(17))
    at_floor = tick(
        state, events(11), config=config(17), now=NOW + timedelta(minutes=5),
    )

    assert not any("CLUSTER-WIDE" in block for block in below.blocks)
    assert sum("覆盖 11/17 台" in block for block in at_floor.blocks) == 1
    cluster_block = next(
        block for block in at_floor.blocks if "CLUSTER-WIDE" in block
    )
    assert "threshold 11" in cluster_block

    singleton = AlertsState(tmp_path / "singleton.json")
    seed_chronic(singleton, events(1), history=["node0"])
    singleton_notifier = tick(singleton, events(1), config=config(1))
    assert sum("CLUSTER-WIDE" in block for block in singleton_notifier.blocks) == 1
    assert "threshold 0" in singleton_notifier.blocks[0]


def test_cluster_wide_only_once_per_group(tmp_path):
    state = AlertsState(tmp_path / "alerts.json")
    seed_chronic(state, events(3), history=[f"node{i}" for i in range(3)])
    notifier = tick(state, events(3), config=config(4))
    assert sum("CLUSTER-WIDE" in block for block in notifier.blocks) == 1


def test_stays_above_threshold_no_repeat(tmp_path):
    state = AlertsState(tmp_path / "alerts.json")
    seed_chronic(state, events(7), history=[f"node{i}" for i in range(7)])
    first = tick(state, events(7), config=config())
    second = tick(
        state, events(8), config=config(), now=NOW + timedelta(minutes=5),
    )
    assert sum("CLUSTER-WIDE" in block for block in first.blocks) == 1
    assert not any("CLUSTER-WIDE" in block for block in second.blocks)


def test_drops_below_then_returns(tmp_path):
    state = AlertsState(tmp_path / "alerts.json")
    seed_chronic(state, events(7), history=[f"node{i}" for i in range(7)])
    first = tick(state, events(7), config=config())
    tick(state, events(6), config=config(), now=NOW + timedelta(minutes=5))
    second = tick(
        state, events(7), config=config(), now=NOW + timedelta(minutes=10),
    )
    assert sum("CLUSTER-WIDE" in block for block in first.blocks) == 1
    assert sum("CLUSTER-WIDE" in block for block in second.blocks) == 1


def test_denominator_from_profile_baseline(tmp_path):
    state = AlertsState(tmp_path / "alerts.json")
    five = events(5)
    seed_chronic(state, five, history=[f"node{i}" for i in range(5)])
    notifier = tick(
        state, five, profiles=profile(manual=True, baseline=6), config=config(10),
    )
    assert sum("覆盖 5/6 台" in block for block in notifier.blocks) == 1


def test_denominator_zero_no_trigger(tmp_path):
    state = AlertsState(tmp_path / "alerts.json")
    seed_chronic(state, events(1), history=["node0"])
    notifier = tick(state, events(1), config=config(0))
    assert not any("CLUSTER-WIDE" in block for block in notifier.blocks)


def test_denominator_tick_local_not_persisted(tmp_path):
    state = AlertsState(tmp_path / "alerts.json")
    seed_chronic(state, events(1), history=["node0"])
    tick(state, events(1), config=config())
    assert all("_profile_snapshot" not in entry for entry in state.active.values())
    assert "_profile_snapshot" not in state.chronic_groups[GROUP]
