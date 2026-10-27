from fastapi.testclient import TestClient

from lib.triage_agent.model_registry import ModelRegistry, ModelUpsert
from web.server.app import create_app


class FakeSupervisor:
    def __init__(self):
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def reconcile(self, configured):
        if configured:
            self.start()
        else:
            self.stop()

    def health(self):
        return {
            "state": "running" if self.started and not self.stopped else "stopped",
            "pid": 123,
        }


def test_app_starts_and_stops_injected_supervisor(tmp_path, monkeypatch):
    from web.server import app as app_module

    monkeypatch.setattr(app_module.sys, "platform", "linux")
    supervisor = FakeSupervisor()
    ModelRegistry(tmp_path / "config" / "model.local.yaml").create_model(
        ModelUpsert(
            title="Primary",
            base_url="https://relay.test/v1",
            api_key="secret",
            model="primary",
            default_reasoning_effort="medium",
        )
    )
    app = create_app(
        state_dir=tmp_path / "state",
        repo_root=tmp_path,
        triage_supervisor=supervisor,
    )

    with TestClient(app) as client:
        body = client.get("/api/triage-health").json()
        assert supervisor.started is True
        assert body["schema_version"] == 1
        assert body["model"] == "configured"
        assert body["can_create"] is True
        assert body["worker"] == {"state": "running", "pid": 123}

    assert supervisor.stopped is True


def test_missing_model_config_keeps_alert_console_available(tmp_path, monkeypatch):
    from web.server import app as app_module

    monkeypatch.setattr(app_module.sys, "platform", "linux")
    app = create_app(state_dir=tmp_path / "state", repo_root=tmp_path)

    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        body = client.get("/api/triage-health").json()
        assert body["model"] == "not_configured"
        assert body["can_create"] is False
        assert body["worker"] == {"state": "not_configured", "pid": None}


def test_health_reports_queue_counts_without_task_contents(tmp_path, monkeypatch):
    from web.server import app as app_module

    monkeypatch.setattr(app_module.sys, "platform", "linux")
    supervisor = FakeSupervisor()
    ModelRegistry(tmp_path / "config" / "model.local.yaml").create_model(
        ModelUpsert(
            title="Primary",
            base_url="https://relay.test/v1",
            api_key="secret",
            model="primary",
            default_reasoning_effort="medium",
        )
    )
    app = create_app(
        state_dir=tmp_path / "state",
        repo_root=tmp_path,
        triage_supervisor=supervisor,
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/triage-tasks",
            json={"message": "secret incident description"},
        )
        assert created.status_code == 201
        body = client.get("/api/triage-health").json()

    assert body["queue"]["queued"] == 1
    assert "secret" not in str(body)
