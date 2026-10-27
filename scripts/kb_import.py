#!/usr/bin/env python
"""Import a local document through the canonical knowledge contract."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.data_root import resolve_data_root
from lib.datastore import open_datastore
from lib.kb.bases import place_document, resolve_knowledge_root
from lib.kb.contract import (
    ImportManifest,
    render_document,
    source_digest,
    target_relative,
)
from lib.kb.frontmatter import load_doc
from lib.kb.writer import KnowledgeWriter


def _existing_target(knowledge_root: Path, relative: Path, manifest: ImportManifest) -> Path:
    """--target 只能改写同一篇既有文档:须为 update,且类型/平台/服务/problem 与原文档一致。"""
    if manifest.mode != "update":
        raise ValueError("--target 只用于 mode=update")
    path = knowledge_root / relative
    if not path.is_file():
        raise ValueError(f"更新目标不存在: {relative.as_posix()}")
    # 比归一后的值:落盘 platform 是资源引用,manifest 仍写短键
    existing = load_doc(path)
    for field in ("type", "platform", "service", "problem"):
        if getattr(existing, field) != getattr(manifest, field):
            raise ValueError(
                f"--target 与 manifest 的 {field} 不一致: {getattr(existing, field)} != {getattr(manifest, field)}"
            )
    return relative


def run(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Import model-normalized local Markdown into knowledge/",
    )
    parser.add_argument("--source", required=True, help="原始本地文档，只读取并记录摘要")
    parser.add_argument("--manifest", required=True, help="模型按 skill 生成的结构化 JSON")
    parser.add_argument("--repo-root", default=str(ROOT))
    parser.add_argument("--state-dir", default="state")
    parser.add_argument(
        "--target", default=None,
        help="mode=update 时直接指定既有文档 ID(相对知识库根);不在约定路径的存量文档用",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    source = Path(args.source).expanduser().resolve()
    knowledge_root = resolve_knowledge_root(repo_root)
    try:
        if not source.is_file():
            raise ValueError(f"本地源文件不存在: {source}")
        try:
            source.relative_to(knowledge_root)
        except ValueError:
            pass
        else:
            raise ValueError("手动导入源文件必须位于 knowledge/ 之外")
        raw_manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        manifest = ImportManifest.model_validate(raw_manifest)
        if args.target:
            relative = _existing_target(knowledge_root, Path(args.target), manifest)
        else:
            relative = place_document(
                knowledge_root, target_relative(manifest), manifest.knowledge_base, manifest.task_type,
            )
        content = render_document(
            manifest,
            source="manual_import",
            source_ref=source.name,
            source_sha256=source_digest(source),
            author=open_datastore(repo_root).users.owner_id(),
        )
        state_dir = Path(args.state_dir)
        if not state_dir.is_absolute():
            state_dir = resolve_data_root(repo_root) / state_dir
        writer = KnowledgeWriter(repo_root, state_dir)
        if manifest.mode == "new":
            result = writer.create(relative, content)
        else:
            target = knowledge_root / relative
            if not target.is_file():
                raise ValueError(f"更新目标不存在: {relative.as_posix()}")
            result = writer.mutate(relative, lambda _original: content)
        print(json.dumps({
            "ok": True,
            "mode": manifest.mode,
            "doc": result.path.relative_to(knowledge_root).as_posix(),
            "source_sha256": source_digest(source),
            "routing_rebuilt": result.routing_rebuilt,
        }, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "error": type(exc).__name__,
            "message": str(exc),
        }, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
