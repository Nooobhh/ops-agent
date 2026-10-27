from fastapi.testclient import TestClient

from web.server.app import create_app


class FakeSupervisor:
    def __init__(self):
        self.started = False
        self.stopped = False
        self.reconciled = []

    def start(self):
        self.started = True
        self.stopped = False

    def stop(self):
        self.stopped = True

    def reconcile(self, configured):
        self.reconciled.append(configured)
        if configured:
            self.start()
        else:
            self.stop()

    def health(self):
        return {
            "state": "running" if self.started and not self.stopped else "stopped",
            "pid": 123 if self.started and not self.stopped else None,
        }


def model_payload(title, model, *, api_key="top-secret"):
    return {
        "title": title,
        "base_url": f"https://{model}.test/v1",
        "api_key": api_key,
        "model": model,
        "default_reasoning_effort": "high",
    }


def test_model_settings_crud_is_sanitized_and_reconciles_worker(tmp_path, monkeypatch):
    from web.server import app as app_module

    monkeypatch.setattr(app_module.sys, "platform", "linux")
    supervisor = FakeSupervisor()
    app = create_app(
        state_dir=tmp_path / "state",
        repo_root=tmp_path,
        triage_supervisor=supervisor,
    )

    with TestClient(app) as client:
        empty = client.get("/api/model-settings")
        assert empty.status_code == 200
        assert empty.json()["models"] == []

        created = client.post(
            "/api/model-settings/models",
            json=model_payload("Primary", "gpt-test"),
        )
        assert created.status_code == 201
        assert "top-secret" not in created.text
        assert created.json()["models"][0]["api_key_configured"] is True
        model_id = created.json()["models"][0]["id"]
        assert supervisor.started is True

        edited = client.put(
            f"/api/model-settings/models/{model_id}",
            json=model_payload("Primary Updated", "gpt-test-2", api_key=""),
        )
        assert edited.status_code == 200
        assert edited.json()["models"][0]["title"] == "Primary Updated"
        assert edited.json()["models"][0]["api_key_configured"] is True

        updated = client.put("/api/model-settings/defaults", json={
            "auto": {"model_id": model_id, "reasoning_effort": "high", "timeout_seconds": 90},
            "manual": {"model_id": model_id, "reasoning_effort": "medium", "timeout_seconds": 120},
            "weekly": {"model_id": model_id, "reasoning_effort": "low", "timeout_seconds": 300},
            "reviewer": {"model_id": model_id, "reasoning_effort": "none", "timeout_seconds": 180},
        })
        assert updated.status_code == 200
        assert updated.json()["defaults"]["reviewer"]["model_id"] == model_id
        assert updated.json()["defaults"]["auto"]["timeout_seconds"] == 90
        assert updated.json()["defaults"]["weekly"]["timeout_seconds"] == 300
        assert client.get("/api/model-settings").json()["defaults"]["reviewer"]["timeout_seconds"] == 180
        assert client.get("/api/triage-health").json()["can_create"] is True

    assert supervisor.reconciled[0] is False
    assert True in supervisor.reconciled
    assert supervisor.stopped is True


def test_default_and_active_model_delete_conflicts_map_to_409(tmp_path, monkeypatch):
    from web.server import app as app_module

    monkeypatch.setattr(app_module.sys, "platform", "linux")
    app = create_app(
        state_dir=tmp_path / "state",
        repo_root=tmp_path,
        triage_supervisor=FakeSupervisor(),
    )

    with TestClient(app) as client:
        primary = client.post(
            "/api/model-settings/models",
            json=model_payload("Primary", "primary"),
        ).json()["models"][0]
        secondary_response = client.post(
            "/api/model-settings/models",
            json=model_payload("Secondary", "secondary"),
        )
        secondary = secondary_response.json()["models"][1]

        default_conflict = client.delete(
            f"/api/model-settings/models/{primary['id']}"
        )
        assert default_conflict.status_code == 409
        assert "默认" in default_conflict.json()["detail"]

        task = client.post("/api/triage-tasks", json={
            "message": "检查备用模型",
            "model_id": secondary["id"],
            "reasoning_effort": "low",
        })
        assert task.status_code == 201
        active_conflict = client.delete(
            f"/api/model-settings/models/{secondary['id']}"
        )
        assert active_conflict.status_code == 409
        assert "未完成任务" in active_conflict.json()["detail"]


def test_reviewer_model_delete_conflicts_map_to_409(tmp_path, monkeypatch):
    from web.server import app as app_module

    monkeypatch.setattr(app_module.sys, "platform", "linux")
    app = create_app(
        state_dir=tmp_path / "state",
        repo_root=tmp_path,
        triage_supervisor=FakeSupervisor(),
    )

    with TestClient(app) as client:
        primary = client.post(
            "/api/model-settings/models",
            json=model_payload("Primary", "primary"),
        ).json()["models"][0]
        secondary = client.post(
            "/api/model-settings/models",
            json=model_payload("Reviewer", "reviewer"),
        ).json()["models"][1]
        configured = client.put("/api/model-settings/defaults", json={
            "auto": {"model_id": primary["id"], "reasoning_effort": "high"},
            "manual": {"model_id": primary["id"], "reasoning_effort": "medium"},
            "weekly": {"model_id": primary["id"], "reasoning_effort": "medium"},
            "reviewer": {"model_id": secondary["id"], "reasoning_effort": "low"},
        })
        assert configured.status_code == 200

        blocked = client.delete(
            f"/api/model-settings/models/{secondary['id']}"
        )
        assert blocked.status_code == 409
        assert "默认" in blocked.json()["detail"]


def test_agent_type_policies_are_editable_and_public(tmp_path, monkeypatch):
    from web.server import app as app_module

    monkeypatch.setattr(app_module.sys, "platform", "linux")
    app = create_app(
        state_dir=tmp_path / "state",
        repo_root=tmp_path,
        triage_supervisor=FakeSupervisor(),
    )

    with TestClient(app) as client:
        primary = client.post(
            "/api/model-settings/models",
            json=model_payload("Primary", "primary"),
        ).json()["models"][0]
        policies = {
            task_type: {
                "model_id": primary["id"],
                "reasoning_effort": "medium",
                "max_tool_turns": turns,
                "model_timeout_seconds": timeout,
            }
            for task_type, turns, timeout in (
                ("qa", 8, 60),
                ("investigation", 40, 180),
                ("alert", 24, 120),
                ("artifact", 16, 120),
                ("default", 12, 120),
            )
        }
        response = client.put(
            "/api/model-settings/agent-policies",
            json=policies,
        )

    assert response.status_code == 200
    assert response.json()["agent_policies"]["investigation"]["max_tool_turns"] == 40
    assert response.json()["agent_policies"]["qa"]["model_timeout_seconds"] == 60


def test_corrupt_model_config_is_reported_without_breaking_web(tmp_path, monkeypatch):
    from web.server import app as app_module

    monkeypatch.setattr(app_module.sys, "platform", "linux")
    config = tmp_path / "config" / "model.local.yaml"
    config.parent.mkdir()
    config.write_text(
        "api_key: do-not-expose\nmodels: [invalid\n",
        encoding="utf-8",
    )
    app = create_app(
        state_dir=tmp_path / "state",
        repo_root=tmp_path,
        triage_supervisor=FakeSupervisor(),
    )

    with TestClient(app) as client:
        settings = client.get("/api/model-settings")
        assert settings.status_code == 200
        assert settings.json()["models"] == []
        assert settings.json()["error"]
        assert "do-not-expose" not in settings.text
        assert client.get("/api/health").status_code == 200
        health = client.get("/api/triage-health").json()
        assert health["model"] == "not_configured"
        assert health["can_create"] is False

        mutation = client.post(
            "/api/model-settings/models",
            json=model_payload("Primary", "primary"),
        )
        assert mutation.status_code == 422
        assert "do-not-expose" not in mutation.text
