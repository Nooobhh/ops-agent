import json
from pathlib import Path

import pytest

from scripts.patrol_runner import run_patrol_once, PatrolResult


class FakePool:
    def __init__(self, playbook_stdout: str):
        self.stdout = playbook_stdout
        self.cmds = []
        self.pushes = []

    def run(self, node, cmd, timeout=180.0, approved=False):
        from lib.ssh_pool import CmdResult
        self.cmds.append(cmd)
        stdout = self.stdout if "bash /tmp/opsagent/" in cmd else ""
        return CmdResult(node, cmd, 0, stdout, "", 0.1, timed_out=False)

    def push(self, node, local, remote):
        self.pushes.append((str(local), remote))


VALID_JSON = ('{"playbook":"alpha/patrol/node_health","node":"node12",'
              '"verdict":"needs_llm","severity":"ok","conclusion_hint":"ok",'
              '"steps":[{"name":"mem","cmd":"free -m","expect":"<90%","actual":"85%","pass":true,"raw":""}],'
              '"metrics":{"mem_pct":85}}')


def test_run_patrol_once_writes_snapshot(tmp_path):
    pool = FakePool(VALID_JSON)
    result = run_patrol_once(
        pool=pool,
        nodes=["node12", "node13"],
        playbook=Path("tests/data/toy_playbook.sh"),
        records_root=tmp_path,
        run_id="patrol-20260710-0800",
        platform="alpha",
    )
    assert isinstance(result, PatrolResult)
    assert result.snapshot_path.exists()
    snap = json.loads(result.snapshot_path.read_text(encoding="utf-8"))
    assert set(snap["nodes"].keys()) == {"node12", "node13"}
    assert snap["nodes"]["node12"]["verdict"] == "needs_llm"
    assert snap["platform"] == "alpha"
    assert result.summary_path.exists()
    summary = result.summary_path.read_text(encoding="utf-8")
    assert "node12" in summary and "node13" in summary


def test_main_rejects_bad_run_id(monkeypatch, tmp_path):
    """CLI main() 里 run_id 非法字符集必须早于 mkdir 拒绝,防路径穿越。"""
    import sys
    import scripts.patrol_runner as pr

    # mock sys.argv 传入非法 run_id
    monkeypatch.setattr(sys, "argv", [
        "patrol_runner",
        "--playbook", "tests/data/toy_playbook.sh",
        "--run-id", "../../etc/foo",
    ])
    # 不该进 run_patrol_once,应早 raise SystemExit
    with pytest.raises(SystemExit):
        pr.main()
    # 关键:records/patrol/../../etc/foo 目录不应被创建
    import os
    assert not os.path.exists("records/patrol/../../etc/foo")


def test_run_patrol_continues_on_single_node_failure(tmp_path):
    class FailingPool(FakePool):
        def __init__(self):
            super().__init__(VALID_JSON)
            self.first = True
        def run(self, node, cmd, timeout=180.0, approved=False):
            from lib.ssh_pool import CmdResult
            if self.first and "bash /tmp/opsagent/" in cmd:
                self.first = False
                return CmdResult(node, cmd, 1, "", "boom", 0.1, timed_out=True)
            return super().run(node, cmd, timeout, approved)

    result = run_patrol_once(
        pool=FailingPool(),
        nodes=["node12", "node13"],
        playbook=Path("tests/data/toy_playbook.sh"),
        records_root=tmp_path,
        run_id="patrol-fail",
        platform="alpha",
    )
    snap = json.loads(result.snapshot_path.read_text(encoding="utf-8"))
    assert "error" in snap["nodes"]["node12"]      # 一台失败
    assert snap["nodes"]["node13"]["verdict"] == "needs_llm"
