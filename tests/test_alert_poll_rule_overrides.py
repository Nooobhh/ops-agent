import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lib.alerts.model import AlertEvent
from lib.alerts.rule_overrides import set_rule_override
from lib.alerts.state import AlertsState
from scripts import alert_poll


NOW = datetime(2026, 7, 22, 9, 0, tzinfo=timezone.utc)


class FakeNotifier:
    def __init__(self):
        self.blocks = []

    def dispatch(self, blocks):
        self.blocks.extend(blocks)

    def flush_queue(self):
        pass


def _event(name, *, severity="critical", source="prometheus", host="hd01"):
    return AlertEvent(
        source=source,
        platform="alpha",
        alertname=name,
        severity=severity,
        host=host,
        host_raw=host,
        service="system",
        state="firing",
        detail="detail",
    )


def _files(tmp_path: Path):
    config = tmp_path / "sources.yaml"
    config.write_text("""
version: 1
sources: {}
poll:
  auto_triage_enabled: false
  triage_concurrency: 3
  retrigger_suppress_minutes: 30
  source_fail_notify_after: 3
""", encoding="utf-8")
    profiles = tmp_path / "profiles.yaml"
    profiles.write_text("""
version: 1
default_class: watch
class_config:
  watch: {min_duration_seconds: 0}
  chronic: {}
  flapping: {}
  ignore: {}
profiles: []
""", encoding="utf-8")
    routing = tmp_path / "routing.json"
    routing.write_text('{"alerts": {}, "docs": []}', encoding="utf-8")
    return config, profiles, routing


def _run(tmp_path, monkeypatch, events, notifier, *, now=NOW):
    config, profiles, routing = _files(tmp_path)
    monkeypatch.setattr(alert_poll, "load_poll_status", lambda _path: {
        "effective": True, "enabled": True, "reason": None,
    })
    monkeypatch.setattr(
        alert_poll, "collect_events", lambda *args: (events, [], {
            alert_poll._event_source_id(event) for event in events
        }),
    )
    monkeypatch.setattr(alert_poll, "build_notifier", lambda *args: notifier)
    return alert_poll.run([
        "--config", str(config),
        "--profiles", str(profiles),
        "--routing", str(routing),
        "--state-dir", str(tmp_path / "state"),
    ], now=now, records_alerts_root=tmp_path / "records" / "alerts")


def test_severity_override_flows_to_state_log_and_notification_once_loaded(
    tmp_path, monkeypatch, capsys,
):
    event = _event("DiskFull")
    state_dir = tmp_path / "state"
    set_rule_override(state_dir, "prometheus:DiskFull", severity="warning")
    real_load = alert_poll.load_rule_overrides
    calls = []
    monkeypatch.setattr(
        alert_poll,
        "load_rule_overrides",
        lambda path: calls.append(path) or real_load(path),
    )
    notifier = FakeNotifier()

    assert _run(tmp_path, monkeypatch, [event], notifier) == 0
    capsys.readouterr()

    assert calls == [state_dir]
    state = AlertsState.load(state_dir / "alerts.json")
    assert state.active[event.key]["severity"] == "warning"
    assert state.active[event.key]["event"]["severity"] == "warning"
    rows = [
        json.loads(line)
        for line in (tmp_path / "records" / "alerts" / "2026-07-22.jsonl").read_text().splitlines()
    ]
    assert next(row for row in rows if row["type"] == "new")["severity"] == "warning"
    assert len(notifier.blocks) == 1
    assert "WARNING" in notifier.blocks[0]


def test_muted_rule_skips_only_its_notification_and_marks_entry(tmp_path, monkeypatch, capsys):
    muted = _event("Muted")
    other = _event("Other", host="hd02")
    set_rule_override(tmp_path / "state", "prometheus:Muted", notify=False)
    notifier = FakeNotifier()

    assert _run(tmp_path, monkeypatch, [muted, other], notifier) == 0
    capsys.readouterr()

    state = AlertsState.load(tmp_path / "state" / "alerts.json")
    assert state.active[muted.key]["muted"] is True
    assert state.active[muted.key]["notified_initial_at"] is None
    assert state.active[other.key].get("muted") is None
    assert state.active[other.key]["notified_initial_at"] is not None
    assert len(notifier.blocks) == 1
    assert "Other" in notifier.blocks[0]


def test_unmuting_active_rule_sends_pending_initial_notification(tmp_path, monkeypatch, capsys):
    event = _event("Resume")
    state_dir = tmp_path / "state"
    set_rule_override(state_dir, "prometheus:Resume", notify=False)

    assert _run(tmp_path, monkeypatch, [event], FakeNotifier()) == 0
    capsys.readouterr()
    set_rule_override(state_dir, "prometheus:Resume", clear=True)
    notifier = FakeNotifier()
    assert _run(
        tmp_path, monkeypatch, [event], notifier, now=NOW + timedelta(minutes=5),
    ) == 0
    capsys.readouterr()

    entry = AlertsState.load(state_dir / "alerts.json").active[event.key]
    assert "muted" not in entry
    assert entry["notified_initial_at"] is not None
    assert len(notifier.blocks) == 1


def test_dedup_and_muting_remain_independent(tmp_path, monkeypatch, capsys):
    event = _event("SuppressedAndMuted")
    set_rule_override(
        tmp_path / "state", "prometheus:SuppressedAndMuted", notify=False,
    )
    monkeypatch.setattr(alert_poll, "apply_dedup", lambda rules, active: {
        event.key: "prometheus:alpha:Winner:hd02",
    })
    monkeypatch.setattr(alert_poll, "load_dedup_rules", lambda path: [])
    notifier = FakeNotifier()

    assert _run(tmp_path, monkeypatch, [event], notifier) == 0
    capsys.readouterr()

    entry = AlertsState.load(tmp_path / "state" / "alerts.json").active[event.key]
    assert entry["muted"] is True
    assert entry["notified_initial_at"] is None
    assert notifier.blocks == []
