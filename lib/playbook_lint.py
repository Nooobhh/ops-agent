"""playbook 入库闸门:bash 语法 + step 严格形态 + step 命令只读扫描(复用 CmdFilter)+ 契约结构检查。

playbook 一律只读——WRITE 也不放行(approved 语义只属于深挖档人在环)。
step 严格形态:所有参数必须是单行双引号字符串字面量,禁命令替换/未转义变量展开——
静态可分析是唯一守住 lint 效力的方式。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import List

from .cmd_filter import CmdFilter, Level

# 严格 step 形态:step "name" "cmd" "expect" [assess_fn]
#                step_opt "name" "cmd" "expect" <组件名>
# 所有 quoted 参数在同一物理行;末尾单个裸 token 对 step 是 assess 函数,对 step_opt 是组件名
_STRICT_STEP_RE = re.compile(
    r'^\s*step(?:_opt)?\s+'
    r'"(?:[^"\\]|\\.)*"\s+'          # name
    r'"((?:[^"\\]|\\.)*)"\s+'        # cmd (captured)
    r'"(?:[^"\\]|\\.)*"'             # expect
    r'(?:\s+\S+)?\s*(?:#.*)?$',      # optional assess fn / 组件名 + optional trailing comment
    re.M,
)
# 任何 step / step_opt 开头的行(用于挑出不合严格形态的)
# 注意:\b 在 step 后不成立于 step_opt(_ 是词字符),必须显式并列,否则 step_opt 会整个绕过只读扫描
_STEP_LINE_RE = re.compile(r'^([ \t]*step(?:_opt)?(?![\w-])[^\n]*(?:\\\n[^\n]*)*)', re.M)


def _find_step_calls(text: str) -> List[str]:
    """返回所有 step 开头的完整调用文本(把行续折叠成单行,便于报错定位)。"""
    return [m.group(1) for m in _STEP_LINE_RE.finditer(text)]


def lint_playbook(path: Path) -> List[str]:
    errors: List[str] = []
    text = Path(path).read_text(encoding="utf-8")

    syn = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
    if syn.returncode != 0:
        errors.append(f"bash 语法错误: {syn.stderr.strip()}")

    if not re.search(r'^\s*pb_init\b', text, re.M):
        errors.append("缺少 pb_init 调用(必须为真实调用,非注释)")
    if not re.search(r'^\s*emit_verdict\b', text, re.M):
        errors.append("缺少 emit_verdict 调用(必须为真实调用,非注释)")

    f = CmdFilter.default()

    for raw_call in _find_step_calls(text):
        # 先做严格形态检查(整行,含可能的行续)
        one_line = raw_call.replace("\\\n", " ")
        m = _STRICT_STEP_RE.match(one_line + "\n")   # 加换行触发 $ 锚
        if m is None:
            snippet = raw_call.strip().replace("\n", " ")[:120]
            errors.append(f"step 调用形态不合规(必须 step \"name\" \"cmd\" \"expect\" [assess] 或 step_opt \"name\" \"cmd\" \"expect\" <组件名>,所有 quoted 参数双引号字面量、单物理行): {snippet}")
            continue

        cmd = m.group(1).replace('\\"', '"')

        # 禁命令替换
        if "$(" in cmd:
            errors.append(f"step cmd 禁止 $() 命令替换(lint 无法静态分析): {cmd}")
            continue
        if "`" in cmd:
            errors.append(f"step cmd 禁止反引号命令替换: {cmd}")
            continue
        # 禁未转义 shell 变量展开(允许 \$1 \$NF 这种转义写法用于 awk/perl)
        if re.search(r'(?<!\\)\$(?:\w+|\{)', cmd):
            errors.append(f"step cmd 禁止 shell 变量展开(实际内容不可分析,awk/perl 变量请写 \\$1): {cmd}")
            continue

        # 只读扫描
        v = f.classify(cmd)
        if v.level is not Level.READONLY:
            errors.append(f"step 命令非只读 [{v.level.value}] {v.reason}: {cmd}")

    return errors


def main(argv: List[str]) -> int:
    failed = False
    for p in argv:
        errs = lint_playbook(Path(p))
        for e in errs:
            print(f"{p}: ERROR {e}")
        failed = failed or bool(errs)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
