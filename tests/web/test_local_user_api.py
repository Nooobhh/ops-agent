"""本机用户接口:写 <数据根>/config/user.local.yaml,并作为人工任务的创建人。"""
from __future__ import annotations

import yaml
from fastapi.testclient import TestClient

from lib.triage_agent.model_registry import ModelRegistry, ModelUpsert
from web.server.app import create_app

from test_triage_tasks_api import write_platform_topology


def _client(tmp_path):
    return TestClient(create_app(state_dir=tmp_path / "state", repo_root=tmp_path))


def test_local_user_unconfigured_then_saved(tmp_path):
    client = _client(tmp_path)
    initial = client.get("/api/local-user").json()
    assert initial == {"schema_version": 1, "configured": False, "user": None, "error": None}

    saved = client.put("/api/local-user", json={"id": "alice", "display_name": "Haze"})
    assert saved.status_code == 200
    assert saved.json()["user"] == {"id": "alice", "display_name": "Haze", "inferred": False}
    stored = yaml.safe_load((tmp_path / "config" / "user.local.yaml").read_text(encoding="utf-8"))
    assert stored == {"id": "alice", "display_name": "Haze"}


def test_local_user_rejects_invalid_id(tmp_path):
    response = _client(tmp_path).put("/api/local-user", json={"id": "Bad Id", "display_name": ""})
    assert response.status_code == 422
    assert not (tmp_path / "config" / "user.local.yaml").exists()


def test_local_user_reports_broken_config(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "user.local.yaml").write_text("id: [x\n", encoding="utf-8")
    body = _client(tmp_path).get("/api/local-user").json()
    assert body["configured"] is False and "无效" in body["error"]


def test_manual_task_created_by_local_user(tmp_path):
    write_platform_topology(tmp_path)
    ModelRegistry(tmp_path / "config" / "model.local.yaml").create_model(ModelUpsert(
        title="M", base_url="https://relay.test/v1", api_key="k", model="m",
        default_reasoning_effort="medium",
    ))
    client = _client(tmp_path)
    before = client.post("/api/triage-tasks", json={"message": "node1 内存高", "platform": "alpha", "node": "node1"})
    assert before.status_code == 201 and before.json()["task"]["created_by"] is None

    client.put("/api/local-user", json={"id": "alice", "display_name": "Haze"})
    after = client.post("/api/triage-tasks", json={"message": "node1 内存高", "platform": "alpha", "node": "node1"})
    assert after.json()["task"]["created_by"] == "alice"
