from pathlib import Path

from lib.playbook_lint import lint_playbook

REPO = Path(__file__).resolve().parents[1]
TOY = REPO / "tests" / "data" / "toy_playbook.sh"


def test_toy_playbook_clean():
    assert lint_playbook(TOY) == []


def test_write_step_rejected(tmp_path):
    bad = tmp_path / "bad.sh"
    bad.write_text(
        '#!/bin/bash\nsource lib.sh\npb_init "t/bad"\n'
        'step "boom" "kill -9 1" "no"\nemit_verdict conclusive ok "x"\n',
        encoding="utf-8",
    )
    errs = lint_playbook(bad)
    assert any("kill" in e for e in errs)


def test_step_opt_still_scanned_for_write_commands(tmp_path):
    """step_opt 不得绕过只读闸门:\\b 在 step 后对 step_opt 不成立,漏配则整行不被扫描。"""
    bad = tmp_path / "opt.sh"
    bad.write_text(
        '#!/bin/bash\nsource lib.sh\npb_init "t/opt"\n'
        'step_opt "boom" "rm -rf /data" "no" DATANODE\nemit_verdict conclusive ok "x"\n',
        encoding="utf-8",
    )
    assert any("rm" in e for e in lint_playbook(bad))


def test_step_opt_accepts_component_as_trailing_token(tmp_path):
    ok = tmp_path / "opt_ok.sh"
    ok.write_text(
        '#!/bin/bash\nsource lib.sh\npb_init "t/opt"\n'
        'step_opt "dn" "ps -eo cmd | grep -E \'DataNode\' | grep -v grep" "DN 存活" DATANODE\n'
        'emit_verdict conclusive ok "x"\n',
        encoding="utf-8",
    )
    assert [e for e in lint_playbook(ok) if "形态" in e] == []


def test_syntax_error_rejected(tmp_path):
    bad = tmp_path / "syn.sh"
    bad.write_text('#!/bin/bash\npb_init "t/syn"\nif [ x ; then\n', encoding="utf-8")
    errs = lint_playbook(bad)
    assert any("语法" in e for e in errs)


def test_missing_emit_rejected(tmp_path):
    bad = tmp_path / "noemit.sh"
    bad.write_text('#!/bin/bash\npb_init "t/x"\nstep "a" "free -m" "e"\n', encoding="utf-8")
    errs = lint_playbook(bad)
    assert any("emit_verdict" in e for e in errs)


def test_single_quoted_step_rejected(tmp_path):
    """Critical: 单引号 step 参数不符合严格形态。"""
    bad = tmp_path / "sq.sh"
    bad.write_text(
        '#!/bin/bash\npb_init "t/sq"\n'
        "step 'boom' 'rm -rf /tmp/foo' 'no'\n"
        'emit_verdict conclusive ok "x"\n',
        encoding="utf-8",
    )
    errs = lint_playbook(bad)
    assert any("形态" in e or "step" in e.lower() for e in errs)


def test_command_substitution_rejected(tmp_path):
    """Critical: $() 命令替换让写操作藏在里面,lint 无法静态分析,一律禁。"""
    bad = tmp_path / "cs.sh"
    bad.write_text(
        '#!/bin/bash\npb_init "t/cs"\n'
        'step "boom" "echo $(rm -rf /tmp/foo)" "no"\n'
        'emit_verdict conclusive ok "x"\n',
        encoding="utf-8",
    )
    errs = lint_playbook(bad)
    assert any("命令替换" in e or "$(" in e for e in errs)


def test_backtick_substitution_rejected(tmp_path):
    """Critical: 反引号同样禁。"""
    bad = tmp_path / "bt.sh"
    bad.write_text(
        '#!/bin/bash\npb_init "t/bt"\n'
        'step "boom" "echo `rm -rf /tmp/foo`" "no"\n'
        'emit_verdict conclusive ok "x"\n',
        encoding="utf-8",
    )
    errs = lint_playbook(bad)
    assert any("命令替换" in e or "反引号" in e or "`" in e for e in errs)


def test_line_continuation_step_rejected(tmp_path):
    """Important: 行续把 step 拆两行,必须报错(严格单行要求)。"""
    bad = tmp_path / "lc.sh"
    bad.write_text(
        '#!/bin/bash\npb_init "t/lc"\n'
        'step "boom" \\\n'
        '     "rm -rf /tmp/foo" "no"\n'
        'emit_verdict conclusive ok "x"\n',
        encoding="utf-8",
    )
    errs = lint_playbook(bad)
    assert any("形态" in e or "step" in e.lower() for e in errs)


def test_variable_in_step_cmd_rejected(tmp_path):
    """Important: cmd 里含 shell 变量($X)让实际内容不可分析。
    但允许 awk/perl 的 $1 $NF 这类工具语法——它们在双引号+空格前后不算 shell 展开点,
    实际上双引号里 shell 会展开 $1,所以严禁,写法必须 '$1' 或转义 \\$1。"""
    bad = tmp_path / "var.sh"
    bad.write_text(
        '#!/bin/bash\npb_init "t/var"\n'
        'step "boom" "eval $CMD" "no"\n'
        'emit_verdict conclusive ok "x"\n',
        encoding="utf-8",
    )
    errs = lint_playbook(bad)
    assert any("变量" in e or "$" in e for e in errs)
