"""数据层契约:按实现参数化,二阶段的同步/远端实现须跑同一套。

覆盖新旧两种落盘格式:worklog 有/无作者行、session.json 有/无 author 键。
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from lib.data_root import DATA_ROOT_ENV
from lib.datastore import DataStoreError, open_datastore
from lib.datastore.local import LocalDataStore
from lib.worklog import WorklogEntry

DAY = date(2026, 10, 20)
TS = datetime(2026, 10, 20, 12, tzinfo=ZoneInfo("Etc/GMT-8"))
LEGACY_DAY = (
    "# 2026-10-20\n\n"
    "## [DONE] [OPS] 旧条目\n"
    "- **来源**: manual:web\n"
    "- **产出**: -\n"
    "- **关联**: -\n"
    "- **过程**: 1. 处理\n\n"
)


@pytest.fixture(params=["local"])
def store(request, tmp_path):
    if request.param == "local":
        return LocalDataStore(tmp_path / "data")
    raise AssertionError(request.param)


def _entry(title="新条目", source="manual:web", process=("处理",)):
    return WorklogEntry(
        status="DONE", category="OPS", title=title, source=source,
        output="-", related="-", process=list(process), ts=TS,
    )


def test_users_unconfigured(store):
    assert store.users.local_user() is None
    assert store.users.owner_id() is None
    assert store.users.resolve(None) is None
    other = store.users.resolve("bob")
    assert (other.id, other.display_name, other.inferred) == ("bob", "bob", False)


def test_users_save_and_resolve_owner(store):
    saved = store.users.save_local_user("alice", "  Haze  ")
    assert (saved.id, saved.display_name) == ("alice", "Haze")
    assert store.users.local_user() == saved
    inferred = store.users.resolve(None)
    assert (inferred.id, inferred.display_name, inferred.inferred) == ("alice", "Haze", True)
    assert store.users.resolve("alice").inferred is False
    assert store.users.save_local_user("bob", "").display_name == "bob"


@pytest.mark.parametrize("bad", ["", "Haze", "-x", "a b", "a/b", "x" * 65])
def test_users_reject_invalid_id(store, bad):
    with pytest.raises(DataStoreError):
        store.users.save_local_user(bad, "x")


def test_users_malformed_config_reads_as_unconfigured(store):
    path = store.users.path
    path.parent.mkdir(parents=True)
    path.write_text("id: [broken\n", encoding="utf-8")
    user, error = store.users.load()
    assert user is None and "无效" in error
    assert store.users.owner_id() is None


def test_worklog_without_owner_writes_no_author_line(store):
    path = store.worklogs.append(_entry())
    assert "作者" not in path.read_text(encoding="utf-8")
    entry = store.worklogs.get(DAY, 0)
    assert entry["author"] is None and entry["author_inferred"] is True


def test_worklog_owner_written_and_legacy_inferred(store):
    store.users.save_local_user("alice", "Haze")
    store.worklogs.root.mkdir(parents=True)
    store.worklogs.path_for(DAY).write_text(LEGACY_DAY, encoding="utf-8")
    path = store.worklogs.append(_entry())
    assert "- **作者**: alice" in path.read_text(encoding="utf-8")
    legacy, current = store.worklogs.days(DAY, DAY)[0]["entries"]
    assert (legacy["author"], legacy["author_inferred"]) == ("alice", True)
    assert (current["author"], current["author_name"], current["author_inferred"]) == ("alice", "Haze", False)


def test_worklog_raw_excludes_author_line(store):
    store.users.save_local_user("alice", "Haze")
    store.worklogs.append(_entry(title="旧条目"))
    authored = store.worklogs.get(DAY, 0)
    legacy_root = store.worklogs.root.parent / "legacy"
    legacy_root.mkdir()
    (legacy_root / "2026-10-20.md").write_text(LEGACY_DAY, encoding="utf-8")
    legacy = LocalDataStore(store.worklogs.root.parent.parent, worklog_root=legacy_root).worklogs.get(DAY, 0)
    assert authored["raw"] == legacy["raw"]


def test_worklog_explicit_author_and_filter_keep_entry_index(store):
    store.users.save_local_user("alice", "Haze")
    store.worklogs.append(_entry(title="a"))
    store.worklogs.append(_entry(title="b"), author="bob")
    entries = store.worklogs.days(DAY, DAY, author_id="bob")[0]["entries"]
    assert [(item["title"], item["entry_index"], item["author_name"]) for item in entries] == [("b", 1, "bob")]


def test_worklog_event_key_idempotent(store):
    assert store.worklogs.append(_entry(source="auto:x"), event_key="auto:x") is not None
    assert store.worklogs.append(_entry(source="auto:x"), event_key="auto:x") is None
    assert len(store.worklogs.days(DAY, DAY)[0]["entries"]) == 1


def test_worklog_weekly_exclusion_keeps_author(store):
    store.users.save_local_user("alice", "Haze")
    store.worklogs.append(_entry())
    assert store.worklogs.set_weekly_excluded(DAY, 0, True) is True
    entry = store.worklogs.get(DAY, 0)
    assert entry["weekly_report_excluded"] is True and entry["author"] == "alice"
    with pytest.raises(IndexError):
        store.worklogs.get(DAY, 5)


def test_worklog_days_window_marks_missing_days(store):
    days = store.worklogs.days(DAY, date(2026, 10, 22))
    assert [(item["date"], item["weekday"], item["available"]) for item in days] == [
        ("2026-10-20", "二", False), ("2026-10-21", "三", False), ("2026-10-22", "四", False),
    ]


def _session_fields():
    return {"node": "node1", "trigger": "manual:检查", "platform": "alpha", "service": "hdfs"}


def test_troubleshoot_start_writes_owner_and_reads_back(store):
    store.users.save_local_user("alice", "Haze")
    session = store.troubleshoot.start("ts-1", **_session_fields())
    assert session.dir == store.troubleshoot.run_dir("ts-1")
    assert store.troubleshoot.session("ts-1")["author"] == "alice"
    assert store.troubleshoot.start("ts-2", author="bob", **_session_fields()) is not None
    assert store.troubleshoot.session("ts-2")["author"] == "bob"
    assert [item["run_id"] for item in store.troubleshoot.list(author_id="bob")] == ["ts-2"]


def test_troubleshoot_legacy_session_without_author_is_inferred(store):
    store.troubleshoot.start("ts-old", **_session_fields())
    assert "author" not in store.troubleshoot.session("ts-old")
    (item,) = store.troubleshoot.list()
    assert item["author"] is None and item["author_inferred"] is True
    store.users.save_local_user("alice", "Haze")
    (item,) = store.troubleshoot.list()
    assert (item["author"], item["author_inferred"]) == ("alice", True)


def test_troubleshoot_report_and_ref(store):
    store.troubleshoot.start("ts-1", **_session_fields())
    assert store.troubleshoot.report("ts-1") is None
    (store.troubleshoot.run_dir("ts-1") / "report.md").write_text("# r\n", encoding="utf-8")
    assert store.troubleshoot.report("ts-1") == "# r\n"
    assert store.troubleshoot.report_ref("ts-1") == "records/troubleshoot/ts-1/report.md"
    assert store.troubleshoot.open("ts-1").run_id == "ts-1"


@pytest.mark.parametrize("bad", ["../x", "a/b", "", ".hidden", "x..y"])
def test_troubleshoot_rejects_unsafe_run_id(store, bad):
    with pytest.raises(DataStoreError):
        store.troubleshoot.run_dir(bad)


def test_work_items_round_trip(store):
    item = store.work_items.create({"title": "x"}, author="alice")
    assert store.work_items.get(item["id"])["title"] == "x"
    assert [entry["id"] for entry in store.work_items.list()] == [item["id"]]


def test_open_datastore_follows_data_root(tmp_path, monkeypatch):
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path / "data"))
    store = open_datastore(tmp_path / "repo")
    assert store.worklogs.root == (tmp_path / "data").resolve() / "records" / "worklog"
    assert store.users.path == (tmp_path / "data").resolve() / "config" / "user.local.yaml"


def test_open_datastore_ambient_keeps_cwd_relative(tmp_path, monkeypatch):
    monkeypatch.delenv(DATA_ROOT_ENV, raising=False)
    monkeypatch.chdir(tmp_path)  # 不受仓库里 paths.local.yaml 的 records_root 影响
    store = open_datastore()
    assert store.troubleshoot.root == Path("records/troubleshoot")
    assert store.users.path == Path("config/user.local.yaml")


def test_session_author_lint(tmp_path):
    from lib.record import lint_records

    store = LocalDataStore(tmp_path)
    store.troubleshoot.start("ts-1", author="alice", **_session_fields())
    store.troubleshoot.start("ts-2", **_session_fields())
    assert lint_records(store.troubleshoot.root) == []
    path = store.troubleshoot.run_dir("ts-2") / "session.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**data, "author": "Bad Id"}), encoding="utf-8")
    assert ("ts-2", "session.json author 必须是合法用户 ID") in lint_records(store.troubleshoot.root)
