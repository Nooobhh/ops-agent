"""按来源的中台推送开关:关了只是不推送,照常接收、记时间线与自动排查。"""
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from lib.alerts.source_notify import load_overrides, set_override, source_notify
from lib.alerts.state import AlertsState
from scripts import alert_poll
from tests.test_alert_poll import NOW, FakeNotifier, base_files, ev
from web.server.app import create_app

CONFIG = {"sources": {
    "prometheus-central": {"type": "prometheus", "plugin": "prometheus", "notify": False},
    "manager-alpha": {"type": "demo", "platform": "alpha"},
}}


def test_defaults_come_from_source_config_and_local_overrides_win(tmp_path):
    assert source_notify(CONFIG, tmp_path) == {
        "prometheus": {"notify": False, "default": False, "overridden": False},
        "demo:alpha": {"notify": True, "default": True, "overridden": False},
    }
    set_override(tmp_path, "prometheus", True)
    set_override(tmp_path, "demo:alpha", False)
    assert {key: item["notify"] for key, item in source_notify(CONFIG, tmp_path).items()} == {
        "prometheus": True, "demo:alpha": False,
    }
    set_override(tmp_path, "prometheus", None)
    assert load_overrides(tmp_path) == {"demo:alpha": False}
    (tmp_path / "alert-source-notify.json").write_text("{broken", encoding="utf-8")
    assert load_overrides(tmp_path) == {}


def _run(tmp_path, monkeypatch, events, *, muted, errors=()):
    config, profiles, routing = base_files(tmp_path)
    config.write_text(config.read_text(encoding="utf-8").replace(
        "sources: {}",
        "sources:\n  prometheus-central: {type: prometheus, plugin: prometheus}\n"
        "  manager-alpha: {type: demo, platform: alpha}",
    ), encoding="utf-8")
    for key in muted:
        set_override(tmp_path / "state", key, False)
    notifier = FakeNotifier()
    triaged = []
    monkeypatch.setattr(alert_poll, "collect_events", lambda *args, **kwargs: (events, list(errors)))
    monkeypatch.setattr(alert_poll, "build_notifier", lambda *args, **kwargs: notifier)
    monkeypatch.setattr(alert_poll, "build_pool_lazy", lambda *args, **kwargs: "pool")
    monkeypatch.setattr(alert_poll, "run_auto_triage", lambda event, routing, pool: triaged.append(event.key) or
                        SimpleNamespace(status="done", run_id="run-1", matched_doc="doc", verdict="needs_llm",
                                        severity="P1", conclusion_hint="", failed_steps=[], error=None))
    rc = alert_poll.run([
        "--config", str(config), "--profiles", str(profiles), "--routing", str(routing),
        "--state-dir", str(tmp_path / "state"),
    ], now=NOW, records_alerts_root=tmp_path / "records" / "alerts")
    assert rc == 0
    rows = [json.loads(line) for path in sorted((tmp_path / "records" / "alerts").glob("*.jsonl"))
            for line in path.read_text(encoding="utf-8").splitlines()]
    return notifier, triaged, rows


def test_muted_source_is_received_and_triaged_but_not_pushed(tmp_path, monkeypatch, capsys):
    events = [ev("disk_full", source="prometheus"), ev("datanode_down", source="demo")]
    notifier, triaged, rows = _run(tmp_path, monkeypatch, events, muted=["prometheus"])
    capsys.readouterr()

    pushed = "\n".join(notifier.blocks)
    assert "datanode_down" in pushed and "disk_full" not in pushed
    assert {row["key"] for row in rows if row["type"] == "new"} == {event.key for event in events}, "时间线照记"
    assert set(triaged) == {event.key for event in events}, "自动排查不受推送开关影响"
    state = AlertsState.load(tmp_path / "state" / "alerts.json")
    assert not state.active[events[0].key].get("notified_initial_at")
    assert not state.active[events[0].key].get("muted"), "不是规则级静音,条目不打静音标"


def test_muted_source_failure_is_not_pushed(tmp_path, monkeypatch, capsys):
    from lib.alerts.sources import SourceError

    state = AlertsState(tmp_path / "state" / "alerts.json")
    state.source_fail = {"prometheus": 2, "demo:alpha": 2}
    state.save()
    notifier, _, _ = _run(tmp_path, monkeypatch, [], muted=["prometheus"],
                          errors=[SourceError("prometheus", "down"), SourceError("demo:alpha", "down")])
    capsys.readouterr()

    pushed = "\n".join(notifier.blocks)
    assert "demo:alpha" in pushed and "prometheus 连续失败" not in pushed


def test_api_lists_and_switches_source_notify(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "alert-sources.yaml").write_text(
        "version: 1\nsources:\n  prometheus-central: {type: prometheus, plugin: prometheus, notify: false}\n"
        "  manager-alpha: {type: demo, platform: alpha}\n", encoding="utf-8")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    client = TestClient(create_app(state_dir=state_dir, repo_root=tmp_path))

    body = client.get("/api/alert-config").json()
    assert body["source_notify"]["prometheus"] == {"notify": False, "default": False, "overridden": False}

    switched = client.put("/api/alert-sources/prometheus/notify", json={"notify": True}).json()
    assert switched["source_notify"]["prometheus"] == {"notify": True, "default": False, "overridden": True}
    assert client.get("/api/poll/status").json()["sources"]["prometheus"]["notify"] is True

    restored = client.put("/api/alert-sources/prometheus/notify", json={"notify": None}).json()
    assert restored["source_notify"]["prometheus"]["notify"] is False
    assert client.put("/api/alert-sources/nope/notify", json={"notify": True}).status_code == 404
