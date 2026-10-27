import sqlite3
from types import SimpleNamespace

import yaml

from lib.alerts.model import AlertEvent
from lib.alerts.state import AlertsState
from lib.alerts.sources import SourceError
from lib.alerts import timeline
from lib.alerts.dedup import apply, load_rules
from scripts import alert_poll


def entry(source, platform, alertname, host="node1", host_raw=None):
    event = AlertEvent(
        source=source, platform=platform, alertname=alertname, severity="critical",
        host=host, host_raw=host_raw or host, service="", state="firing", detail="",
    )
    return {"event": {
        "source": event.source, "platform": event.platform, "alertname": event.alertname,
        "severity": event.severity, "host": event.host, "host_raw": event.host_raw,
        "service": event.service, "state": event.state, "detail": event.detail,
    }}


def write_rules(path, rules):
    path.write_text(yaml.safe_dump({"version": 1, "rules": rules}, sort_keys=False), encoding="utf-8")
    return path


def pair_rule():
    return {
        "match": {
            "demo": {"platform": "alpha", "alertname": "zookeeper_server_process"},
            "prometheus": {"port": 2181},
        },
        "winner": "demo",
        "reason": "service context",
    }


def test_apply_suppresses_only_loser_when_both_sources_are_active(tmp_path):
    rules = load_rules(write_rules(tmp_path / "rules.yaml", [pair_rule()]))
    active = {
        "demo:alpha:zookeeper_server_process:node1": entry("demo", "alpha", "zookeeper_server_process"),
        "prometheus:alpha:zookeeper_port:node1": entry("prometheus", "alpha", "zookeeper_port", host_raw="node1:2181"),
    }

    assert apply(rules, active) == {
        "prometheus:alpha:zookeeper_port:node1": "demo:alpha:zookeeper_server_process:node1",
    }


def test_apply_does_not_suppress_loser_without_active_winner(tmp_path):
    rules = load_rules(write_rules(tmp_path / "rules.yaml", [pair_rule()]))
    active = {
        "prometheus:alpha:zookeeper_port:node1": entry("prometheus", "alpha", "zookeeper_port", host_raw="node1:2181"),
    }

    assert apply(rules, active) == {}


def test_apply_never_suppresses_events_with_unknown_platform(tmp_path):
    rules = load_rules(write_rules(tmp_path / "rules.yaml", [pair_rule()]))
    active = {
        "demo:alpha:zookeeper_server_process:node9": entry(
            "demo", "alpha", "zookeeper_server_process", host="node9",
        ),
        "prometheus:unknown:zookeeper_port:node9": entry(
            "prometheus", "unknown", "zookeeper_port", host="node9", host_raw="node9:2181",
        ),
    }

    assert apply(rules, active) == {}


def test_apply_keeps_unmatched_pairs_and_unique_demo_alerts(tmp_path):
    rules = load_rules(write_rules(tmp_path / "rules.yaml", [pair_rule()]))
    active = {
        "demo:alpha:datanode_health_summary:node1": entry("demo", "alpha", "datanode_health_summary"),
        "prometheus:alpha:other:node1": entry("prometheus", "alpha", "other", host_raw="node1:9999"),
    }

    assert apply(rules, active) == {}


def test_missing_or_empty_rules_are_safe(tmp_path):
    assert apply(load_rules(tmp_path / "missing.yaml"), {}) == {}
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    assert apply(load_rules(empty), {}) == {}


def test_tick_suppresses_notification_but_projects_loser_event(tmp_path, monkeypatch):
    rules = load_rules(write_rules(tmp_path / "rules.yaml", [pair_rule()]))
    monkeypatch.setattr(alert_poll, "load_dedup_rules", lambda *args: rules)
    demo = AlertEvent("demo", "alpha", "zookeeper_server_process", "critical", "node1", "node1", "", "firing", "")
    prometheus = AlertEvent("prometheus", "alpha", "zookeeper_port", "critical", "node1", "node1:2181", "", "firing", "")
    state = AlertsState(tmp_path / "alerts.json")
    profiles = SimpleNamespace(
        default_class="watch", profiles=[],
        class_config={"watch": {"min_duration_seconds": 0}},
    )
    notifier = SimpleNamespace(blocks=[], dispatch=lambda blocks: notifier.blocks.extend(blocks), flush_queue=lambda: None)

    counts, _, event_log = alert_poll._process_tick(
        [demo, prometheus], [], {"demo:alpha", "prometheus"}, state, profiles, {},
        {"poll": {"auto_triage_enabled": False}}, alert_poll._ensure_now("2026-07-19T00:00:00+00:00"),
        notifier=notifier, dry_run=False, records_alerts_root=tmp_path / "records" / "alerts",
    )

    assert counts["notified"] == 1
    assert len(notifier.blocks) == 1
    loser = next(row for row in event_log if row["key"] == prometheus.key and row["type"] == "new")
    assert loser["suppressed_by"] == demo.key
    db = tmp_path / "timeline.db"
    timeline.project(db, event_log)
    with sqlite3.connect(db) as conn:
        stored = conn.execute("SELECT suppressed_by FROM events WHERE key = ?", (prometheus.key,)).fetchone()
    assert stored == (demo.key,)


def test_failed_winner_source_does_not_suppress_new_loser(tmp_path, monkeypatch):
    rules = load_rules(write_rules(tmp_path / "rules.yaml", [pair_rule()]))
    monkeypatch.setattr(alert_poll, "load_dedup_rules", lambda *args: rules)
    demo = AlertEvent("demo", "alpha", "zookeeper_server_process", "critical", "node1", "node1", "", "firing", "")
    prometheus = AlertEvent("prometheus", "alpha", "zookeeper_port", "critical", "node1", "node1:2181", "", "firing", "")
    state = AlertsState(tmp_path / "alerts.json")
    state.transition([demo], "2026-07-19T00:00:00+00:00")
    state.active[demo.key]["class"] = "watch"
    state.active[demo.key]["class_reason"] = "default"
    state.active[demo.key]["notified_initial_at"] = "2026-07-19T00:00:00+00:00"
    profiles = SimpleNamespace(
        default_class="watch", profiles=[],
        class_config={"watch": {"min_duration_seconds": 0}},
    )
    notifier = SimpleNamespace(blocks=[], dispatch=lambda blocks: notifier.blocks.extend(blocks), flush_queue=lambda: None)

    counts, _, event_log = alert_poll._process_tick(
        [prometheus], [SourceError("demo:alpha", "down")], {"prometheus"}, state, profiles, {},
        {"poll": {"auto_triage_enabled": False}}, alert_poll._ensure_now("2026-07-19T00:05:00+00:00"),
        notifier=notifier, dry_run=False, records_alerts_root=tmp_path / "records" / "alerts",
    )

    loser = next(row for row in event_log if row["key"] == prometheus.key and row["type"] == "new")
    assert loser.get("suppressed_by") is None
    assert counts["notified"] == 1
    assert any("zookeeper_port" in block for block in notifier.blocks)

