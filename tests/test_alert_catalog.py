import json
from datetime import datetime, timedelta, timezone

from lib.alerts.sources import PrometheusSource
from scripts import alert_poll
from tests.prometheus_feed import LABEL_CLUSTERS, FakeFeed


NOW = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)


class FakeNotifier:
    def dispatch(self, blocks):
        pass

    def flush_queue(self):
        pass


class FakePrometheus:
    def __init__(self, definitions):
        self.last_definitions = definitions


class FakeDemoSource:
    def __init__(self, definitions=None, error=None):
        self.definitions = definitions or []
        self.error = error
        self.calls = 0

    def fetch_definitions(self):
        self.calls += 1
        if self.error:
            raise self.error
        return list(self.definitions)


def _files(tmp_path, sources_text):
    config = tmp_path / "sources.yaml"
    config.write_text(f"""
version: 1
sources:
{sources_text}
poll:
  auto_triage_enabled: false
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
profiles:
  - {match_name: ignore_rule, match_env: alpha, class: ignore}
  - {match_name: chronic_rule, match_env: beta, class: chronic}
""", encoding="utf-8")
    routing = tmp_path / "routing.json"
    routing.write_text('{"alerts": {}, "docs": []}', encoding="utf-8")
    return config, profiles, routing


def _run(tmp_path, monkeypatch, sources_text, runtime, successes, *, dry_run=False):
    config, profiles, routing = _files(tmp_path, sources_text)
    monkeypatch.setattr(alert_poll, "load_poll_status", lambda path: {
        "effective": True, "enabled": True, "reason": None,
    })

    def collect(config, state):
        config["_runtime_sources"] = runtime
        return [], [], successes

    monkeypatch.setattr(alert_poll, "collect_events", collect)
    monkeypatch.setattr(alert_poll, "build_notifier", lambda *args: FakeNotifier())
    argv = [
        "--config", str(config), "--profiles", str(profiles),
        "--routing", str(routing), "--state-dir", str(tmp_path / "state"),
    ]
    if dry_run:
        argv.append("--dry-run")
    return alert_poll.run(
        argv, now=NOW, records_alerts_root=tmp_path / "records" / "alerts",
    )


def test_prometheus_definitions_come_from_rules_feed_in_the_same_tick():
    feed = FakeFeed(rules=[
        {"name": "inactive_rule", "group": "system", "state": "inactive",
         "labels": {"severity": "critical", "platform": "alpha"}},
        {"name": "active_rule", "group": "system", "state": "firing", "labels": {}},
    ])
    source = PrometheusSource(feed, LABEL_CLUSTERS, {}, query_start_ms=0, query_end_ms=300_000)

    assert source.fetch() == []
    assert [role for role, _ in feed.calls] == ["current", "rules", "history"]
    assert source.last_definitions == [
        {"alertname": "inactive_rule", "group": "system", "severity": "critical", "platform": "alpha", "state": "inactive"},
        {"alertname": "active_rule", "group": "system", "severity": "warning", "platform": "unknown", "state": "firing"},
    ]


def test_tick_writes_three_source_catalog_and_drops_retired_sources(
    tmp_path, monkeypatch, capsys,
):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_dir.joinpath("alert-catalog.json").write_text(json.dumps({
        "version": 1,
        "updated_at": (NOW - timedelta(hours=1)).isoformat(),
        "sources": {
            "mtail": {
                "updated_at": (NOW - timedelta(hours=1)).isoformat(),
                "alerts": [{"alertname": "retired_rule", "class": "watch"}],
            },
        },
    }), encoding="utf-8")
    sources = """  prometheus-central: {type: prometheus, plugin: prometheus}
  manager-alpha: {type: demo, platform: alpha}
  manager-beta: {type: demo, platform: beta}
"""
    runtime = {
        "prometheus": FakePrometheus([
            {"alertname": "ignore_rule", "group": "g", "severity": "warning", "platform": "alpha", "state": "inactive"},
            {"alertname": "chronic_rule", "group": "g", "severity": "warning", "platform": "beta", "state": "firing"},
            {"alertname": "default_rule", "group": "g", "severity": "critical", "platform": "alpha", "state": "firing"},
        ]),
        "demo:alpha": FakeDemoSource([{"alertname": "demo_hd", "label": "Alpha", "enabled": True}]),
        "demo:beta": FakeDemoSource([{"alertname": "demo_iot", "label": "Beta", "enabled": True}]),
    }

    assert _run(tmp_path, monkeypatch, sources, runtime, set(runtime)) == 0
    capsys.readouterr()
    catalog = json.loads((tmp_path / "state" / "alert-catalog.json").read_text())

    assert set(catalog["sources"]) == {"prometheus", "demo:alpha", "demo:beta"}
    prom = catalog["sources"]["prometheus"]["alerts"]
    assert next(item for item in prom if item["alertname"] == "ignore_rule")["class"] == "ignore"
    assert next(item for item in prom if item["alertname"] == "chronic_rule")["class"] == "chronic"
    assert next(item for item in prom if item["alertname"] == "default_rule")["class"] == "watch"
    assert next(item for item in prom if item["alertname"] == "ignore_rule")["state"] == "inactive"


def test_demo_refresh_failure_preserves_old_source(tmp_path, monkeypatch, capsys):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    old_source = {
        "updated_at": (NOW - timedelta(hours=2)).isoformat(),
        "alerts": [{"alertname": "preserved", "class": "watch"}],
    }
    (state_dir / "alert-catalog.json").write_text(json.dumps({
        "version": 1, "updated_at": old_source["updated_at"],
        "sources": {"demo:alpha": old_source},
    }), encoding="utf-8")
    fake = FakeDemoSource(error=RuntimeError("definitions down"))

    assert _run(
        tmp_path, monkeypatch, "  manager-alpha: {type: demo, platform: alpha}\n",
        {"demo:alpha": fake}, {"demo:alpha"},
    ) == 0
    captured = capsys.readouterr()
    catalog = json.loads((state_dir / "alert-catalog.json").read_text())

    assert fake.calls == 1
    assert catalog["sources"]["demo:alpha"] == old_source
    assert "catalog refresh failed" in captured.err


def test_recent_demo_catalog_skips_definition_request(tmp_path, monkeypatch, capsys):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    recent = {"updated_at": NOW.isoformat(), "alerts": [{"alertname": "fresh"}]}
    (state_dir / "alert-catalog.json").write_text(json.dumps({
        "version": 1, "updated_at": NOW.isoformat(),
        "sources": {"demo:alpha": recent},
    }), encoding="utf-8")
    fake = FakeDemoSource(error=AssertionError("must not refresh"))

    assert _run(
        tmp_path, monkeypatch, "  manager-alpha: {type: demo, platform: alpha}\n",
        {"demo:alpha": fake}, {"demo:alpha"},
    ) == 0
    capsys.readouterr()

    assert fake.calls == 0
    catalog = json.loads((state_dir / "alert-catalog.json").read_text())
    assert catalog["sources"]["demo:alpha"] == recent


def test_dry_run_does_not_write_catalog(tmp_path, monkeypatch, capsys):
    assert _run(
        tmp_path, monkeypatch, "  prometheus-central: {type: prometheus, plugin: prometheus}\n",
        {"prometheus": FakePrometheus([])}, {"prometheus"}, dry_run=True,
    ) == 0
    capsys.readouterr()
    assert not (tmp_path / "state" / "alert-catalog.json").exists()
