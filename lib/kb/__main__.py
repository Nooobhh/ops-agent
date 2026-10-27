"""kb-lint CLI:.venv/bin/python -m lib.kb <kb_root>

对指定目录跑完整 lint(frontmatter + 命名 + 目录 + procedure 硬约束 + alertname 唯一 +
wikilink 断链 + playbook 指针存在 + redlines hash);任一错误 → exit 1。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .bases import redlines_path, resolve_knowledge_root
from .lint import lint_kb
from .redlines import check_redlines_hash


def _promote_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m lib.kb promote",
                                 description="draft→verified 晋升 + wiki 重建")
    ap.add_argument("doc", help="相对 knowledge/ 的文档路径")
    ap.add_argument("--reason", required=True, help="人工确认晋升的理由")
    ap.add_argument("--run-id", help="可选的关联排查 run")
    ap.add_argument("--repo-root", default=".")
    a = ap.parse_args(argv)
    from .promote import AlreadyVerified, PromoteError, promote_manual
    try:
        info = promote_manual(
            a.doc, reason=a.reason, run_id=a.run_id,
            repo_root=Path(a.repo_root),
        )
    except AlreadyVerified:
        print(f"已是 verified,跳过: {a.doc}")
        return 0
    except PromoteError as e:
        print(f"ERROR {e}", file=sys.stderr)
        return 1
    print(f"晋升: {info['doc']} → verified({info['verified_at']},依据 {info['verified_by']})")
    try:
        from .rebuild import rebuild
        print(f"wiki 重建: {rebuild(Path(a.repo_root))}")
    except Exception as e:                       # 晋升已落盘,重建失败只报警
        print(f"WARN wiki 重建失败: {e}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "promote":
        return _promote_main(args[1:])
    ap = argparse.ArgumentParser(prog="python -m lib.kb", description="ops-agent 知识库 lint")
    ap.add_argument("kb_root", nargs="?", default=None,
                    help="知识库根目录(默认按 config/paths.local.yaml 解析)")
    ap.add_argument("--repo-root", default=".",
                    help="ops-agent 仓库根(playbook 指针与报告回链相对该根)")
    ap.add_argument("--redlines", default=None, help="默认取默认库 shared/procedures/redlines.md")
    ap.add_argument("--filter-rules", default=None,
                    help="核心规则文件,默认 <repo>/lib/data/filter_rules.yaml;环境包红线补充自动并入")
    a = ap.parse_args(args)

    failed = False
    repo_root = Path(a.repo_root).resolve()
    kb_root = Path(a.kb_root) if a.kb_root else resolve_knowledge_root(repo_root)
    for p, errs in lint_kb(kb_root, repo_root=repo_root):
        for e in errs:
            print(f"{p.relative_to(repo_root) if repo_root in p.parents else p}: ERROR {e}")
        failed = failed or bool(errs)

    err = check_redlines_hash(
        Path(a.redlines) if a.redlines else redlines_path(kb_root),
        _redline_sources(repo_root, a.filter_rules),
    )
    if err:
        print(f"redlines: ERROR {err}")
        failed = True
    return 1 if failed else 0


def _redline_sources(repo_root: Path, core_override):
    """有效红线规则来源:核心规则(可覆盖路径)+ 环境包红线补充。"""
    from lib.cmd_filter import redline_sources
    from lib.envpack import load_env_pack

    sources = redline_sources(load_env_pack(repo_root))
    if core_override:
        sources[0] = Path(core_override)
    return sources


if __name__ == "__main__":
    sys.exit(main())
