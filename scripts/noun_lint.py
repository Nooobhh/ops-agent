"""专有名词 lint:按环境包词表 + 内置通用敏感模式扫核心仓入库文件(envs/ 除外)。

  .venv/bin/python scripts/noun_lint.py                  # 文本汇总 + 命中最多的文件
  .venv/bin/python scripts/noun_lint.py --details        # 另列逐处「文件 · 行 · 词条」
  .venv/bin/python scripts/noun_lint.py --json           # 完整 JSON 报告(可存作基线)
  .venv/bin/python scripts/noun_lint.py --baseline x.json  # 标出相对基线增长的词条与文件
  .venv/bin/python scripts/noun_lint.py --check          # 有命中即退出码 1(闸门)
  .venv/bin/python scripts/noun_lint.py --staged --check # 只扫暂存区内容(pre-commit)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.envpack import load_env_pack
from lib.envpack.noun_lint import builtin_terms, compare, load_terms, render_text, scan, staged_files


def run(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="输出完整 JSON 报告")
    ap.add_argument("--baseline", help="基线报告(--json 的输出),只标出增长")
    ap.add_argument("--top", type=int, default=20, help="文本模式列出命中最多的文件数")
    ap.add_argument("--details", action="store_true", help="文本模式逐处列出命中")
    ap.add_argument("--exclude", action="append", default=[], help="不扫的路径前缀,可重复")
    ap.add_argument("--staged", action="store_true", help="只扫 git 暂存区里新增/改动的文件内容")
    ap.add_argument("--check", action="store_true", help="有命中即退出码 1")
    a = ap.parse_args(argv)

    terms = [*load_terms(load_env_pack(ROOT)), *builtin_terms()]
    if a.staged:
        report = scan(ROOT, terms, exclude=a.exclude, files=staged_files(ROOT), reader=lambda relative: subprocess.run(
            ["git", "-C", str(ROOT), "show", f":{relative}"], capture_output=True, check=True).stdout)
    else:
        report = scan(ROOT, terms, exclude=a.exclude)
    comparison = None
    if a.baseline:
        comparison = compare(report, json.loads(Path(a.baseline).read_text(encoding="utf-8")))
    if a.json:
        payload = dict(report)
        if comparison is not None:
            payload["comparison"] = comparison
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    else:
        print(render_text(report, top=a.top, details=a.details or a.check, comparison=comparison))
    return 1 if a.check and report["total_hits"] else 0


if __name__ == "__main__":
    sys.exit(run())
