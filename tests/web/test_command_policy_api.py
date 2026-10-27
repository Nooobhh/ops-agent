from tests.web.test_triage_tasks_api import client_at, create_task
from lib.triage_agent.models import TaskStatus
from lib.triage_agent.task_store import TaskStore


def test_command_policy_can_be_read_and_mode_changed(tmp_path):
    _, client = client_at(tmp_path)

    initial = client.get("/api/command-policy")
    assert initial.status_code == 200
    assert set(initial.json()) == {"mode", "allow", "deny"}

    changed = client.put("/api/command-policy", json={"mode": "bypass"})
    assert changed.status_code == 200
    assert changed.json()["mode"] == "bypass"
    assert client.get("/api/command-policy").json()["mode"] == "bypass"


def test_tool_catalog_lists_the_runtime_tools_without_command_templates(tmp_path):
    _, client = client_at(tmp_path)
    response = client.get("/api/triage-tools")
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == 1
    names = {item["name"] for item in payload["items"]}
    assert {"shell", "diagnostic", "kb_read", "finish", "create_kb_draft", "create_task_artifact"} <= names
    assert "run_command" not in names and "run_registered_playbook" not in names
    for item in payload["items"]:
        assert item["description"] and item["parameters"]["type"] == "object"


def test_command_policy_rejects_invalid_mode_and_unknown_fields(tmp_path):
    _, client = client_at(tmp_path)

    assert client.put("/api/command-policy", json={"mode": "wide"}).status_code == 422
    assert client.put("/api/command-policy", json={"mode": "auto", "allow": []}).status_code == 422


def test_runtime_controls_accept_a_partial_update(tmp_path):
    state_dir, client = client_at(tmp_path)
    created = create_task(client, platform="alpha", node="node1")
    task_id = created["task"]["task_id"]

    response = client.patch(
        f"/api/triage-tasks/{task_id}/runtime-controls",
        json={"command_mode": "bypass", "node": "node2"},
    )

    assert response.status_code == 200
    assert response.json()["task"]["command_mode"] == "bypass"
    assert response.json()["task"]["node"] == "node2"


def test_runtime_controls_reject_unknown_fields(tmp_path):
    _, client = client_at(tmp_path)
    task_id = create_task(client)["task"]["task_id"]

    assert client.patch(
        f"/api/triage-tasks/{task_id}/runtime-controls",
        json={"unexpected": True},
    ).status_code == 422


def test_runtime_controls_keep_omitted_fields_and_accept_explicit_null(tmp_path):
    state_dir, client = client_at(tmp_path)
    created = create_task(client, platform="alpha", node="node1", model_id="model-02")
    task_id = created["task"]["task_id"]

    changed = client.patch(
        f"/api/triage-tasks/{task_id}/runtime-controls",
        json={"command_mode": "auto"},
    ).json()["task"]
    assert changed["command_mode"] == "auto"
    assert changed["node"] == "node1"
    assert changed["model_profile_id"] == "model-02"

    cleared = client.patch(
        f"/api/triage-tasks/{task_id}/runtime-controls",
        json={"node": None},
    ).json()["task"]
    assert cleared["node"] is None
    assert cleared["command_mode"] == "auto"


def test_runtime_controls_reject_auto_and_cancelled_but_allow_completed(tmp_path):
    state_dir, client = client_at(tmp_path)
    store = TaskStore(state_dir / "triage-tasks")

    auto, _ = store.enqueue_auto("occ-1", {"alertname": "x", "platform": "alpha", "host": "node1"})
    assert client.patch(
        f"/api/triage-tasks/{auto.task_id}/runtime-controls",
        json={"command_mode": "bypass"},
    ).status_code == 409

    cancelled = create_task(client)["task"]["task_id"]
    store.transition(cancelled, TaskStatus.QUEUED, TaskStatus.CANCELLED)
    assert client.patch(
        f"/api/triage-tasks/{cancelled}/runtime-controls",
        json={"command_mode": "bypass"},
    ).status_code == 409

    completed = create_task(client)["task"]["task_id"]
    store.transition(completed, TaskStatus.QUEUED, TaskStatus.RUNNING)
    store.transition(completed, TaskStatus.RUNNING, TaskStatus.COMPLETED)
    assert client.patch(
        f"/api/triage-tasks/{completed}/runtime-controls",
        json={"command_mode": "bypass"},
    ).status_code == 200


def test_runtime_controls_refuse_node_when_topology_unreadable(tmp_path):
    """拓扑读不出来时必须拒绝写节点。

    此前路由层把读失败吞成空列表,而 service 层「空表即跳过校验」,两者相加
    会在拓扑缺失时静默放行任何脏节点——与 set_task_context 的硬拒不一致。
    """
    _, client = client_at(tmp_path)
    task_id = create_task(client, platform="alpha")["task"]["task_id"]
    (tmp_path / "config" / "platform-alpha.yaml").unlink()

    response = client.patch(
        f"/api/triage-tasks/{task_id}/runtime-controls",
        json={"node": "node1"},
    )

    assert response.status_code == 409
    assert "拓扑不可读" in response.json()["detail"]


def test_create_refuses_platform_inference_when_topology_unreadable(tmp_path):
    _, client = client_at(tmp_path)
    (tmp_path / "config" / "topology-db.yaml").unlink()

    response = client.post("/api/triage-tasks", json={"message": "排查 198.51.100.101 磁盘异常"})

    assert response.status_code == 409
    assert "拓扑不可读" in response.json()["detail"]


def test_runtime_controls_returns_available_nodes_for_out_of_bounds_node(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "platform-alpha.yaml").write_text(
        "platform: alpha\nnodes:\n  node1:\n    host: 198.51.100.101\n  node2:\n    host: 198.51.100.102\n",
        encoding="utf-8",
    )
    _, client = client_at(tmp_path)
    task_id = create_task(client, platform="alpha")["task"]["task_id"]

    response = client.patch(
        f"/api/triage-tasks/{task_id}/runtime-controls",
        json={"node": "node99"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["available_nodes"] == ["node1", "node2"]
