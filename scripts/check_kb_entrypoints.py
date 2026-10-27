#!/usr/bin/env python
"""Reject newly staged knowledge documents that bypass the two entry points."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.kb.bases import iter_documents, resolve_knowledge_root
from lib.kb.contract import DOCUMENT_SOURCES, SCHEMA_VERSION
from lib.kb.frontmatter import FrontmatterError, load_doc


def validate_added_document(path: Path) -> list[str]:
    path = Path(path)
    if path.name == "_index.md" or "_templates" in path.parts:
        return []
    try:
        doc = load_doc(path)
    except FrontmatterError as exc:
        return [str(exc)]
    errors = []
    if doc.raw.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"新增文档必须使用 schema_version: {SCHEMA_VERSION}")
    if doc.raw.get("source") not in DOCUMENT_SOURCES:
        errors.append(f"新增文档 source 必须是 {sorted(DOCUMENT_SOURCES)}")
    return errors


def run(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=str(ROOT))
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    kb_root = resolve_knowledge_root(repo_root)
    if not (kb_root / ".git").exists():
        return 0
    result = subprocess.run(
        ["git", "-C", str(kb_root), "diff", "--cached", "--diff-filter=A",
         "--name-only", "--", "*.md"],
        text=True, capture_output=True, check=True,
    )
    # 只校验库内知识文档;根级 CLAUDE.md/README.md 等仓库说明不走入库契约
    documents = {item.doc_id for item in iter_documents(kb_root)}
    failed = False
    for relative in (line.strip() for line in result.stdout.splitlines() if line.strip()):
        if relative not in documents:
            continue
        for error in validate_added_document(kb_root / relative):
            failed = True
            print(f"knowledge/{relative}: ERROR {error}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(run())
