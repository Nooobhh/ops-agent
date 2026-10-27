from datetime import date, timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from time import sleep

from fastapi.testclient import TestClient

from web.server.app import create_app
from tests.envpack_helpers import as_env_pack


class FakeRegistry:
    def resolve(self, _source):
        return SimpleNamespace(
            model_name="fake", reasoning_effort="none", model_profile_id="fake",
        )

    def client_material_for_task(self, _task):
        return SimpleNamespace(
            base_url="http://model.invalid/v1", api_key="secret",
            timeout_seconds=30, max_tool_turns=10,
        )


def _client(tmp_path: Path) -> TestClient:
    template = tmp_path / "skills" / "weekly-report" / "assets" / "template.docx"
    template.parent.mkdir(parents=True)
    template.write_bytes(Path("envs/example/skills/weekly-report/assets/template.docx").read_bytes())
    worklog = tmp_path / "records" / "worklog"
    worklog.mkdir(parents=True)
    (worklog / "2026-08-20.md").write_text(
        "# 2026-08-20\n\n"
        "## [DONE] [OPS] 完成 HDFS 排查\n"
        "- **来源**: manual:alice\n"
        "- **产出**: report.md\n"
        "- **关联**: HDFS\n"
        "- **过程**: 1. 采证  2. 收口\n",
        encoding="utf-8",
    )
    as_env_pack(tmp_path)  # 周报周期取环境包配置:临时仓不放即走缺省(周四至下周三)
    return TestClient(create_app(state_dir=tmp_path / "state", repo_root=tmp_path))


def _wait_for_job(client: TestClient, response):
    assert response.status_code == 202
    job = response.json()
    for _ in range(100):
        current = client.get(f"/api/weekly-report-jobs/{job['job_id']}")
        assert current.status_code == 200
        body = current.json()
        if body["status"] in {"completed", "failed"}:
            return body
        sleep(0.01)
    raise AssertionError("周报后台任务未在测试期限内完成")


def test_worklogs_group_by_day_and_generate_previewable_docx(tmp_path, monkeypatch):
    from web.server import app as app_module

    monkeypatch.setattr(app_module.sys, "platform", "linux")
    monkeypatch.setattr(
        "web.server.routes.worklogs.draft_report_items",
        lambda *_args, **_kwargs: (
            [
                "每日完成Alpha集群、Beta集群、IoTDB及MySQL数据库的例行巡检，核心运行指标均处于正常范围；Webhook服务运行稳定，Nginx无异常告警，有效保障了平台基础服务的高可用性与连续性。",
                "完成 HDFS 排查并形成根因报告。",
            ],
            ["复核 HDFS 排查整改效果并形成验收记录。"],
        ),
    )
    with _client(tmp_path) as client:
        response = client.get(
            "/api/worklogs?start_date=2026-08-20&end_date=2026-08-26"
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body["days"]) == 7
        assert body["days"][0]["entries"][0]["title"] == "完成 HDFS 排查"

        created = client.post("/api/weekly-reports", json={
            "start_date": "2026-08-20", "end_date": "2026-08-26",
        })
        completed = _wait_for_job(client, created)
        report = completed["report"]
        assert report["filename"].endswith("20260820至20260826.docx")

        preview = client.get(
            f"/api/weekly-reports/{report['filename']}/preview"
        )
        assert preview.status_code == 200
        assert "完成 HDFS 排查" in "\n".join(preview.json()["paragraphs"])
        assert preview.json()["summary"][1].startswith("完成 HDFS 排查")
        assert preview.json()["project_name"] == "示例平台运维项目"
        assert preview.json()["project_manager"] == "张三"

        downloaded = client.get(f"/api/weekly-reports/{report['filename']}")
        assert downloaded.status_code == 200
        assert downloaded.content.startswith(b"PK")


def test_weekly_report_job_runs_in_background_and_exposes_stage(tmp_path, monkeypatch):
    from web.server import app as app_module

    started = Event()
    release = Event()

    def report_items(*_args, stage_callback=None, **_kwargs):
        assert stage_callback is not None
        stage_callback("事实聚合", "running")
        started.set()
        assert release.wait(timeout=2)
        stage_callback("事实聚合", "completed")
        return (
            [
                "每日完成Alpha集群、Beta集群、IoTDB及MySQL数据库的例行巡检，核心运行指标均处于正常范围；Webhook服务运行稳定，Nginx无异常告警，有效保障了平台基础服务的高可用性与连续性。",
                "完成 HDFS 后台任务生成验证。",
            ],
            ["复核 HDFS 周报生成结果并形成验收记录。"],
        )

    monkeypatch.setattr(app_module.sys, "platform", "linux")
    monkeypatch.setattr("web.server.routes.worklogs.draft_report_items", report_items)
    with _client(tmp_path) as client:
        created = client.post("/api/weekly-reports", json={
            "start_date": "2026-08-20", "end_date": "2026-08-26",
        })
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        assert started.wait(timeout=2)

        running = client.get(f"/api/weekly-report-jobs/{job_id}").json()
        assert running["status"] == "running"
        assert running["current_stage"] == "事实聚合"
        assert client.get(
            "/api/weekly-report-jobs?start_date=2026-08-20&end_date=2026-08-26"
        ).json()["items"][0]["job_id"] == job_id

        release.set()
        completed = _wait_for_job(client, created)

    assert completed["status"] == "completed"
    assert completed["completed_stages"] == ["事实聚合", "生成DOCX"]


def test_report_range_must_be_complete_thursday_week(tmp_path, monkeypatch):
    from web.server import app as app_module

    monkeypatch.setattr(app_module.sys, "platform", "linux")
    with _client(tmp_path) as client:
        response = client.post("/api/weekly-reports", json={
            "start_date": "2026-08-19", "end_date": "2026-08-25",
        })
        assert response.status_code == 400
        assert "周四至下周三" in response.json()["detail"]


def test_weekly_report_settings_control_window(tmp_path, monkeypatch):
    from web.server import app as app_module

    monkeypatch.setattr(app_module.sys, "platform", "linux")
    with _client(tmp_path) as client:
        updated = client.put("/api/weekly-report-settings", json={
            "start_weekday": 0, "end_weekday": 4,
        })
        assert updated.status_code == 200
        assert updated.json()["start_label"] == "周一"
        assert updated.json()["end_label"] == "周五"
        assert (tmp_path / "config" / "weekly-report.local.yaml").is_file()
        assert not (tmp_path / "config" / "weekly-report.yaml").exists()   # 不写环境包

        response = client.get(
            "/api/worklogs?start_date=2026-08-17&end_date=2026-08-21"
        )
        assert response.status_code == 200
        assert len(response.json()["days"]) == 5

        settings = updated.json()
        future_start = date.fromisoformat(settings["current_start_date"]) + timedelta(days=7)
        future_end = date.fromisoformat(settings["current_end_date"]) + timedelta(days=7)
        future = client.get(
            f"/api/worklogs?start_date={future_start}&end_date={future_end}"
        )
        assert future.status_code == 400
        assert "当前周期之后" in future.json()["detail"]


def test_weekly_reports_are_sorted_by_period_not_import_time(tmp_path, monkeypatch):
    from web.server import app as app_module

    monkeypatch.setattr(app_module.sys, "platform", "linux")
    root = tmp_path / "records" / "weekly-reports"
    root.mkdir(parents=True)
    older = root / "运维周报20260701至20260707.docx"
    newer = root / "运维周报20260801至20260807.docx"
    template = Path("envs/example/skills/weekly-report/assets/template.docx").read_bytes()
    newer.write_bytes(template)
    older.write_bytes(template)
    with _client(tmp_path) as client:
        items = client.get("/api/weekly-reports").json()["items"]
    assert items[0]["start_date"] == "2026-08-01"


def test_weekly_reports_return_all_items_by_default(tmp_path, monkeypatch):
    from web.server import app as app_module

    monkeypatch.setattr(app_module.sys, "platform", "linux")
    root = tmp_path / "records" / "weekly-reports"
    root.mkdir(parents=True)
    first_start = date(2024, 1, 4)
    for offset in range(105):
        start = first_start + timedelta(days=offset * 7)
        end = start + timedelta(days=6)
        filename = (
            "运维周报"
            f"{start:%Y%m%d}至{end:%Y%m%d}.docx"
        )
        (root / filename).touch()

    with _client(tmp_path) as client:
        all_items = client.get("/api/weekly-reports").json()["items"]
        limited_items = client.get("/api/weekly-reports?limit=5").json()["items"]

    assert len(all_items) == 105
    assert len(limited_items) == 5


def test_weekly_report_reveal_opens_containing_folder(tmp_path, monkeypatch):
    from web.server import app as app_module
    from web.server.routes import worklogs as worklogs_routes

    monkeypatch.setattr(app_module.sys, "platform", "linux")
    calls = []
    monkeypatch.setattr(worklogs_routes.subprocess, "Popen", lambda command, **_kwargs: calls.append(command))
    with _client(tmp_path) as client:
        filename = "运维周报20260820至20260826.docx"
        report_root = tmp_path / "records" / "weekly-reports"
        report_root.mkdir(parents=True)
        (report_root / filename).write_bytes(Path("envs/example/skills/weekly-report/assets/template.docx").read_bytes())
        response = client.post(f"/api/weekly-reports/{filename}/reveal")

    assert response.status_code == 200
    assert response.json()["filename"] == filename
    assert calls and calls[0][0] == "xdg-open"


def test_manual_worklog_append_validates_fields_and_date(tmp_path, monkeypatch):
    from web.server import app as app_module

    monkeypatch.setattr(app_module.sys, "platform", "linux")
    payload = {
        "date": "2026-08-20",
        "status": "DONE",
        "category": "MEETING",
        "title": "完成周报需求评审",
        "output": "评审纪要",
        "related": "周报页面",
        "process": ["整理需求", "完成评审", "记录结论"],
    }
    with _client(tmp_path) as client:
        created = client.post("/api/worklogs", json=payload)
        invalid_status = client.post(
            "/api/worklogs", json={**payload, "status": "INVALID"},
        )
        future = client.post(
            "/api/worklogs", json={**payload, "date": "2999-01-01"},
        )

    assert created.status_code == 201
    assert created.json()["entry"]["source"] == "manual:web"
    text = (tmp_path / "records" / "worklog" / "2026-08-20.md").read_text(
        encoding="utf-8",
    )
    assert "## [DONE] [MEETING] 完成周报需求评审" in text
    assert "1. 整理需求  2. 完成评审  3. 记录结论" in text
    assert invalid_status.status_code == 422
    assert future.status_code == 422
    assert "北京时间" in future.json()["detail"]


def test_manual_worklog_allows_omitted_process(tmp_path, monkeypatch):
    from web.server import app as app_module

    monkeypatch.setattr(app_module.sys, "platform", "linux")
    payload = {
        "date": "2026-08-20",
        "status": "DONE",
        "category": "OPS",
        "title": "手动记录无步骤工作",
        "output": "-",
        "related": "-",
    }
    with _client(tmp_path) as client:
        created = client.post("/api/worklogs", json=payload)
        listed = client.get(
            "/api/worklogs?start_date=2026-08-20&end_date=2026-08-26"
        )

    assert created.status_code == 201
    assert created.json()["entry"]["process"] == []
    text = (tmp_path / "records" / "worklog" / "2026-08-20.md").read_text(
        encoding="utf-8",
    )
    assert "- **过程**: -" in text
    assert listed.json()["days"][0]["entries"][-1]["process"] == []


def test_worklog_can_be_hidden_from_and_restored_to_weekly_report(tmp_path, monkeypatch):
    from web.server import app as app_module

    monkeypatch.setattr(app_module.sys, "platform", "linux")
    with _client(tmp_path) as client:
        hidden = client.put(
            "/api/worklogs/2026-08-20/0/weekly-report-exclusion",
            json={"excluded": True},
        )
        worklogs = client.get(
            "/api/worklogs?start_date=2026-08-20&end_date=2026-08-26"
        )
        restored = client.put(
            "/api/worklogs/2026-08-20/0/weekly-report-exclusion",
            json={"excluded": False},
        )

    assert hidden.status_code == 200
    assert hidden.json()["weekly_report_excluded"] is True
    entry = worklogs.json()["days"][0]["entries"][0]
    assert entry["entry_index"] == 0
    assert entry["weekly_report_excluded"] is True
    assert restored.status_code == 200
    assert restored.json()["weekly_report_excluded"] is False
    text = (tmp_path / "records" / "worklog" / "2026-08-20.md").read_text(
        encoding="utf-8",
    )
    assert "- **周报**: 忽略" not in text


def test_worklog_detail_includes_linked_troubleshoot_report(tmp_path, monkeypatch):
    from web.server import app as app_module

    monkeypatch.setattr(app_module.sys, "platform", "linux")
    worklog_path = tmp_path / "records" / "worklog" / "2026-08-20.md"
    report_path = (
        tmp_path / "records" / "troubleshoot" / "ts-detail-1" / "report.md"
    )
    with _client(tmp_path) as client:
        worklog_path.write_text(
            worklog_path.read_text(encoding="utf-8").replace(
                "manual:alice", "auto:troubleshoot:ts-detail-1",
            ),
            encoding="utf-8",
        )
        report_path.parent.mkdir(parents=True)
        report_path.write_text(
            "# 排查报告\n\n完整根因与采证内容。\n",
            encoding="utf-8",
        )
        detail = client.get("/api/worklogs/2026-08-20/0")

    assert detail.status_code == 200
    body = detail.json()
    assert body["entry"]["title"] == "完成 HDFS 排查"
    assert body["report_path"] == "records/troubleshoot/ts-detail-1/report.md"
    assert "完整根因与采证内容" in body["report_markdown"]


def test_worklog_detail_without_report_and_missing_entry(tmp_path, monkeypatch):
    from web.server import app as app_module

    monkeypatch.setattr(app_module.sys, "platform", "linux")
    with _client(tmp_path) as client:
        detail = client.get("/api/worklogs/2026-08-20/0")
        missing = client.get("/api/worklogs/2026-08-20/99")

    assert detail.status_code == 200
    assert detail.json()["report_path"] is None
    assert detail.json()["report_markdown"] is None
    assert missing.status_code == 404


def test_weekly_report_can_be_regenerated_and_deleted(tmp_path, monkeypatch):
    from web.server import app as app_module

    monkeypatch.setattr(app_module.sys, "platform", "linux")
    summary = {"value": "初次生成内容"}

    def report_items(*_args, **_kwargs):
        return ([
            "每日完成Alpha集群、Beta集群、IoTDB及MySQL数据库的例行巡检，核心运行指标均处于正常范围；Webhook服务运行稳定，Nginx无异常告警，有效保障了平台基础服务的高可用性与连续性。",
            summary["value"],
        ], ["复核周报内容并形成验收记录"])

    monkeypatch.setattr("web.server.routes.worklogs.draft_report_items", report_items)
    with _client(tmp_path) as client:
        created = client.post("/api/weekly-reports", json={
            "start_date": "2026-08-20", "end_date": "2026-08-26",
        })
        completed = _wait_for_job(client, created)
        filename = completed["report"]["filename"]
        summary["value"] = "重新生成后的内容"

        regenerated = client.put(f"/api/weekly-reports/{filename}", json={})
        _wait_for_job(client, regenerated)
        preview = client.get(f"/api/weekly-reports/{filename}/preview")
        deleted = client.delete(f"/api/weekly-reports/{filename}")
        missing = client.get(f"/api/weekly-reports/{filename}/preview")

    assert regenerated.status_code == 202
    assert "重新生成后的内容" in preview.json()["summary"]
    assert deleted.status_code == 200
    assert missing.status_code == 404
