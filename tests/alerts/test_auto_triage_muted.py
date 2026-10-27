from datetime import datetime, timezone
from types import SimpleNamespace

from lib.alerts.model import AlertEvent
from lib.alerts.profiles import ProfilesConfig
from lib.alerts.state import AlertsState
from scripts import alert_poll


NOW = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)


def event(name="Watch"):
    return AlertEvent(
        source="prometheus", platform="alpha", alertname=name, severity="warning",
        host="nodeA", host_raw="nodeA", service="system", state="firing", detail="",
    )


def run(state, ev, monkeypatch, *, muted=False, override=False):
    if not state.active:
        state.transition([ev], NOW)
    state.active[ev.key]["class"] = "watch"
    if muted:
        state.active[ev.key]["muted"] = True
    pools = []
    triages = []
    monkeypatch.setattr(
        alert_poll, "build_pool_lazy",
        lambda platform: pools.append(platform) or object(),
    )
    monkeypatch.setattr(
        alert_poll, "run_auto_triage",
        lambda *args: triages.append(args) or SimpleNamespace(
            status="done", run_id="run", error=None,
        ),
    )
    config = {
        "poll": {"auto_triage_enabled": True, "triage_concurrency": 3},
        "_rule_overrides": {
            f"prometheus:{ev.alertname}": {"notify": False}
        } if override else {},
        "_nodes_by_platform": {"alpha": ["nodeA", "nodeB"]},
    }
    alert_poll._process_tick(
        [ev], [], {"prometheus"}, state,
        ProfilesConfig(
            1, "watch", {"watch": {"min_duration_seconds": 0}, "chronic": {}}, [],
        ),
        {}, config, NOW, notifier=None, dry_run=False,
        records_alerts_root=state.path.parent,
    )
    return pools, triages


def test_muted_watch_not_auto_triaged(tmp_path, monkeypatch):
    state = AlertsState(tmp_path / "alerts.json")
    ev = event()
    pools, triages = run(state, ev, monkeypatch, muted=True)
    assert pools == [] and triages == []
    triage = state.active[ev.key]["triage"]
    assert triage["status"] == "not_applicable"
    assert triage["defer_reason"] == "muted"


def test_rule_override_notify_false_not_auto_triaged(tmp_path, monkeypatch):
    state = AlertsState(tmp_path / "alerts.json")
    ev = event()
    pools, triages = run(state, ev, monkeypatch, override=True)
    assert pools == [] and triages == []
    triage = state.active[ev.key]["triage"]
    assert triage["status"] == "not_applicable"
    assert triage["defer_reason"] == "override_muted"


def test_unmuted_watch_still_auto_triaged(tmp_path, monkeypatch):
    state = AlertsState(tmp_path / "alerts.json")
    ev = event()
    pools, triages = run(state, ev, monkeypatch)
    assert pools == ["alpha"]
    assert len(triages) == 1
    assert state.active[ev.key]["triage"]["status"] == "done"
