import subprocess
from pathlib import Path

import pytest

from lib.playbook import parse_result

REPO = Path(__file__).resolve().parents[1]

SAMPLE = """{"playbook":"alpha/system/x","node":"node12","verdict":"conclusive",
"severity":"P1","conclusion_hint":"h",
"steps":[{"name":"s1","cmd":"free -m","expect":"e","actual":"a","pass":false,"raw":"r"}],
"metrics":{"mem_pct":91.2},"raw_dir":"/tmp/opsagent/run-1"}"""


def test_parse_sample():
    r = parse_result(SAMPLE)
    assert r.verdict == "conclusive" and r.node == "node12"
    assert r.steps[0].name == "s1" and r.steps[0].pass_ is False
    assert r.metrics["mem_pct"] == 91.2
    assert r.next_playbook is None


def test_parse_tolerates_noise_around_json():
    assert parse_result("warn: x\n" + SAMPLE + "\n").verdict == "conclusive"


def test_parse_rejects_bad_verdict():
    with pytest.raises(ValueError):
        parse_result(SAMPLE.replace("conclusive", "maybe"))


def _run_opt_playbook(tmp_path, components):
    """跑一个只含 step_opt 的 playbook,目标进程保证不存在,返回该 step。"""
    pb = tmp_path / "opt.sh"
    pb.write_text(
        '#!/bin/bash\nsource "${PLAYBOOK_LIB}"\npb_init "test/opt"\n'
        'step_opt "dn" "ps -eo cmd | grep -E \'NoSuchProcess7788\' | grep -v grep" "DN 存活" DATANODE\n'
        'emit_verdict conclusive ok "t"\n',
        encoding="utf-8",
    )
    env = {"PATH": "/usr/bin:/bin", "OPSAGENT_RUN_DIR": str(tmp_path / "run"),
           "OPSAGENT_NODE": "local",
           "PLAYBOOK_LIB": str(REPO / "playbooks" / "_lib" / "playbook_lib.sh")}
    if components is not None:
        env["OPSAGENT_COMPONENTS"] = components
    out = subprocess.run(["bash", str(pb)], capture_output=True, text=True, timeout=30, env=env)
    return parse_result(out.stdout).steps[0]


def test_step_opt_skips_when_component_not_deployed(tmp_path):
    step = _run_opt_playbook(tmp_path, "NAMENODE,RESOURCEMANAGER")
    assert step.pass_ is True and "not-deployed" in step.actual


def test_step_opt_still_fails_when_deployed_component_is_down(tmp_path):
    """关键:组件本该在此却没跑 = 真故障,不能被「若有」语义静音。"""
    step = _run_opt_playbook(tmp_path, "NAMENODE,DATANODE")
    assert step.pass_ is False


def test_step_opt_falls_back_to_strict_without_topology(tmp_path):
    assert _run_opt_playbook(tmp_path, None).pass_ is False


def test_metric_with_missing_value_stays_parseable(tmp_path):
    """采集工具缺失导致 metric 值为空时,不得拼出损坏 JSON 毁掉整份结果。"""
    pb = tmp_path / "m.sh"
    pb.write_text(
        '#!/bin/bash\nsource "${PLAYBOOK_LIB}"\npb_init "test/m"\n'
        'metric good 42\n'
        'metric missing "$(command -v definitely_missing_tool_xyz)"\n'
        'metric junk "abc"\n'
        'emit_verdict conclusive ok "t"\n',
        encoding="utf-8",
    )
    out = subprocess.run(
        ["bash", str(pb)], capture_output=True, text=True, timeout=30,
        env={"PATH": "/usr/bin:/bin", "OPSAGENT_RUN_DIR": str(tmp_path / "run"),
             "OPSAGENT_NODE": "local",
             "PLAYBOOK_LIB": str(REPO / "playbooks" / "_lib" / "playbook_lib.sh")},
    )
    r = parse_result(out.stdout)          # 旧实现在此抛 JSON 解析错
    assert r.metrics == {"good": 42, "missing": None, "junk": None}


def test_toy_playbook_runs_locally(tmp_path):
    out = subprocess.run(
        ["bash", str(REPO / "tests" / "data" / "toy_playbook.sh")],
        capture_output=True, text=True, timeout=30,
        env={"PATH": "/usr/bin:/bin", "OPSAGENT_RUN_DIR": str(tmp_path), "OPSAGENT_NODE": "local"},
    )
    r = parse_result(out.stdout)
    assert r.verdict == "needs_llm" and r.playbook == "test/toy"
    assert [s.pass_ for s in r.steps] == [True, False]
    assert r.metrics["answer"] == 42
    assert (tmp_path / "host.out").exists()      # raw 全量落盘


def test_parse_rejects_string_pass_bool():
    """Critical #3: pass 字段必须是 JSON bool,字符串"false" 应拒绝。"""
    bad = SAMPLE.replace('"pass":false', '"pass":"false"')
    with pytest.raises(ValueError, match="pass"):
        parse_result(bad)


def test_parse_tolerates_braces_in_noise():
    """Important #4: 前置警告里的花括号不得干扰真 JSON 定位。"""
    noisy = "warn: config {oops}\nlecture: user{admin}\n" + SAMPLE
    assert parse_result(noisy).verdict == "conclusive"


def test_parse_rejects_missing_top_key():
    """Important #5: 缺 playbook 或 node 抛 ValueError,不是 KeyError。"""
    import json
    d = json.loads(SAMPLE); del d["playbook"]
    with pytest.raises(ValueError, match="playbook"):
        parse_result(json.dumps(d))


def test_toy_playbook_handles_ansi_and_utf8(tmp_path):
    """Critical #1+#2 集成:ANSI 转义 + 中文长文本 4KB+ 都产出合法 JSON。"""
    import subprocess
    pb = tmp_path / "hard.sh"
    pb.write_text(
        '#!/bin/bash\n'
        'source "${PLAYBOOK_LIB}"\n'
        'pb_init "test/hard"\n'
        'step "ansi" \'printf "\\033[31mRED\\033[0m\\n"\' "颜色输出"\n'
        'step "cn_long" \'python3 -c "print(\\"内存交换分区使用率过高警告\\"*400,end=\\"\\")"\' "中文长文本"\n'
        'emit_verdict needs_llm P2 "hard 测试"\n',
        encoding="utf-8",
    )
    lib_sh = REPO / "playbooks" / "_lib" / "playbook_lib.sh"
    out = subprocess.run(
        ["bash", str(pb)],
        capture_output=True, text=True, timeout=30,
        env={"PATH": "/usr/bin:/bin", "OPSAGENT_RUN_DIR": str(tmp_path), "OPSAGENT_NODE": "local",
             "PLAYBOOK_LIB": str(lib_sh)},
    )
    r = parse_result(out.stdout)   # 不炸 = 已合法 JSON + 合法 UTF-8
    assert r.verdict == "needs_llm"
    assert len(r.steps) == 2


def test_to_session_dict_renames_pass_underscore_to_pass():
    """dataclass 字段 pass_ 序列化时必须重命名为 'pass' 键(避免 Python 保留字)。"""
    r = parse_result(SAMPLE)
    d = r.to_session_dict()
    assert d["steps"][0]["pass"] is False
    assert "pass_" not in d["steps"][0]
    assert d["playbook"] == "alpha/system/x"
    assert d["metrics"]["mem_pct"] == 91.2
