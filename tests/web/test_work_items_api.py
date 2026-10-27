"""工作项 API:建项、编辑、挂载(任务/排查记录/worklog)、派生归属、导入计划、周期分桶。"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from lib.datastore import open_datastore
from lib.triage_agent.task_store import TaskStore
from web.server.app import create_app

SHEET = (
    "项目节点\t完成状态\t具体事项\t开始时间\t截止时间\n"
    "依赖关系解耦\tFALSE\t全链路血缘元数据接入\t2026/10/11\t2026/10/14\n"
    "\tFALSE\t解耦剥离方案架构评审\t2026/10/19\t2026/10/23\n"
    "\tFALSE\t依赖解耦分批灰度切流\t2026/10/26\t2026/10/28\n"
    "产物验证\tFALSE\t长流程任务平均等待时间下降\t\t\n"
)


def _client(tmp_path: Path) -> TestClient:
    datastore = open_datastore(tmp_path)
    datastore.users.save_local_user("alice", "Haze")
    return TestClient(create_app(state_dir=tmp_path / "state", repo_root=tmp_path))


def _task_with_run(tmp_path: Path, run_id: str = "ts-20261022-1000-abcd") -> str:
    store = TaskStore(tmp_path / "state" / "triage-tasks")
    task = store.create_manual("node3 DataNode 掉线", created_by="alice")
    store.update(task.task_id, expected=task.status, run_id=run_id)
    datastore = open_datastore(tmp_path)
    datastore.troubleshoot.start(run_id, node="node3", trigger="DataNode 掉线", platform="alpha", service="hdfs")
    run_dir = datastore.troubleshoot.run_dir(run_id)
    (run_dir / "report.md").write_text("# r\n", encoding="utf-8")
    (run_dir / "artifacts").mkdir()
    (run_dir / "artifacts" / "plan.md").write_text("x", encoding="utf-8")
    session = json.loads((run_dir / "session.json").read_text(encoding="utf-8"))
    session["write_operations"] = [{"id": "ap-1", "kind": "plugin", "title": "重启 DataNode",
                                    "outcome": "succeeded", "approver": "alice", "arguments": {"x": 1}}]
    (run_dir / "session.json").write_text(json.dumps(session, ensure_ascii=False), encoding="utf-8")
    store.append_event(task.task_id, "approval_requested", {"approval_id": "ap-1", "kind": "operation"})
    store.append_event(task.task_id, "approval_decided", {
        "approval_id": "ap-1", "decision": "approve", "decided_by": {"id": "alice", "name": "Haze"},
    })
    return task.task_id


def test_create_edit_and_list(tmp_path):
    client = _client(tmp_path)
    created = client.post("/api/work-items", json={"title": "证书续期", "due_date": "2026-10-28"})
    assert created.status_code == 201, created.text
    item = created.json()["item"]
    assert (item["assignee"], item["created_by"]) == ("alice", "alice")
    patched = client.patch(f"/api/work-items/{item['id']}", json={"status": "done", "note": "已续"})
    assert patched.json()["item"]["closed_at"]
    assert client.get("/api/work-items").json()["items"] == []
    listed = client.get("/api/work-items", params={"include_closed": "true"}).json()["items"]
    assert [entry["id"] for entry in listed] == [item["id"]] and "history" not in listed[0]
    assert client.post("/api/work-items", json={"title": "x", "source": "plan"}).status_code == 422
    assert client.post("/api/work-items", json={"title": "x", "due_date": "bad"}).status_code == 422
    assert client.get("/api/work-items/wi-20261023-000000").status_code == 404


def test_alert_item_conflict_returns_owner(tmp_path):
    client = _client(tmp_path)
    body = {"title": "NodeDown@node3", "source": "alert", "source_ref": "prometheus:alpha:NodeDown:node3"}
    first = client.post("/api/work-items", json=body).json()["item"]
    second = client.post("/api/work-items", json=body)
    assert second.status_code == 409 and second.json()["detail"]["owner_id"] == first["id"]


def test_task_link_resolves_report_artifacts_approvals_and_worklogs(tmp_path):
    client = _client(tmp_path)
    task_id = _task_with_run(tmp_path)
    item = client.post("/api/work-items", json={"title": "HDFS DataNode 掉线"}).json()["item"]
    assert client.post(f"/api/work-items/{item['id']}/links",
                       json={"kind": "task", "ref": "tr-missing"}).status_code == 404
    assert client.post(f"/api/work-items/{item['id']}/links",
                       json={"kind": "task", "ref": "../../x"}).status_code == 422
    linked = client.post(f"/api/work-items/{item['id']}/links", json={"kind": "task", "ref": task_id})
    assert linked.status_code == 201, linked.text
    other = client.post("/api/work-items", json={"title": "另一件"}).json()["item"]
    clash = client.post(f"/api/work-items/{other['id']}/links", json={"kind": "task", "ref": task_id})
    assert clash.status_code == 409

    # 任务产生的 worklog 自动归属;手工日志写入时显式挂
    datastore = open_datastore(tmp_path)
    worklog = datastore.worklogs.root / "2026-10-22.md"
    worklog.parent.mkdir(parents=True, exist_ok=True)
    worklog.write_text(
        "# 2026-10-22\n\n"
        "## [DONE] [OPS] DataNode 恢复\n- **来源**: auto:troubleshoot:ts-20261022-1000-abcd\n"
        "- **产出**: -\n- **关联**: -\n- **过程**: 1. x\n\n", encoding="utf-8")
    written = client.post("/api/worklogs", json={
        "date": "2026-10-22", "status": "DONE", "category": "OPS", "title": "补录:通知业务方",
        "work_item_id": item["id"],
    })
    assert written.status_code == 201 and written.json()["worklog_ref"] == "2026-10-22#1"

    detail = client.get(f"/api/work-items/{item['id']}").json()
    (task,) = detail["links"]["tasks"]
    assert task["report_ref"] == "records/troubleshoot/ts-20261022-1000-abcd/report.md"
    assert task["artifacts"] == ["plan.md"]
    assert task["approvals"][0]["decision"] == "approve" and task["approvals"][0]["decided_by"] == "Haze"
    assert task["write_operations"] == [{"id": "ap-1", "kind": "plugin", "title": "重启 DataNode",
                                         "outcome": "succeeded", "approver": "alice",
                                         "decided_at": None, "oplog": None}]
    assert [(entry["ref"], entry["derived"]) for entry in detail["links"]["worklogs"]] == [
        ("2026-10-22#0", True), ("2026-10-22#1", False)]

    mapping = client.get("/api/work-items/worklog-map",
                         params={"start_date": "2026-10-22", "end_date": "2026-10-28"}).json()["refs"]
    assert set(mapping) == {"2026-10-22#0", "2026-10-22#1"}
    owner = client.get("/api/work-items/by-link", params={"kind": "worklog", "ref": "2026-10-22#0"}).json()
    assert owner["item"]["id"] == item["id"] and owner["derived"] is True
    owner = client.get("/api/work-items/by-link", params={"kind": "task", "ref": task_id}).json()
    assert owner["item"]["id"] == item["id"]

    removed = client.delete(f"/api/work-items/{item['id']}/links", params={"kind": "task", "ref": task_id})
    assert removed.status_code == 200 and removed.json()["item"]["history"][-1]["action"] == "unlinked"


def test_task_created_from_work_item_is_linked(tmp_path, monkeypatch):
    from lib.data_root import local_config_path
    from lib.triage_agent.model_registry import ModelRegistry, ModelUpsert
    from web.server import app as app_module

    class Offline:
        def __init__(self, *args, **kwargs):
            pass

        def complete(self, *args, **kwargs):
            raise RuntimeError("测试不外呼模型")

    ModelRegistry(local_config_path(tmp_path, "model.local.yaml")).create_model(ModelUpsert(
        title="M", base_url="https://m.test/v1", api_key="k", model="m", default_reasoning_effort="medium",
    ))
    monkeypatch.setattr(app_module, "OpenAICompatibleClient", Offline)
    client = _client(tmp_path)
    item = client.post("/api/work-items", json={"title": "排查 node1 内存"}).json()["item"]
    missing = client.post("/api/triage-tasks", params={"work_item_id": "wi-20261023-000000"},
                          json={"message": "整体写入变慢,先看看情况"})
    assert missing.status_code == 404
    created = client.post("/api/triage-tasks", params={"work_item_id": item["id"]},
                          json={"message": "整体写入变慢,先看看情况"})
    assert created.status_code == 201, created.text
    task_id = created.json()["task"]["task_id"]
    assert client.get(f"/api/work-items/{item['id']}").json()["item"]["links"][0]["ref"] == task_id


def test_import_plan_and_weekly_buckets(tmp_path):
    client = _client(tmp_path)
    assert client.post("/api/work-item-imports/preview", json={"text": SHEET}).status_code == 422
    preview = client.post("/api/work-item-imports/preview", json={"text": SHEET, "plan_title": "下半年治理"})
    assert preview.status_code == 200 and preview.json()["create"] == 4
    assert client.get("/api/work-item-plans").json()["plans"] == []
    applied = client.post("/api/work-item-imports", json={"text": SHEET, "plan_title": "下半年治理"})
    assert applied.status_code == 201, applied.text
    plan_id = applied.json()["plan"]["id"]

    plans = client.get("/api/work-item-plans").json()["plans"]
    assert plans[0]["progress"]["total"] == 4
    detail = client.get(f"/api/work-item-plans/{plan_id}").json()
    (node,) = detail["nodes"]
    assert node["name"] == "依赖关系解耦"
    assert [item["kind"] for item in node["items"]] == ["task", "task", "task", "acceptance"]

    weekly = client.get("/api/work-items/weekly", params={"start_date": "2026-10-15", "end_date": "2026-10-21"})
    buckets = {entry["item"]["title"]: entry["bucket"] for entry in weekly.json()["items"]}
    assert buckets == {
        "全链路血缘元数据接入": "open",
        "解耦剥离方案架构评审": "in_progress",
        "依赖解耦分批灰度切流": "planned",
        "长流程任务平均等待时间下降": "open",
    }
    assert client.get("/api/work-items/weekly",
                      params={"start_date": "2026-10-21", "end_date": "2026-10-15"}).status_code == 422

    again = client.post("/api/work-item-imports", json={"text": SHEET, "plan_id": plan_id})
    assert again.json()["unchanged"] == 4


def test_match_suggestions_returns_only_suggestions(tmp_path, monkeypatch):
    from web.server.routes import work_items as module

    client = _client(tmp_path)
    datastore = open_datastore(tmp_path)
    worklog = datastore.worklogs.root / "2026-10-20.md"
    worklog.parent.mkdir(parents=True, exist_ok=True)
    worklog.write_text("# 2026-10-20\n\n## [DONE] [OPS] 修复复制断链\n- **来源**: manual:web\n"
                       "- **产出**: -\n- **关联**: -\n- **过程**: -\n\n", encoding="utf-8")
    params = {"start_date": "2026-10-15", "end_date": "2026-10-21"}
    assert client.post("/api/work-items/match-suggestions", json=params).json()["suggestions"] == []
    item = client.post("/api/work-items", json={"title": "MySQL 复制修复"}).json()["item"]
    reply = json.dumps({"matches": [{"worklog_ref": "2026-10-20#0", "work_item_id": item["id"], "reason": "同一件事"}]})

    class Fake:
        def complete(self, messages, tools):
            from types import SimpleNamespace
            return SimpleNamespace(content=reply)

    monkeypatch.setattr(module, "_match_client", lambda _registry: Fake())
    found = client.post("/api/work-items/match-suggestions", json=params).json()
    assert found["suggestions"][0]["work_item_id"] == item["id"] and found["unassigned"] == 1
    assert open_datastore(tmp_path).work_items.get(item["id"])["links"] == []  # 只建议,不写
