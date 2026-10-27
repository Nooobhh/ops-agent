"""作者贯通:CLI 与 Agent 写入的排查记录/worklog 带作者,缺省本机用户。"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from lib.data_root import DATA_ROOT_ENV
from lib.datastore.local import LocalDataStore
from lib.triage_agent.actions import ActionGateway
from lib.triage_agent.paths import AppPaths

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))


def test_ts_session_cli_writes_local_user(tmp_path, monkeypatch, capsys):
    import ts_session

    monkeypatch.chdir(tmp_path)
    LocalDataStore(tmp_path).users.save_local_user("alice", "Haze")
    assert ts_session.run(["start", "--run-id", "ts-cli", "--node", "node1", "--trigger", "manual:x",
                           "--platform", "alpha", "--service", "hdfs"]) == 0
    capsys.readouterr()
    data = json.loads((tmp_path / "records" / "troubleshoot" / "ts-cli" / "session.json").read_text(encoding="utf-8"))
    assert data["author"] == "alice"


def test_agent_records_use_task_creator_then_owner(tmp_path, monkeypatch):
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path / "data"))
    paths = AppPaths.from_repo_root(tmp_path / "repo")
    gateway = ActionGateway(paths, SimpleNamespace())
    gateway.datastore.users.save_local_user("alice", "Haze")

    gateway.start_session(platform="alpha", node="node1", trigger="manual:x", run_id="ts-a", author="bob")
    gateway.start_workspace(task_id="t1", trigger="聊聊")
    assert gateway.datastore.troubleshoot.session("ts-a")["author"] == "bob"
    assert gateway.datastore.troubleshoot.session("op-t1")["author"] == "alice"

    entries = [{"category": "OPS", "title": "处理告警", "process": ["采证"]}]
    preview = gateway.render_worklog_entries(entries, status="DONE", source_prefix="auto:agent-ops:t1:e1",
                                             author="bob")
    assert "- **作者**: bob" in preview
    written = gateway.commit_worklog_entries(entries, status="DONE", source_prefix="auto:agent-ops:t1:e1",
                                             author="bob")
    text = (paths.worklog_root / written[0].rsplit("/", 1)[-1]).read_text(encoding="utf-8")
    assert "- **作者**: bob" in text
