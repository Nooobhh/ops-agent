#!/usr/bin/env python
"""Verify managed index changes produced by rebuild are staged."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.kb.bases import resolve_knowledge_root


def unstaged_indexes(kb_root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(kb_root), "status", "--porcelain", "--untracked-files=all"],
        text=True, capture_output=True, check=True,
    )
    pending = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        status, path = line[:2], line[3:]
        if not (path == "_index.md" or path.endswith("/_index.md")):
            continue
        # First column is staged state; second column is unstaged state.
        if status == "??" or status[1] != " ":
            pending.append(path)
    return pending


def run(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    pending = unstaged_indexes(resolve_knowledge_root(repo_root))
    if not pending:
        return 0
    print("ERROR rebuild 已更新知识索引，请将以下文件一并暂存后重试:")
    for path in pending:
        print(f"  knowledge/{path}")
    return 1


if __name__ == "__main__":
    raise SystemExit(run())
