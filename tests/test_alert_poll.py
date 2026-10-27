import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib.alerts.model import AlertEvent
from lib.alerts.channels import load_channels, save_channel
from lib.alerts.notify import FeishuNotifier, MultiNotifier, WecomNotifier
from lib.alerts.state import AlertsState
from lib.alerts.sources import SourceError
from scripts import alert_poll


NOW = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)
REAL_LOAD_POLL_STATUS = alert_poll.load_poll_status


@pytest.fixture(autouse=True)
def _enable_poll_gate(monkeypatch):
    monkeypatch.setattr(alert_poll, "load_poll_status", lambda _path: {
        "enabled": True,
        "effective": True,
        "reason": None,
        "updated_at": NOW.isoformat(),
        "since_boot": True,
    })


def ev(name, *, host="node1", source="prometheus", platform="alpha",
       severity="warning", maintenance=False):
    return AlertEvent(
        source=source,
        platform=platform,
        alertname=name,
        severity=severity,
        host=host,
        host_raw=host or "raw",
        service="system",
        state="firing",
        maintenance=maintenance,
        detail="",
    )


class FakeNotifier:
    def __init__(self):
        self.blocks = []
        self.calls = 0
        self.flushes = 0

    def dispatch(self, blocks):
        self.calls += 1
        self.blocks.extend(blocks)

    def flush_queue(self):
        self.flushes += 1


def write_yaml(path: Path, text: str):
    path.write_text(text, encoding="utf-8")
    return path


def test_build_notifier_migrates_legacy_wecom_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(alert_poll, "_load_credentials", lambda: {
        "wecom:ops-alerts": {"webhook_key": "legacy-key"},
    })

    notifier = alert_poll.build_notifier({}, tmp_path)

    assert isinstance(notifier, MultiNotifier)
    assert len(notifier.notifiers) == 1
    assert isinstance(notifier.notifiers[0], WecomNotifier)
    assert load_channels(tmp_path)["wecom"]["enabled"] is True
    assert load_channels(tmp_path)["wecom"]["webhook_key"] == "legacy-key"


def test_build_notifier_migration_preserves_existing_feishu(tmp_path, monkeypatch):
    feishu = save_channel(
        tmp_path,
        "feishu",
        enabled=True,
        credentials_update={
            "webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/token",
            "secret": "secret",
        },
        now=NOW,
    )
    monkeypatch.setattr(alert_poll, "_load_credentials", lambda: {
        "wecom:ops-alerts": {"webhook_key": "legacy-key"},
    })

    notifier = alert_poll.build_notifier({}, tmp_path)

    channels = load_channels(tmp_path)
    assert channels["feishu"] == feishu
    assert channels["wecom"]["webhook_key"] == "legacy-key"
    assert {type(item) for item in notifier.notifiers} == {WecomNotifier, FeishuNotifier}


def test_build_notifier_respects_existing_disabled_wecom(tmp_path, monkeypatch):
    existing = save_channel(
        tmp_path,
        "wecom",
        enabled=False,
        credentials_update={"webhook_key": "saved-key"},
        now=NOW,
    )
    monkeypatch.setattr(alert_poll, "_load_credentials", lambda: {
        "wecom:ops-alerts": {"webhook_key": "legacy-key"},
    })

    notifier = alert_poll.build_notifier({}, tmp_path)

    assert notifier.notifiers == []
    assert load_channels(tmp_path)["wecom"] == existing


def test_build_notifier_with_all_channels_disabled_does_not_call_network(tmp_path, monkeypatch):
    save_channel(
        tmp_path,
        "wecom",
        enabled=False,
        credentials_update={"webhook_key": "saved-key"},
    )
    save_channel(
        tmp_path,
        "feishu",
        enabled=False,
        credentials_update={
            "webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/token",
            "secret": "",
        },
    )
    monkeypatch.setattr(
        "lib.alerts.notify.request.urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected HTTP request")),
    )

    notifier = alert_poll.build_notifier({"notify": {"credential": "ignored"}}, tmp_path)
    notifier.dispatch(["message"])

    assert isinstance(notifier, MultiNotifier)
    assert notifier.notifiers == []


def base_files(tmp_path, *, min_duration=0, triage_concurrency=3):
    config = write_yaml(tmp_path / "alert-sources.yaml", f"""
version: 1
sources: {{}}
poll:
  triage_concurrency: {triage_concurrency}
  retrigger_suppress_minutes: 30
  source_fail_notify_after: 3
  auto_triage_enabled: true
notify:
  credential: wecom:ops-alerts
""")
    profiles = write_yaml(tmp_path / "profiles.yaml", f"""
version: 1
default_class: watch
class_config:
  watch: {{min_duration_seconds: {min_duration}}}
  chronic: {{escalate_host_delta: 2}}
  flapping: {{}}
  ignore: {{}}
profiles:
  - {{match_name: ignore_me, class: ignore, reason: ignored}}
  - {{match_name: chronic_me, class: chronic, baseline_host_count: 6, reason: chronic}}
""")
    routing = tmp_path / "routing.json"
    routing.write_text(json.dumps({"alerts": {}, "docs": []}), encoding="utf-8")
    return config, profiles, routing


def run_poll(tmp_path, monkeypatch, events, *, min_duration=0, notifier=None, triage=None,
             now=NOW, collect_result=None, pool_builder=None, triage_concurrency=3):
    config, profiles, routing = base_files(
        tmp_path, min_duration=min_duration, triage_concurrency=triage_concurrency,
    )
    notifier = notifier or FakeNotifier()
    monkeypatch.setattr(alert_poll, "collect_events",
                        lambda *args, **kwargs: collect_result or (events, []))
    monkeypatch.setattr(alert_poll, "build_notifier", lambda *args, **kwargs: notifier)
    monkeypatch.setattr(alert_poll, "build_pool_lazy",
                        pool_builder or (lambda *args, **kwargs: "pool"))
    if triage is None:
        triage = lambda event, routing, pool: SimpleNamespace(
            status="done",
            run_id=f"run-{event.alertname}",
            matched_doc="doc",
            verdict="needs_llm",
            severity="P1",
            conclusion_hint="hint",
            failed_steps=[],
            error=None,
        )
    monkeypatch.setattr(alert_poll, "run_auto_triage", triage)
    rc = alert_poll.run([
        "--config", str(config),
        "--profiles", str(profiles),
        "--routing", str(routing),
        "--state-dir", str(tmp_path / "state"),
    ], now=now, records_alerts_root=tmp_path / "records" / "alerts")
    return rc, notifier


def test_disabled_poll_exits_before_any_side_effect(tmp_path, monkeypatch, capsys):
    state_dir = tmp_path / "state"
    records_dir = tmp_path / "records" / "alerts"
    state_dir.mkdir(parents=True)
    records_dir.mkdir(parents=True)
    state_path = state_dir / "alerts.json"
    timeline_path = state_dir / "timeline.db"
    jsonl_path = records_dir / "2026-07-16.jsonl"
    state_path.write_text('{"version": 1, "active": {}, "source_fail": {}}', encoding="utf-8")
    timeline_path.write_bytes(b"existing timeline")
    jsonl_path.write_text('{"existing": true}\n', encoding="utf-8")
    state_dir.joinpath("poll-enabled.json").write_text(json.dumps({
        "enabled": False,
        "updated_at": NOW.isoformat(),
        "updated_by": "web",
    }), encoding="utf-8")
    before = {
        path: (path.stat().st_mtime_ns, path.read_bytes())
        for path in (state_path, timeline_path, jsonl_path)
    }

    def forbidden(*_args, **_kwargs):
        raise AssertionError("disabled tick reached a side effect")

    monkeypatch.setattr(alert_poll, "acquire_lock", forbidden)
    monkeypatch.setattr(alert_poll, "collect_events", forbidden)
    monkeypatch.setattr(alert_poll, "build_notifier", forbidden)
    monkeypatch.setattr(alert_poll, "load_poll_status", REAL_LOAD_POLL_STATUS)

    rc = alert_poll.run([
        "--config", str(tmp_path / "missing-config.yaml"),
        "--profiles", str(tmp_path / "missing-profiles.yaml"),
        "--routing", str(tmp_path / "missing-routing.json"),
        "--state-dir", str(state_dir),
    ], now=NOW, records_alerts_root=records_dir)

    output = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert output["ok"] is True
    assert output["skipped_poll_disabled"] is True
    assert "停止" in output["reason"]
    assert not state_dir.joinpath("alert_poll.lock").exists()
    for path, snapshot in before.items():
        assert (path.stat().st_mtime_ns, path.read_bytes()) == snapshot


def test_orchestration_processes_classes_triages_watch_and_writes_state_jsonl(tmp_path, monkeypatch, capsys):
    events = [
        ev("maint", maintenance=True),
        ev("ignore_me"),
        ev("chronic_me"),
        ev("watch_a", host="node2"),
        ev("watch_b", host="node3"),
    ]

    rc, notifier = run_poll(tmp_path, monkeypatch, events)
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert out["ok"] is True
    assert out["new"] == 5
    assert out["triaged"] == 2
    assert out["notified"] == 1
    assert notifier.calls == 1
    state = AlertsState.load(tmp_path / "state" / "alerts.json")
    assert state.active[events[0].key]["class"] == "ignore"
    assert state.active[events[2].key]["class"] == "chronic"
    assert state.active[events[3].key]["triage"]["status"] == "done"
    lines = (tmp_path / "records" / "alerts" / "2026-07-16.jsonl").read_text(encoding="utf-8").splitlines()
    assert {json.loads(line)["type"] for line in lines} >= {"new", "triaged", "notified"}
    new_details = [
        json.loads(line)["detail"] for line in lines
        if json.loads(line)["type"] == "new"
    ]
    assert new_details
    assert all(
        "description" in detail and "source_active_at" in detail
        for detail in new_details
    )


def test_source_fail_is_per_source_and_third_failure_notifies(tmp_path, monkeypatch, capsys):
    config, profiles, routing = base_files(tmp_path)
    state = AlertsState.load(tmp_path / "state" / "alerts.json")
    state.source_fail = {"prometheus": 2, "demo:alpha": 2}
    state.save()
    notifier = FakeNotifier()
    monkeypatch.setattr(alert_poll, "build_notifier", lambda *args, **kwargs: notifier)
    monkeypatch.setattr(alert_poll, "build_pool_lazy", lambda *args, **kwargs: "pool")
    monkeypatch.setattr(alert_poll, "run_auto_triage", lambda *args, **kwargs: SimpleNamespace(status="done", run_id="r", matched_doc="d", verdict=None, severity=None, conclusion_hint=None, failed_steps=[], error=None))

    def collect(*args, **kwargs):
        return [ev("watch_a")], [SourceError("demo:alpha", "down")], {"prometheus"}

    monkeypatch.setattr(alert_poll, "collect_events", collect)
    argv = ["--config", str(config), "--profiles", str(profiles), "--routing", str(routing), "--state-dir", str(tmp_path / "state")]
    rc = alert_poll.run(argv, now=NOW, records_alerts_root=tmp_path / "records" / "alerts")
    assert rc == 0
    capsys.readouterr()

    state = AlertsState.load(tmp_path / "state" / "alerts.json")
    assert "prometheus" not in state.source_fail
    assert state.source_fail["demo:alpha"] == 3
    assert any("source degraded" in block for block in notifier.blocks)


def test_watch_defer_then_ready_gets_initial_notification_and_triage(tmp_path, monkeypatch, capsys):
    event = ev("watch_wait")
    rc, notifier = run_poll(tmp_path, monkeypatch, [event], min_duration=300)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["deferred"] == 1
    assert out["triaged"] == 0
    assert notifier.blocks == []

    rc, notifier = run_poll(
        tmp_path, monkeypatch, [event], min_duration=300,
        notifier=FakeNotifier(), now=NOW + timedelta(minutes=6),
    )
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["triaged"] == 1
    assert out["notified"] == 1
    assert len(notifier.blocks) == 1
    state = AlertsState.load(tmp_path / "state" / "alerts.json")
    assert state.active[event.key]["notified_initial_at"] is not None


def test_deferred_active_becomes_ready_while_source_is_temporarily_failed(tmp_path, monkeypatch, capsys):
    event = ev("demo_deferred_watch", source="demo")
    rc, notifier = run_poll(tmp_path, monkeypatch, [event], min_duration=300)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["deferred"] == 1
    assert out["triaged"] == 0
    assert notifier.blocks == []

    rc, notifier = run_poll(
        tmp_path, monkeypatch, [], min_duration=300,
        notifier=FakeNotifier(), now=NOW + timedelta(minutes=6),
        collect_result=([], [SourceError("demo:alpha", "down")], set()),
    )
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert out["triaged"] == 1
    assert out["notified"] == 1
    assert len(notifier.blocks) == 1


def test_failed_triage_retries_while_source_is_temporarily_failed(tmp_path, monkeypatch, capsys):
    event = ev("demo_retry_watch", source="demo")
    outcomes = iter([
        SimpleNamespace(status="failed", run_id="old", matched_doc="d", verdict=None, severity=None, conclusion_hint=None, failed_steps=[], error="boom"),
        SimpleNamespace(status="done", run_id="new", matched_doc="d", verdict="needs_llm", severity="P1", conclusion_hint="hint", failed_steps=[], error=None),
    ])
    triage = lambda *args, **kwargs: next(outcomes)

    rc, _ = run_poll(tmp_path, monkeypatch, [event], min_duration=300)
    assert rc == 0
    capsys.readouterr()

    rc, notifier = run_poll(
        tmp_path, monkeypatch, [], min_duration=300, triage=triage,
        notifier=FakeNotifier(), now=NOW + timedelta(minutes=6),
        collect_result=([], [SourceError("demo:alpha", "down")], set()),
    )
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["triage_failed"] == 1

    rc, notifier = run_poll(
        tmp_path, monkeypatch, [], min_duration=300, triage=triage,
        notifier=notifier, now=NOW + timedelta(minutes=12),
        collect_result=([], [SourceError("demo:alpha", "down")], set()),
    )
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert out["triaged"] == 1
    assert out["notified"] == 1


def test_chronic_escalation_resets_triage_notifies_and_logs_escalated(tmp_path, monkeypatch, capsys):
    first_events = [ev("chronic_me", host=f"node{i}") for i in range(6)]
    rc, notifier = run_poll(tmp_path, monkeypatch, first_events, triage_concurrency=10)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["triaged"] == 0
    assert notifier.blocks == []

    second_events = [ev("chronic_me", host=f"node{i}") for i in range(8)]
    rc, notifier = run_poll(tmp_path, monkeypatch, second_events, notifier=FakeNotifier(),
                            now=NOW + timedelta(minutes=5), triage_concurrency=10)
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert out["triaged"] == 8
    assert out["notified"] == 1
    state = AlertsState.load(tmp_path / "state" / "alerts.json")
    assert state.active[second_events[0].key]["class"] == "watch"
    assert state.active[second_events[0].key]["triage"]["status"] == "done"
    rows = [
        json.loads(line)
        for line in (tmp_path / "records" / "alerts" / "2026-07-16.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(row["type"] == "escalated" for row in rows)

    rc, notifier = run_poll(tmp_path, monkeypatch, second_events, notifier=FakeNotifier(),
                            now=NOW + timedelta(minutes=10), triage_concurrency=10)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["notified"] == 0
    rows_after = [
        json.loads(line)
        for line in (tmp_path / "records" / "alerts" / "2026-07-16.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert sum(row["type"] == "escalated" for row in rows_after) == \
        sum(row["type"] == "escalated" for row in rows)


def test_first_observed_escalation_logs_once(tmp_path, monkeypatch, capsys):
    events = [ev("chronic_me", host=f"node{i}") for i in range(8)]
    rc, _ = run_poll(tmp_path, monkeypatch, events, triage_concurrency=10)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["triaged"] == 8
    rows = [
        json.loads(line)
        for line in (tmp_path / "records" / "alerts" / "2026-07-16.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    escalated_count = sum(row["type"] == "escalated" for row in rows)
    assert escalated_count == 8

    for minutes in (5, 10):
        rc, _ = run_poll(
            tmp_path, monkeypatch, events, notifier=FakeNotifier(),
            now=NOW + timedelta(minutes=minutes), triage_concurrency=10,
        )
        assert rc == 0
        capsys.readouterr()

    rows_after = [
        json.loads(line)
        for line in (tmp_path / "records" / "alerts" / "2026-07-16.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert sum(row["type"] == "escalated" for row in rows_after) == escalated_count


def test_resurfaced_within_suppress_window_does_not_retriage_or_notify(tmp_path, monkeypatch, capsys):
    event = ev("resurface")
    rc, notifier = run_poll(tmp_path, monkeypatch, [event])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["triaged"] == 1
    assert out["notified"] == 1
    first_state = AlertsState.load(tmp_path / "state" / "alerts.json")
    first_triage = dict(first_state.active[event.key]["triage"])

    rc, _ = run_poll(tmp_path, monkeypatch, [], notifier=FakeNotifier(), now=NOW + timedelta(minutes=1))
    assert rc == 0
    capsys.readouterr()

    rc, notifier = run_poll(
        tmp_path, monkeypatch, [event], notifier=FakeNotifier(),
        now=NOW + timedelta(minutes=10),
    )
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert out["triaged"] == 0
    assert out["notified"] == 0
    assert notifier.blocks == []
    state = AlertsState.load(tmp_path / "state" / "alerts.json")
    assert state.active[event.key]["triage"] == first_triage


def test_failed_triage_retries_next_tick_and_sends_only_diagnosis_followup(tmp_path, monkeypatch, capsys):
    event = ev("retry_me")
    config, profiles, routing = base_files(tmp_path)
    notifier = FakeNotifier()
    monkeypatch.setattr(alert_poll, "collect_events", lambda *args, **kwargs: ([event], []))
    monkeypatch.setattr(alert_poll, "build_notifier", lambda *args, **kwargs: notifier)
    monkeypatch.setattr(alert_poll, "build_pool_lazy", lambda *args, **kwargs: "pool")
    outcomes = iter([
        SimpleNamespace(status="failed", run_id="old", matched_doc="d", verdict=None, severity=None, conclusion_hint=None, failed_steps=[], error="boom"),
        SimpleNamespace(status="done", run_id="new", matched_doc="d", verdict="needs_llm", severity="P1", conclusion_hint="hint", failed_steps=[], error=None),
    ])
    monkeypatch.setattr(alert_poll, "run_auto_triage", lambda *args, **kwargs: next(outcomes))

    rc = alert_poll.run(["--config", str(config), "--profiles", str(profiles), "--routing", str(routing), "--state-dir", str(tmp_path / "state")], now=NOW, records_alerts_root=tmp_path / "records" / "alerts")
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["triage_failed"] == 1
    assert out["notified"] == 1

    rc = alert_poll.run(["--config", str(config), "--profiles", str(profiles), "--routing", str(routing), "--state-dir", str(tmp_path / "state")], now=NOW + timedelta(minutes=5), records_alerts_root=tmp_path / "records" / "alerts")
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert out["triaged"] == 1
    assert out["notified"] == 1
    assert len(notifier.blocks) == 2
    state = AlertsState.load(tmp_path / "state" / "alerts.json")
    assert state.active[event.key]["notified_initial_at"] is not None
    assert state.active[event.key]["notified_diagnosis_at"] is not None


def test_dry_run_has_no_state_jsonl_or_lock_side_effects(tmp_path, monkeypatch, capsys):
    config, profiles, routing = base_files(tmp_path)
    monkeypatch.setattr(alert_poll, "collect_events", lambda *args, **kwargs: ([ev("dry")], []))
    rc = alert_poll.run([
        "--config", str(config),
        "--profiles", str(profiles),
        "--routing", str(routing),
        "--state-dir", str(tmp_path / "state"),
        "--dry-run",
    ], now=NOW, records_alerts_root=tmp_path / "records" / "alerts")
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert out["ok"] is True
    assert out["dry_run"] is True
    assert out["actions"][0]["key"] == "prometheus:alpha:dry:node1"
    assert out["actions"][0]["class"] == "watch"
    assert out["actions"][0]["decision"] in {"would_triage", "bare_alert"}
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "records").exists()


def test_dry_run_deferred_event_outputs_single_decision_row(tmp_path, monkeypatch, capsys):
    config, profiles, routing = base_files(tmp_path, min_duration=300)
    monkeypatch.setattr(alert_poll, "collect_events", lambda *args, **kwargs: ([ev("dry_defer")], []))

    rc = alert_poll.run([
        "--config", str(config),
        "--profiles", str(profiles),
        "--routing", str(routing),
        "--state-dir", str(tmp_path / "state"),
        "--dry-run",
    ], now=NOW, records_alerts_root=tmp_path / "records" / "alerts")
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert len(out["actions"]) == 1
    assert out["actions"][0]["decision"] == "deferred"
    assert "action" not in out["actions"][0]


def test_dry_run_includes_ready_actions_derived_from_state_active(tmp_path, monkeypatch, capsys):
    config, profiles, routing = base_files(tmp_path, min_duration=300)
    routing.write_text(json.dumps({
        "alerts": {
            "alpha": {"zookeeper_server_process": {
                "doc": "knowledge/alpha/zookeeper/process.md",
                "playbook": "playbooks/alpha/system/check_memory_pressure.sh",
                "platform": "alpha",
                "service": "zookeeper",
            }}
        },
        "docs": [],
    }), encoding="utf-8")
    event = ev("zookeeper_server_process", source="demo")
    state = AlertsState.load(tmp_path / "state" / "alerts.json")
    state.transition([event], NOW - timedelta(minutes=6))
    state.active[event.key]["class"] = "watch"
    state.active[event.key]["class_reason"] = "default"
    state.save()
    monkeypatch.setattr(alert_poll, "collect_events", lambda *args, **kwargs: (
        [], [SourceError("demo:alpha", "down")], set(),
    ))

    rc = alert_poll.run([
        "--config", str(config),
        "--profiles", str(profiles),
        "--routing", str(routing),
        "--state-dir", str(tmp_path / "state"),
        "--dry-run",
    ], now=NOW, records_alerts_root=tmp_path / "records" / "alerts")
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert out["actions"] == [{
        "key": event.key,
        "class": "watch",
        "reason": "default",
        "decision": "would_triage",
        "would_notify": True,
        "would_triage": True,
    }]
    assert not (tmp_path / "records").exists()


def test_source_fail_config_error_and_success_use_same_source_key(tmp_path, monkeypatch, capsys):
    config = write_yaml(tmp_path / "alert-sources.yaml", """
version: 1
sources:
  prometheus-central:
    type: prometheus
poll: {triage_concurrency: 3, retrigger_suppress_minutes: 30, source_fail_notify_after: 3}
notify: {credential: wecom:ops-alerts}
""")
    profiles = write_yaml(tmp_path / "profiles.yaml", """
version: 1
default_class: watch
class_config: {watch: {min_duration_seconds: 0}, chronic: {}, flapping: {}, ignore: {}}
profiles: []
""")
    routing = tmp_path / "routing.json"
    routing.write_text(json.dumps({"alerts": {}, "docs": []}), encoding="utf-8")

    argv = ["--config", str(config), "--profiles", str(profiles), "--routing", str(routing), "--state-dir", str(tmp_path / "state")]
    rc = alert_poll.run(argv, now=NOW, records_alerts_root=tmp_path / "records" / "alerts")
    assert rc == 0
    capsys.readouterr()
    state = AlertsState.load(tmp_path / "state" / "alerts.json")
    assert state.source_fail == {"prometheus": 1}

    class FakePrometheus:
        def __init__(self, *args, **kwargs):
            pass
        def fetch(self):
            return []

    monkeypatch.setattr(alert_poll, "PrometheusSource", FakePrometheus)
    config.write_text("""
version: 1
sources:
  prometheus-central:
    type: prometheus
    plugin: prometheus
poll: {triage_concurrency: 3, retrigger_suppress_minutes: 30, source_fail_notify_after: 3}
notify: {credential: wecom:ops-alerts}
""", encoding="utf-8")
    rc = alert_poll.run(argv, now=NOW + timedelta(minutes=1), records_alerts_root=tmp_path / "records" / "alerts")
    assert rc == 0
    capsys.readouterr()
    state = AlertsState.load(tmp_path / "state" / "alerts.json")
    assert state.source_fail == {}


def test_source_error_freezes_active_entries_until_source_recovers(tmp_path, monkeypatch, capsys):
    events = [ev("source_down_a", host="node1"), ev("source_down_b", host="node2")]

    rc, notifier = run_poll(tmp_path, monkeypatch, events, triage_concurrency=10)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["triaged"] == 2
    assert out["notified"] == 1

    config, profiles, routing = base_files(tmp_path, triage_concurrency=10)
    monkeypatch.setattr(alert_poll, "build_notifier", lambda *args, **kwargs: FakeNotifier())
    monkeypatch.setattr(alert_poll, "build_pool_lazy", lambda *args, **kwargs: "pool")
    monkeypatch.setattr(alert_poll, "run_auto_triage", lambda *args, **kwargs: SimpleNamespace(status="done", run_id="r", matched_doc="d", verdict=None, severity=None, conclusion_hint=None, failed_steps=[], error=None))
    argv = ["--config", str(config), "--profiles", str(profiles), "--routing", str(routing), "--state-dir", str(tmp_path / "state")]

    for minutes in (5, 40, 80):
        monkeypatch.setattr(alert_poll, "collect_events",
                            lambda *args, **kwargs: ([], [SourceError("prometheus", "down")], set()))
        rc = alert_poll.run(argv, now=NOW + timedelta(minutes=minutes),
                            records_alerts_root=tmp_path / "records" / "alerts")
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["resolved"] == 0
        assert len(AlertsState.load(tmp_path / "state" / "alerts.json").active) == 2

    notifier = FakeNotifier()
    monkeypatch.setattr(alert_poll, "build_notifier", lambda *args, **kwargs: notifier)
    monkeypatch.setattr(alert_poll, "collect_events", lambda *args, **kwargs: (events, [], {"prometheus"}))
    rc = alert_poll.run(argv, now=NOW + timedelta(minutes=120),
                        records_alerts_root=tmp_path / "records" / "alerts")
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["new"] == 0
    assert out["triaged"] == 0
    assert out["notified"] == 0

    monkeypatch.setattr(alert_poll, "collect_events", lambda *args, **kwargs: ([], [], {"prometheus"}))
    rc = alert_poll.run(argv, now=NOW + timedelta(minutes=125),
                        records_alerts_root=tmp_path / "records" / "alerts")
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["resolved"] == 2


def test_mixed_platform_triage_uses_platform_pool_cache(tmp_path, monkeypatch, capsys):
    events = [ev("hd_watch", host="hd1", platform="alpha"), ev("iot_watch", host="iot1", platform="beta")]
    pools = []
    calls = []

    def build_pool(platform):
        pool = f"pool-{platform}"
        pools.append(pool)
        return pool

    def triage(event, routing, pool):
        calls.append((event.platform, event.host, pool))
        return SimpleNamespace(status="done", run_id=f"run-{event.platform}", matched_doc="d", verdict=None, severity=None, conclusion_hint=None, failed_steps=[], error=None)

    rc, _ = run_poll(
        tmp_path, monkeypatch, events, triage=triage,
        notifier=FakeNotifier(), pool_builder=build_pool,
    )
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert pools == ["pool-alpha", "pool-beta"]
    assert ("alpha", "hd1", "pool-alpha") in calls
    assert ("beta", "iot1", "pool-beta") in calls


def test_notifier_flushes_queue_even_without_new_blocks(tmp_path, monkeypatch, capsys):
    notifier = FakeNotifier()
    rc, _ = run_poll(tmp_path, monkeypatch, [], notifier=notifier)
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert out["notified"] == 0
    assert notifier.flushes == 1


def test_skipped_locked_writes_event_log(tmp_path, monkeypatch, capsys):
    config, profiles, routing = base_files(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "alert_poll.lock").write_text(
        json.dumps({"pid": 1, "started_at": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )

    rc = alert_poll.run([
        "--config", str(config),
        "--profiles", str(profiles),
        "--routing", str(routing),
        "--state-dir", str(state_dir),
    ], now=NOW, records_alerts_root=tmp_path / "records" / "alerts")
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert out["skipped_locked"] is True
    rows = [
        json.loads(line)
        for line in (tmp_path / "records" / "alerts" / "2026-07-16.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert rows[-1]["type"] == "skipped_locked"


def test_load_credentials_merges_project_local_and_home(tmp_path, monkeypatch):
    """数据根本地与 ~/.ops-agent 合并读取,home 优先(SSH 节点凭证与 Demo/wecom 键各归其位)。"""
    home = tmp_path / "home"
    (home / ".ops-agent").mkdir(parents=True)
    (home / ".ops-agent" / "credentials.json").write_text(
        json.dumps({"demo:alpha": {"user": "new_admin", "password": "np"}, "wecom:ops-alerts": {"webhook_key": "K"}}),
        encoding="utf-8",
    )
    project = tmp_path / "project"
    (project / "config").mkdir(parents=True)
    (project / "config" / "credentials.json").write_text(
        json.dumps({"198.51.100.101": {"user": "root", "password": "sshpw"}, "demo:alpha": {"user": "STALE", "password": "OLD"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPS_AGENT_DATA_ROOT", str(project))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    merged = alert_poll._load_credentials()
    assert merged["198.51.100.101"]["user"] == "root"  # 项目本地 SSH 凭证保留
    assert merged["demo:alpha"]["user"] == "new_admin"  # home 覆盖 stale
    assert merged["wecom:ops-alerts"]["webhook_key"] == "K"  # home 补齐


def test_exception_releases_lock(tmp_path, monkeypatch):
    config, profiles, routing = base_files(tmp_path)
    monkeypatch.setattr(alert_poll, "collect_events", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError):
        alert_poll.run(["--config", str(config), "--profiles", str(profiles), "--routing", str(routing), "--state-dir", str(tmp_path / "state")], now=NOW, records_alerts_root=tmp_path / "records" / "alerts")

    assert not (tmp_path / "state" / "alert_poll.lock").exists()


def test_tick_accepts_legacy_patrol_state_without_emitting_patrol_events(tmp_path, monkeypatch, capsys):
    config, profiles, routing = base_files(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "alerts.json").write_text(json.dumps({
        "version": 1,
        "patrol_last_scanned": "patrol-20260716-0800-alpha-node_health",
        "active": {
            "patrol:alpha:patrol_node_health:node1": {
                "first_seen": NOW.isoformat(),
                "last_seen": NOW.isoformat(),
                "class": "watch",
                "class_reason": "default",
                "triage": {"status": "pending", "attempts": 0, "last_attempt_at": None},
                "notified_initial_at": None,
                "event": {
                    "source": "patrol",
                    "platform": "alpha",
                    "alertname": "patrol:node_health:disk",
                    "severity": "warning",
                    "host": "node1",
                    "host_raw": "node1",
                    "service": "patrol",
                    "state": "firing",
                    "detail": "",
                },
            }
        },
    }), encoding="utf-8")
    notifier = FakeNotifier()
    monkeypatch.setattr(alert_poll, "collect_events", lambda *args, **kwargs: (
        [ev("healthy_source_alert", source="demo")], [], {"demo:alpha"},
    ))
    monkeypatch.setattr(alert_poll, "build_notifier", lambda *args, **kwargs: notifier)
    monkeypatch.setattr(alert_poll, "build_pool_lazy", lambda *args, **kwargs: "pool")
    monkeypatch.setattr(alert_poll, "run_auto_triage", lambda event, routing, pool: SimpleNamespace(
        status="done", run_id="run", matched_doc="doc", verdict=None, severity=None,
        conclusion_hint=None, failed_steps=[], error=None,
    ))

    rc = alert_poll.run([
        "--config", str(config), "--profiles", str(profiles),
        "--routing", str(routing), "--state-dir", str(state_dir),
    ], now=NOW, records_alerts_root=tmp_path / "records" / "alerts")

    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["ok"] is True
    assert notifier.blocks
    assert all("patrol" not in block for block in notifier.blocks)
    rows = [
        json.loads(line)
        for line in (tmp_path / "records" / "alerts" / "2026-07-16.jsonl").read_text(
            encoding="utf-8",
        ).splitlines()
    ]
    assert all(not str(row.get("key") or "").startswith("patrol:") for row in rows)
    assert all("patrol" not in json.dumps(row.get("detail"), ensure_ascii=False) for row in rows)


def test_legacy_two_tuple_collect_result_derives_successes_from_observed_events_only():
    prometheus = ev("prom")
    demo_iot = ev("beta", source="demo", platform="beta")

    events, errors, successes = alert_poll._normalize_collect_result((
        [prometheus, demo_iot], [],
    ))

    assert events == [prometheus, demo_iot]
    assert errors == []
    assert successes == {"prometheus", "demo:beta"}


def test_tick_prunes_jsonl_files_older_than_ninety_days(tmp_path, monkeypatch, capsys):
    config, profiles, routing = base_files(tmp_path)
    alerts_root = tmp_path / "records" / "alerts"
    alerts_root.mkdir(parents=True)
    old = alerts_root / "2026-04-19.jsonl"
    boundary = alerts_root / "2026-04-20.jsonl"
    unrelated = alerts_root / "manual-export.jsonl"
    old.write_text("old\n", encoding="utf-8")
    boundary.write_text("boundary\n", encoding="utf-8")
    unrelated.write_text("manual\n", encoding="utf-8")
    monkeypatch.setattr(alert_poll, "collect_events", lambda *args, **kwargs: ([], [], set()))
    monkeypatch.setattr(alert_poll, "build_notifier", lambda *args, **kwargs: FakeNotifier())

    rc = alert_poll.run([
        "--config", str(config), "--profiles", str(profiles),
        "--routing", str(routing), "--state-dir", str(tmp_path / "state"),
    ], now=datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc), records_alerts_root=alerts_root)
    capsys.readouterr()

    assert rc == 0
    assert not old.exists()
    assert boundary.exists()
    assert unrelated.exists()


def test_alert_sources_no_longer_exposes_patrol_scanner():
    import lib.alerts.sources as sources

    assert not hasattr(sources, "PatrolScanner")


def test_auto_triage_disabled_skips_pool_and_batch_selection(tmp_path, monkeypatch, capsys):
    config, profiles, routing = base_files(tmp_path)
    text = config.read_text(encoding="utf-8").replace("  auto_triage_enabled: true\n", "")
    config.write_text(text, encoding="utf-8")
    event = ev("watch_without_ssh")
    notifier = FakeNotifier()
    monkeypatch.setattr(alert_poll, "collect_events", lambda *args, **kwargs: ([event], []))
    monkeypatch.setattr(alert_poll, "build_notifier", lambda *args, **kwargs: notifier)
    monkeypatch.setattr(alert_poll, "build_pool_lazy", lambda *args, **kwargs: pytest.fail("pool must not be built"))
    monkeypatch.setattr(alert_poll.AlertsState, "select_triage_batch", lambda *args, **kwargs: pytest.fail("batch must not be selected"))
    monkeypatch.setattr(alert_poll, "run_auto_triage", lambda *args, **kwargs: pytest.fail("triage must not run"))

    rc = alert_poll.run([
        "--config", str(config), "--profiles", str(profiles),
        "--routing", str(routing), "--state-dir", str(tmp_path / "state"),
    ], now=NOW, records_alerts_root=tmp_path / "records" / "alerts")

    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["triaged"] == 0
    assert out["triage_failed"] == 0


def test_auto_triage_enabled_preserves_collection_path(tmp_path, monkeypatch, capsys):
    config, profiles, routing = base_files(tmp_path)
    event = ev("watch_with_ssh")
    calls = []
    notifier = FakeNotifier()
    monkeypatch.setattr(alert_poll, "collect_events", lambda *args, **kwargs: ([event], []))
    monkeypatch.setattr(alert_poll, "build_notifier", lambda *args, **kwargs: notifier)
    monkeypatch.setattr(alert_poll, "build_pool_lazy", lambda platform: calls.append(("pool", platform)) or "pool")
    monkeypatch.setattr(alert_poll, "run_auto_triage", lambda event, routing, pool: calls.append(("triage", pool)) or SimpleNamespace(
        status="done", run_id="run", matched_doc="doc", verdict="needs_llm", severity="P1",
        conclusion_hint="hint", failed_steps=[], error=None,
    ))

    rc = alert_poll.run([
        "--config", str(config), "--profiles", str(profiles),
        "--routing", str(routing), "--state-dir", str(tmp_path / "state"),
    ], now=NOW, records_alerts_root=tmp_path / "records" / "alerts")

    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["triaged"] == 1
    assert calls == [("pool", "alpha"), ("triage", "pool")]


def test_auto_triage_disabled_keeps_existing_triage_state(tmp_path, monkeypatch, capsys):
    config, profiles, routing = base_files(tmp_path)
    config.write_text(config.read_text(encoding="utf-8").replace("  auto_triage_enabled: true\n", "  auto_triage_enabled: false\n"), encoding="utf-8")
    event = ev("existing_triage")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state = AlertsState(state_dir / "alerts.json")
    state.transition([event], NOW)
    state.active[event.key]["triage"] = {
        "status": "failed", "attempts": 2, "last_attempt_at": "old", "run_id": "r",
    }
    state.save()
    monkeypatch.setattr(alert_poll, "collect_events", lambda *args, **kwargs: ([event], []))
    monkeypatch.setattr(alert_poll, "build_notifier", lambda *args, **kwargs: FakeNotifier())

    before = dict(AlertsState.load(state_dir / "alerts.json").active[event.key]["triage"])
    rc = alert_poll.run([
        "--config", str(config), "--profiles", str(profiles),
        "--routing", str(routing), "--state-dir", str(state_dir),
    ], now=NOW + timedelta(minutes=5), records_alerts_root=tmp_path / "records" / "alerts")
    capsys.readouterr()

    assert rc == 0
    after = AlertsState.load(state_dir / "alerts.json").active[event.key]["triage"]
    assert after == before


def test_source_status_marks_failed_source_and_keeps_other_sources_ok(tmp_path, monkeypatch, capsys):
    config, profiles, routing = base_files(tmp_path)
    config.write_text(config.read_text(encoding="utf-8").replace("sources: {}", """sources:
  prometheus-central: {type: prometheus}
  manager-alpha: {type: demo, platform: alpha}
"""), encoding="utf-8")
    monkeypatch.setattr(alert_poll, "build_notifier", lambda *args, **kwargs: FakeNotifier())
    monkeypatch.setattr(alert_poll, "collect_events", lambda *args, **kwargs: (
        [], [SourceError("prometheus", "down")], {"demo:alpha"},
    ))

    rc = alert_poll.run([
        "--config", str(config), "--profiles", str(profiles),
        "--routing", str(routing), "--state-dir", str(tmp_path / "state"),
    ], now=NOW, records_alerts_root=tmp_path / "records" / "alerts")

    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["source_status"] == {"prometheus-central": "fail", "manager-alpha": "ok"}
    assert out["ok"] is True


def test_all_source_failures_return_ok_false_without_raising(tmp_path, monkeypatch, capsys):
    config, profiles, routing = base_files(tmp_path)
    config.write_text(config.read_text(encoding="utf-8").replace("sources: {}", """sources:
  prometheus-central: {type: prometheus}
  manager-alpha: {type: demo, platform: alpha}
"""), encoding="utf-8")
    monkeypatch.setattr(alert_poll, "build_notifier", lambda *args, **kwargs: FakeNotifier())
    monkeypatch.setattr(alert_poll, "collect_events", lambda *args, **kwargs: (
        [], [SourceError("prometheus", "down"), SourceError("demo:alpha", "down")], set(),
    ))

    rc = alert_poll.run([
        "--config", str(config), "--profiles", str(profiles),
        "--routing", str(routing), "--state-dir", str(tmp_path / "state"),
    ], now=NOW, records_alerts_root=tmp_path / "records" / "alerts")

    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["ok"] is False
    assert out["source_status"] == {"prometheus-central": "fail", "manager-alpha": "fail"}


def test_event_log_carries_event_severity_and_service(tmp_path, monkeypatch, capsys):
    event = ev("disk_full", severity="critical")

    rc, _ = run_poll(tmp_path, monkeypatch, [event])
    assert rc == 0
    capsys.readouterr()

    rows = [
        json.loads(line)
        for line in (tmp_path / "records" / "alerts" / "2026-07-16.jsonl")
        .read_text(encoding="utf-8").splitlines()
    ]
    keyed = [row for row in rows if row["key"] == event.key]
    assert keyed
    assert all(row["severity"] == "critical" for row in keyed)
    assert all(row["service"] == "system" for row in keyed)


def test_event_log_without_event_context_has_null_storage_fields():
    row = alert_poll._log(
        NOW, "source_error", "prometheus", "", "source_error", "down",
        severity=None, service=None,
    )

    assert row["severity"] is None
    assert row["service"] is None
