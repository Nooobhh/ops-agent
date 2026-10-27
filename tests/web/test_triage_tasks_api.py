import json
from pathlib import Path

from fastapi.testclient import TestClient

from lib.triage_agent.model_registry import ModelRegistry, ModelUpsert
from lib.triage_agent.models import TaskStatus
from lib.triage_agent.task_store import TaskStore
from web.server.app import create_app
from tests.envpack_helpers import as_env_pack


def write_platform_topology(repo_root: Path):
    """节点校验读真实拓扑;不铺这份最小配置,校验会因拓扑不可读而拒绝请求。"""
    as_env_pack(repo_root)
    config_dir = repo_root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    for platform in ("alpha", "beta"):
        (config_dir / f"platform-{platform}.yaml").write_text(
            "platform: {}\nnodes:\n"
            "  node1:\n    host: 198.51.100.101\n"
            "  node2:\n    host: 198.51.100.102\n".format(platform),
            encoding="utf-8",
        )
    # 节点推断平台要遍历全部登记目标,共享拓扑也得在
    for name, node, host in (
        ("topology-db.yaml", "db1", "203.0.113.1"),
        ("topology-misc.yaml", "vm-app01", "203.0.113.2"),
        ("topology-cache.yaml", "cache01", "203.0.113.3"),
        ("topology-edge.yaml", "site-a-edge", "203.0.113.4"),
    ):
        (config_dir / name).write_text(
            f"platform: shared\nnodes:\n  {node}:\n    host: {host}\n",
            encoding="utf-8",
        )


def client_at(tmp_path):
    state_dir = tmp_path / "state"
    write_platform_topology(tmp_path)
    ModelRegistry(
        tmp_path / "config" / "model.local.yaml",
        id_factory=lambda: "model-02",
    ).create_model(ModelUpsert(
        title="Test Model",
        base_url="https://relay.test/v1",
        api_key="test-secret",
        model="test-model",
        default_reasoning_effort="medium",
    ))
    return state_dir, TestClient(create_app(
        state_dir=state_dir,
        repo_root=tmp_path,
    ))


def files_below(root: Path):
    if not root.exists():
        return set()
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }


def create_task(client, **overrides):
    body = {"message": "node3 内存持续升高", **overrides}
    response = client.post("/api/triage-tasks", json=body)
    assert response.status_code == 201
    return response.json()


def test_create_list_and_read_manual_task(tmp_path):
    _, client = client_at(tmp_path)

    created = create_task(client, platform="alpha", node="node3")
    task_id = created["task"]["task_id"]

    assert created["schema_version"] == 1
    assert created["task"]["status"] == "queued"
    assert client.get(f"/api/triage-tasks/{task_id}").json() == created
    listed = client.get("/api/triage-tasks").json()
    assert listed["schema_version"] == 1
    assert [item["task_id"] for item in listed["items"]] == [task_id]


def test_task_can_be_renamed(tmp_path):
    _, client = client_at(tmp_path)
    created = create_task(client)
    task_id = created["task"]["task_id"]

    response = client.patch(
        f"/api/triage-tasks/{task_id}",
        json={"title": "node3 内存巡检"},
    )

    assert response.status_code == 200
    assert response.json()["task"]["display_title"] == "node3 内存巡检"
    assert client.patch(
        f"/api/triage-tasks/{task_id}",
        json={"title": "  "},
    ).status_code == 422


def test_task_api_accepts_selection_fields_without_exposing_secrets(tmp_path):
    _, client = client_at(tmp_path)

    created = create_task(
        client,
        model_id="model-02",
        reasoning_effort="high",
    )

    assert "api_key" not in str(created)
    assert "base_url" not in str(created)


def test_create_task_accepts_command_mode_override(tmp_path):
    _, client = client_at(tmp_path)

    created = create_task(client, command_mode="auto")

    assert created["task"]["command_mode"] == "auto"


def test_resource_risk_card_creates_bound_investigation_task(tmp_path):
    state_dir, client = client_at(tmp_path)
    state_dir.mkdir(parents=True, exist_ok=True)
    risk_ref = "resource_forecast:beta:business:node1:iot_mysql_exporter:row_lock_current_waits"
    state_dir.joinpath("resource-risks.json").write_text(json.dumps({
        "findings": [{
            "key": risk_ref,
            "severity": "critical",
            "platform": "beta",
            "node": "node1",
            "resource": "business",
            "evidence": {
                "service": "MySQL",
                "job": "iot_mysql_exporter",
                "metric": "row_lock_current_waits",
                "label": "当前行锁等待",
                "value": 15,
                "threshold": 10,
                "samples": 112,
            },
        }],
    }), encoding="utf-8")

    response = client.post(
        f"/api/resource-risks/{risk_ref}/triage-task",
        json={},
    )

    assert response.status_code == 201
    task = response.json()["task"]
    assert task["task_type"] == "investigation"
    assert task["platform"] == "beta"
    assert task["node"] == "node1"
    assert task["risk_seed"]["risk_ref"] == risk_ref
    assert task["risk_seed"]["value"] == 15.0
    assert task["manual_artifacts"] is True


def test_events_support_after_cursor(tmp_path):
    _, client = client_at(tmp_path)
    created = create_task(client)
    task_id = created["task"]["task_id"]
    first = client.get(f"/api/triage-tasks/{task_id}/events").json()["items"]

    tail = client.get(
        f"/api/triage-tasks/{task_id}/events",
        params={"after": first[0]["event_id"]},
    ).json()["items"]

    assert [item["event_id"] for item in tail] == [
        item["event_id"] for item in first[1:]
    ]


def test_message_resumes_waiting_input_task(tmp_path):
    state_dir, client = client_at(tmp_path)
    created = create_task(client)
    task_id = created["task"]["task_id"]
    store = TaskStore(state_dir / "triage-tasks")
    store.transition(task_id, TaskStatus.QUEUED, TaskStatus.RUNNING)
    store.transition(
        task_id,
        TaskStatus.RUNNING,
        TaskStatus.WAITING_INPUT,
        lease_owner=None,
        lease_expires_at=None,
    )

    response = client.post(
        f"/api/triage-tasks/{task_id}/messages",
        json={"message": "alpha 平台，node3"},
    )

    assert response.status_code == 200
    assert response.json()["task"]["status"] == "queued"


def test_human_completion_marks_idle_task_and_follow_up_reopens_it(tmp_path):
    state_dir, client = client_at(tmp_path)
    created = create_task(client)
    task_id = created["task"]["task_id"]
    store = TaskStore(state_dir / "triage-tasks")
    store.transition(task_id, TaskStatus.QUEUED, TaskStatus.RUNNING)
    store.transition(task_id, TaskStatus.RUNNING, TaskStatus.WAITING_INPUT)

    completed = client.post(f"/api/triage-tasks/{task_id}/complete", json={})
    assert completed.status_code == 200
    assert completed.json()["task"]["status"] == "completed"

    resumed = client.post(
        f"/api/triage-tasks/{task_id}/messages",
        json={"message": "继续补充调查"},
    )
    assert resumed.status_code == 200
    assert resumed.json()["task"]["status"] == "queued"


def test_completion_is_rejected_while_task_is_running(tmp_path):
    state_dir, client = client_at(tmp_path)
    created = create_task(client)
    task_id = created["task"]["task_id"]
    TaskStore(state_dir / "triage-tasks").transition(
        task_id, TaskStatus.QUEUED, TaskStatus.RUNNING,
    )

    response = client.post(f"/api/triage-tasks/{task_id}/complete", json={})

    assert response.status_code == 409


def test_failed_manual_task_can_resume_by_message_or_retry(tmp_path):
    state_dir, client = client_at(tmp_path)
    store = TaskStore(state_dir / "triage-tasks")

    message_task = create_task(client)
    message_id = message_task["task"]["task_id"]
    store.transition(message_id, TaskStatus.QUEUED, TaskStatus.RUNNING)
    store.transition(
        message_id, TaskStatus.RUNNING, TaskStatus.FAILED, error="失败",
    )
    message_response = client.post(
        f"/api/triage-tasks/{message_id}/messages",
        json={"message": "继续"},
    )

    retry_task = create_task(client)
    retry_id = retry_task["task"]["task_id"]
    store.transition(retry_id, TaskStatus.QUEUED, TaskStatus.RUNNING)
    store.transition(
        retry_id, TaskStatus.RUNNING, TaskStatus.KB_FAILED, error="沉淀失败",
    )
    retry_response = client.post(
        f"/api/triage-tasks/{retry_id}/retry",
    )

    assert message_response.status_code == 200
    assert message_response.json()["task"]["status"] == "queued"
    assert retry_response.status_code == 200
    assert retry_response.json()["task"]["status"] == "queued"
    assert retry_response.json()["task"]["error"] is None


def test_unknown_conflict_and_invalid_payload_have_stable_statuses(tmp_path):
    state_dir, client = client_at(tmp_path)
    assert client.get("/api/triage-tasks/missing").status_code == 404
    assert client.post(
        "/api/triage-tasks",
        json={"message": "   ", "unexpected": True},
    ).status_code == 422

    created = create_task(client)
    task_id = created["task"]["task_id"]
    store = TaskStore(state_dir / "triage-tasks")
    store.transition(task_id, TaskStatus.QUEUED, TaskStatus.CANCELLED)
    assert client.post(
        f"/api/triage-tasks/{task_id}/messages",
        json={"message": "继续"},
    ).status_code == 409


def test_path_traversal_is_rejected_and_writes_stay_in_task_root(tmp_path):
    state_dir, client = client_at(tmp_path)
    before = files_below(tmp_path)

    created = create_task(client)
    task_id = created["task"]["task_id"]
    changed = files_below(tmp_path) - before

    assert changed
    assert all(path.startswith("state/triage-tasks/") for path in changed)
    assert client.get("/api/triage-tasks/%2E%2E%2Frecords").status_code in {
        404, 422,
    }
    assert not (state_dir / "records").exists()


def test_report_download_is_read_only_and_confined_to_records(tmp_path):
    state_dir, client = client_at(tmp_path)
    created = create_task(client)
    task_id = created["task"]["task_id"]
    store = TaskStore(state_dir / "triage-tasks")
    store.transition(task_id, TaskStatus.QUEUED, TaskStatus.RUNNING)
    report = tmp_path / "records" / "troubleshoot" / "ts-1" / "report.md"
    report.parent.mkdir(parents=True)
    report.write_text("# 排查报告\n", encoding="utf-8")
    store.update(
        task_id,
        expected=TaskStatus.RUNNING,
        report_path="records/troubleshoot/ts-1/report.md",
    )

    response = client.get(f"/api/triage-tasks/{task_id}/report")

    assert response.status_code == 200
    assert response.text == "# 排查报告\n"

    store.update(
        task_id,
        expected=TaskStatus.RUNNING,
        report_path="../outside.md",
    )
    assert client.get(
        f"/api/triage-tasks/{task_id}/report"
    ).status_code == 404


def test_task_artifacts_can_be_listed_previewed_and_downloaded(tmp_path):
    state_dir, client = client_at(tmp_path)
    created = create_task(client)
    task_id = created["task"]["task_id"]
    store = TaskStore(state_dir / "triage-tasks")
    store.transition(task_id, TaskStatus.QUEUED, TaskStatus.RUNNING)
    store.update(task_id, expected=TaskStatus.RUNNING, run_id="op-task-1")
    artifact = (
        tmp_path / "records" / "troubleshoot" / "op-task-1"
        / "artifacts" / "inventory.md"
    )
    artifact.parent.mkdir(parents=True)
    artifact.write_text("# 服务器现状\n", encoding="utf-8")

    listed = client.get(f"/api/triage-tasks/{task_id}/artifacts")
    preview = client.get(
        f"/api/triage-tasks/{task_id}/artifacts/inventory.md"
    )
    download = client.get(
        f"/api/triage-tasks/{task_id}/artifacts/inventory.md?download=true"
    )

    assert listed.status_code == 200
    assert listed.json()["items"][0]["filename"] == "inventory.md"
    assert preview.text == "# 服务器现状\n"
    assert "attachment" in download.headers["content-disposition"]


def _waiting_command_approval(state_dir, client):
    created = create_task(client)
    task_id = created["task"]["task_id"]
    store = TaskStore(state_dir / "triage-tasks")
    store.transition(task_id, TaskStatus.QUEUED, TaskStatus.RUNNING)
    store.append_event(
        task_id,
        "approval_requested",
        {
            "approval_id": "approval-command",
            "kind": "command",
            "signature": "printf",
        },
    )
    store.transition(
        task_id,
        TaskStatus.RUNNING,
        TaskStatus.WAITING_APPROVAL,
        lease_owner=None,
        lease_expires_at=None,
    )
    return task_id


def test_command_approval_scope_is_forwarded_and_strictly_validated(tmp_path):
    state_dir, client = client_at(tmp_path)
    task_id = _waiting_command_approval(state_dir, client)

    response = client.post(
        f"/api/triage-tasks/{task_id}/approvals/approval-command",
        json={"decision": "approve", "scope": "always"},
    )

    assert response.status_code == 200
    decided = response.json()["events"][-2]
    assert decided["type"] == "approval_decided"
    assert decided["payload"]["scope"] == "always"


def test_command_approval_scope_enum_and_strict_model_are_enforced(tmp_path):
    state_dir, client = client_at(tmp_path)
    task_id = _waiting_command_approval(state_dir, client)
    url = f"/api/triage-tasks/{task_id}/approvals/approval-command"

    assert client.post(url, json={"decision": "approve", "scope": "forever"}).status_code == 422
    assert client.post(url, json={"decision": "approve", "unexpected": True}).status_code == 422


def test_task_detail_carries_derived_resource_ref(tmp_path):
    _, client = client_at(tmp_path)

    on_node = create_task(client, platform="beta", node="198.51.100.102")
    assert on_node["task"]["node"] == "node2"
    assert on_node["target_ref"] == "example/beta/host/node2"
    task_id = on_node["task"]["task_id"]
    assert client.get(f"/api/triage-tasks/{task_id}").json()["target_ref"] == "example/beta/host/node2"

    shared = create_task(client, platform="shared", node="vm-app01")
    assert shared["target_ref"] == "example/misc/host/vm-app01"

    unscoped = create_task(client, message="随便看看集群")
    assert unscoped["task"]["platform"] is None
    assert unscoped["target_ref"] is None


def test_unregistered_platform_rejected(tmp_path):
    _, client = client_at(tmp_path)

    response = client.post("/api/triage-tasks", json={"message": "看看", "platform": "staging"})
    assert response.status_code == 422
