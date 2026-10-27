"""scripts/worklog_add.py:log-work skill 的唯一写入口,作者缺省本机用户。"""
from __future__ import annotations

import json

from lib.data_root import DATA_ROOT_ENV
from lib.datastore import open_datastore
from scripts import worklog_add


def _run(capsys, *args):
    rc = worklog_add.run(list(args))
    return rc, json.loads(capsys.readouterr().out)


def test_add_writes_entry_with_local_user(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))
    open_datastore(tmp_path).users.save_local_user("alice", "Haze")
    rc, out = _run(capsys, "--title", "评审 1.0 方案", "--status", "DONE", "--category", "MEETING",
                   "--source", "manual:会议", "--process", "过方案", "--process", "定里程碑",
                   "--date", "2026-10-20")
    assert rc == 0 and out["written"] is True and out["author"] == "alice"
    text = (tmp_path / "records" / "worklog" / "2026-10-20.md").read_text(encoding="utf-8")
    assert "## [DONE] [MEETING] 评审 1.0 方案" in text
    assert "- **作者**: alice" in text and "1. 过方案  2. 定里程碑" in text


def test_dry_run_and_validation(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))
    rc, out = _run(capsys, "--title", "x", "--status", "WIP", "--category", "OPS", "--dry-run")
    assert rc == 0 and out["written"] is False and "## [WIP] [OPS] x" in out["preview"]
    assert not (tmp_path / "records").exists()
    rc, out = _run(capsys, "--title", "x", "--status", "WIP", "--category", "OPS", "--date", "2999-01-01")
    assert rc == 1 and out["error"] == "bad_args"
    rc, out = _run(capsys, "--title", "x", "--status", "WIP", "--category", "OPS", "--author", "Bad Id")
    assert rc == 1 and out["error"] == "bad_args"


def test_add_links_work_item(tmp_path, monkeypatch, capsys):
    from scripts import work_items as work_items_cli

    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))
    store = open_datastore(tmp_path).work_items
    item = store.create({"title": "证书续期", "start_date": "2026-10-19", "due_date": "2026-10-23"})
    store.create({"title": "下月演练", "due_date": "2026-11-20"})
    assert work_items_cli.run(["list", "--date", "2026-10-20"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [entry["id"] for entry in listed["items"]] == [item["id"]]

    rc, out = _run(capsys, "--title", "续期完成", "--status", "DONE", "--category", "OPS",
                   "--date", "2026-10-20", "--work-item", item["id"])
    assert rc == 0 and out["worklog_ref"] == "2026-10-20#0"
    assert store.find_link("worklog", "2026-10-20#0")["id"] == item["id"]
    rc, out = _run(capsys, "--title", "x", "--status", "DONE", "--category", "OPS",
                   "--work-item", "wi-20261020-000000")
    assert rc == 1 and out["error"] == "bad_work_item"
