from pathlib import Path

import pytest

from lib.runner import PlaybookTimeout, run_playbook
from lib.ssh_pool import CmdResult

REPO = Path(__file__).resolve().parents[1]
TOY = REPO / "tests" / "data" / "toy_playbook.sh"

TOY_JSON = ('{"playbook":"test/toy","node":"node12","verdict":"conclusive","severity":"ok",'
            '"conclusion_hint":"h","steps":[],"metrics":{},"raw_dir":"/tmp/opsagent/r1"}')


class FakePool:
    def __init__(self, stdout=TOY_JSON, timed_out=False):
        self.cmds, self.pushes = [], []
        self._stdout, self._timed_out = stdout, timed_out

    def run(self, node, cmd, timeout=30.0, approved=False):
        self.cmds.append(cmd)
        return CmdResult(node, cmd, None if self._timed_out else 0,
                         self._stdout, "", 0.1, timed_out=self._timed_out)

    def push(self, node, local, remote):
        self.pushes.append((str(local), remote))


class TopologyPool(FakePool):
    def __init__(self, components):
        super().__init__()
        self._components = components

    def components(self, node):
        return self._components


def test_injects_node_components_for_optional_steps():
    pool = TopologyPool(["NAMENODE", "RESOURCEMANAGER"])
    run_playbook(pool, "node1", TOY, run_id="r1", cleanup=False)
    assert "OPSAGENT_COMPONENTS=NAMENODE,RESOURCEMANAGER " in pool.cmds[-1]


def test_components_unavailable_injects_empty_so_optional_steps_stay_strict():
    pool = FakePool()                                  # 无 components 方法
    run_playbook(pool, "node1", TOY, run_id="r1", cleanup=False)
    assert "OPSAGENT_COMPONENTS=''" in pool.cmds[-1]


def test_run_playbook_flow():
    pool = FakePool()
    r = run_playbook(pool, "node12", TOY, run_id="r1", cleanup=False)
    assert r.verdict == "conclusive"
    assert pool.cmds[0] == "mkdir -p /tmp/opsagent/r1"
    assert all(remote.startswith("/tmp/opsagent/r1/") for _, remote in pool.pushes)
    assert len(pool.pushes) == 2                      # playbook_lib.sh + playbook 本体
    assert "bash /tmp/opsagent/r1/toy_playbook.sh" in pool.cmds[-1]
    assert "OPSAGENT_RUN_DIR=/tmp/opsagent/r1" in pool.cmds[-1]


def test_run_playbook_timeout_raises():
    with pytest.raises(PlaybookTimeout):
        run_playbook(FakePool(timed_out=True), "node12", TOY, run_id="r2")


def test_invalid_run_id_rejected():
    """run_id 必须是文件名安全字符集,防路径穿越 + 命令注入。"""
    pool = FakePool()
    for bad in ["../../../etc/cron.d", "r1; cat /etc/shadow", "r1 && rm x", "r1/sub", "r1$X"]:
        with pytest.raises(ValueError, match="run_id"):
            run_playbook(pool, "node12", TOY, run_id=bad)
    assert pool.cmds == []          # 校验早于任何 SSH 出口


def test_valid_run_id_accepted():
    """合法字符:字母数字下划线连字符点,不含斜杠/空格/shell metachars。"""
    pool = FakePool()
    for good in ["r1", "smoke", "ts-20260710-1432-ab3f", "run.1"]:
        pool.cmds.clear(); pool.pushes.clear()
        r = run_playbook(pool, "node12", TOY, run_id=good)
        assert r.verdict == "conclusive"


def test_args_shell_quoted():
    """args 每个元素独立 shlex.quote,防注入。"""
    import shlex
    pool = FakePool()
    r = run_playbook(pool, "node12", TOY, run_id="r3",
                     args=["--target", "node12; rm -rf /", "--json"], cleanup=False)
    # 最后一条 cmd 必须包含 shlex.quote 后的 args
    exec_cmd = pool.cmds[-1]
    assert "--target" in exec_cmd
    assert shlex.quote("node12; rm -rf /") in exec_cmd    # 引号包住,不能裸出
    # 关键:注入串不出现在裸形式
    assert "node12; rm -rf /" not in exec_cmd.replace(shlex.quote("node12; rm -rf /"), "")


def test_args_none_or_empty_ok():
    """args=None 或 [] 都合法,不追加任何 args。"""
    pool = FakePool()
    r1 = run_playbook(pool, "node12", TOY, run_id="r4", cleanup=False)
    exec1 = pool.cmds[-1]
    pool.cmds.clear(); pool.pushes.clear()
    r2 = run_playbook(pool, "node12", TOY, run_id="r5", args=[], cleanup=False)
    exec2 = pool.cmds[-1]
    # 都不带 args,exec cmd 结尾应干净收(以 bash <pb> 结尾,无额外 arg)
    assert exec1.rstrip().endswith(TOY.name)
    assert exec2.rstrip().endswith(TOY.name)


def test_run_playbook_cleans_remote_dir(fixture_pool, tmp_path):
    from lib.runner import run_playbook
    pb = tmp_path / "x.sh"
    pb.write_text("#!/bin/bash\n", encoding="utf-8")
    run_playbook(fixture_pool, "node1", pb, run_id="ts-clean")
    assert fixture_pool.cmds[-1] == ("rm -rf /tmp/opsagent/ts-clean", True)


def test_run_playbook_cleanup_optout(fixture_pool, tmp_path):
    from lib.runner import run_playbook
    pb = tmp_path / "x.sh"
    pb.write_text("#!/bin/bash\n", encoding="utf-8")
    run_playbook(fixture_pool, "node1", pb, run_id="ts-keep", cleanup=False)
    assert all("rm -rf" not in c for c, _ in fixture_pool.cmds)
