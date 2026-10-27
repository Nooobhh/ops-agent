from pathlib import Path

import pytest

from lib.envpack import load_env_pack

from lib.data_root import (
    DATA_ROOT_ENV,
    LOCAL_CONFIG_NAMES,
    local_config_path,
    resolve_data_root,
)


def test_unset_data_root_is_repo_root(tmp_path, monkeypatch):
    monkeypatch.delenv(DATA_ROOT_ENV, raising=False)
    assert resolve_data_root(tmp_path) == tmp_path


def test_blank_data_root_is_repo_root(tmp_path, monkeypatch):
    monkeypatch.setenv(DATA_ROOT_ENV, "   ")
    assert resolve_data_root(tmp_path) == tmp_path


def test_absolute_data_root(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setenv(DATA_ROOT_ENV, str(data))
    assert resolve_data_root(tmp_path / "repo") == data.resolve()


def test_relative_data_root_resolves_against_repo_root(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv(DATA_ROOT_ENV, "../data")
    assert resolve_data_root(repo) == (tmp_path / "data").resolve()


def test_home_data_root_expands_user(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(DATA_ROOT_ENV, "~/ops-data")
    assert resolve_data_root(Path("/unused")) == (tmp_path / "ops-data").resolve()


@pytest.mark.parametrize("name", sorted(LOCAL_CONFIG_NAMES))
def test_local_config_under_data_root(tmp_path, monkeypatch, name):
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path / "data"))
    assert local_config_path(tmp_path / "repo", name) == (tmp_path / "data").resolve() / "config" / name


def test_local_config_without_data_root_stays_in_repo_config(tmp_path, monkeypatch):
    monkeypatch.delenv(DATA_ROOT_ENV, raising=False)
    assert local_config_path(tmp_path, "model.local.yaml") == tmp_path / "config" / "model.local.yaml"


@pytest.mark.parametrize("name", ["platform-alpha.yaml", "../credentials.json", "", "config/model.local.yaml"])
def test_local_config_rejects_unregistered_names(tmp_path, name):
    with pytest.raises(ValueError, match="不是登记的本机私有配置"):
        local_config_path(tmp_path, name)


def test_knowledge_root_defaults_under_data_root(tmp_path, monkeypatch):
    from lib.kb.bases import KNOWLEDGE_ROOT_ENV, resolve_knowledge_root

    monkeypatch.delenv(KNOWLEDGE_ROOT_ENV, raising=False)
    repo, data = tmp_path / "repo", tmp_path / "data"
    (repo / "config").mkdir(parents=True)
    # 仓库根下的 paths.local 不再生效:私有配置随数据根走
    (repo / "config" / "paths.local.yaml").write_text("knowledge_root: /elsewhere\n", encoding="utf-8")
    monkeypatch.setenv(DATA_ROOT_ENV, str(data))
    assert resolve_knowledge_root(repo) == data.resolve() / "knowledge"


def test_knowledge_root_paths_local_relative_to_data_root(tmp_path, monkeypatch):
    from lib.kb.bases import KNOWLEDGE_ROOT_ENV, resolve_knowledge_root

    monkeypatch.delenv(KNOWLEDGE_ROOT_ENV, raising=False)
    data = tmp_path / "data"
    (data / "config").mkdir(parents=True)
    (data / "config" / "paths.local.yaml").write_text("knowledge_root: ../kb\n", encoding="utf-8")
    monkeypatch.setenv(DATA_ROOT_ENV, str(data))
    assert resolve_knowledge_root(tmp_path / "repo") == (tmp_path / "kb").resolve()


def test_app_paths_runtime_fields_under_data_root(tmp_path, monkeypatch):
    from lib.kb.bases import KNOWLEDGE_ROOT_ENV
    from lib.triage_agent.paths import AppPaths

    monkeypatch.delenv(KNOWLEDGE_ROOT_ENV, raising=False)
    repo, data = tmp_path / "repo", (tmp_path / "data").resolve()
    monkeypatch.setenv(DATA_ROOT_ENV, str(data))
    paths = AppPaths.from_repo_root(repo)
    assert paths.repo_root == repo.resolve()
    assert paths.state_dir == data / "state"
    assert paths.tasks_root == data / "state" / "triage-tasks"
    assert paths.records_root == data / "records" / "troubleshoot"
    assert paths.worklog_root == data / "records" / "worklog"
    assert paths.knowledge_root == data / "knowledge"
    assert paths.routing_path == data / "build" / "routing.json"
    assert paths.recall_index_path == data / "build" / "kb-recall.sqlite"
    assert paths.retrieval_index_path == data / "build" / "kb-retrieval.sqlite"
    assert paths.model_config == data / "config" / "model.local.yaml"
    # 环境数据随环境包(临时仓未配 env_pack,取代码仓 envs/ 下唯一包)
    pack = load_env_pack(repo)
    assert paths.diagnostics_config == pack.config_file("agent-diagnostics.yaml")
    assert paths.log_catalog_path == pack.config_file("log-catalog.yaml")


def test_app_paths_explicit_state_dir_wins(tmp_path, monkeypatch):
    from lib.triage_agent.paths import AppPaths

    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path / "data"))
    paths = AppPaths.from_repo_root(tmp_path / "repo", state_dir=tmp_path / "custom-state")
    assert paths.state_dir == (tmp_path / "custom-state").resolve()


def test_acquisition_storage_defaults_under_data_root(tmp_path, monkeypatch):
    from lib.acquisition.settings import load_settings, path_status, resolve_storage_path

    repo, data = tmp_path / "repo", tmp_path / "data"
    monkeypatch.setenv(DATA_ROOT_ENV, str(data))
    settings = load_settings(data / "state")
    alerts = resolve_storage_path(repo, settings["alerts"]["history_root"])
    resources = resolve_storage_path(repo, settings["resources"]["snapshot_root"])
    assert alerts == data.resolve() / "records" / "alerts"
    assert resources == data.resolve() / "records" / "telemetry" / "resources"
    assert path_status(repo, "records/alerts")["resolved"] == str(alerts)
    # 绝对路径照旧原样使用
    assert resolve_storage_path(repo, str(tmp_path / "elsewhere")) == (tmp_path / "elsewhere").resolve()


def test_ambient_data_root_keeps_cwd_relative_when_unset(monkeypatch):
    from lib.data_root import ambient_data_root

    monkeypatch.delenv(DATA_ROOT_ENV, raising=False)
    assert ambient_data_root() / "records" / "worklog" == Path("records/worklog")


def test_worklog_default_root_follows_data_root(tmp_path, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from lib.datastore import open_datastore
    from lib.worklog import WorklogEntry

    monkeypatch.chdir(tmp_path)
    data = tmp_path / "data"
    monkeypatch.setenv(DATA_ROOT_ENV, str(data))
    entry = WorklogEntry(
        status="DONE", category="OPS", title="t", source="manual:t", output="-",
        related="-", process=["p"], ts=datetime(2026, 10, 21, 9, tzinfo=ZoneInfo("Etc/GMT-8")),
    )
    path = open_datastore().worklogs.append(entry)
    assert path == data.resolve() / "records" / "worklog" / "2026-10-21.md"
    assert not (tmp_path / "records").exists()


def test_package_index_stamp_lands_in_data_root(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from lib.kb.bases import KNOWLEDGE_ROOT_ENV
    from lib.apphost.prepare import INDEX_STAMP, prepare_package

    monkeypatch.delenv(KNOWLEDGE_ROOT_ENV, raising=False)
    repo, data = tmp_path / "repo", tmp_path / "data"
    repo.mkdir()
    (repo / "package-info.json").write_text(json.dumps({"version": "1.0.0", "commit": "abc"}), encoding="utf-8")
    monkeypatch.setenv(DATA_ROOT_ENV, str(data))

    assert prepare_package(repo, "py", tmp_path / "log", run=lambda *a, **k: SimpleNamespace(returncode=0)) is None
    assert (data / INDEX_STAMP).read_text(encoding="utf-8") == "1.0.0+abc"
    assert not (repo / "knowledge").exists() and not (repo / "build").exists()


def test_records_root_defaults_under_data_root(tmp_path, monkeypatch):
    from lib.data_root import resolve_records_root

    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path / "data"))
    assert resolve_records_root(tmp_path / "repo") == (tmp_path / "data").resolve() / "records"


def test_records_root_from_paths_local(tmp_path, monkeypatch):
    from lib.data_root import records_root_for, resolve_records_root

    data = tmp_path / "data"
    (data / "config").mkdir(parents=True)
    (data / "config" / "paths.local.yaml").write_text(
        "knowledge_root: ../kb\nrecords_root: ../records\n", encoding="utf-8")
    monkeypatch.setenv(DATA_ROOT_ENV, str(data))
    assert resolve_records_root(tmp_path / "repo") == (tmp_path / "records").resolve()
    (data / "config" / "paths.local.yaml").write_text(
        f"records_root: {tmp_path / 'abs'}\n", encoding="utf-8")
    assert records_root_for(data) == (tmp_path / "abs").resolve()


def test_records_root_rejects_non_mapping_paths_local(tmp_path):
    from lib.data_root import records_root_for

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "paths.local.yaml").write_text("- x\n", encoding="utf-8")
    with pytest.raises(ValueError, match="顶层必须是映射"):
        records_root_for(tmp_path)
