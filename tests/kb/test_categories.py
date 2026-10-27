"""运维视角 5 类文档与操作手册 sop_kind:推导口径、校验与 ContextPack 引用标注。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lib.kb.categories import CATEGORIES, doc_category, sop_kind_of
from lib.kb.contract import ImportManifest, render_document
from lib.kb.frontmatter import FrontmatterError, load_doc
from lib.kb.retrieval.context import RetrievalService


def test_category_mapping_and_default_sop_kind():
    assert CATEGORIES == ("troubleshoot", "procedure", "cluster_info", "reference", "oplog")
    assert doc_category("troubleshoot", "diagnosis") == "troubleshoot"
    assert doc_category("procedure", "sop") == "procedure"
    assert doc_category("oplog", "oplog") == "oplog"
    assert doc_category("knowledge", "baseline") == "cluster_info"
    assert doc_category("knowledge", "topology") == "cluster_info"
    assert doc_category("knowledge", "reference") == "reference"
    assert doc_category("knowledge", None) == "reference"
    assert sop_kind_of("procedure", None) == "routine"
    assert sop_kind_of("procedure", "deploy") == "deploy"
    assert sop_kind_of("troubleshoot", "deploy") is None


def _write(tmp_path: Path, extra: str, doc_type: str = "procedure") -> Path:
    path = tmp_path / "x.md"
    path.write_text(
        f"---\ntitle: x\ntype: {doc_type}\nplatform: shared\nservice: system\nproblem: x\n{extra}---\n\n# x\n",
        encoding="utf-8",
    )
    return path


def test_frontmatter_rejects_unknown_sop_kind(tmp_path: Path):
    assert load_doc(_write(tmp_path, "sop_kind: change\n")).sop_kind == "change"
    with pytest.raises(FrontmatterError, match="sop_kind"):
        load_doc(_write(tmp_path, "sop_kind: weekly\n"))


def _manifest(**updates):
    data = {
        "mode": "new", "title": "Redis 部署", "type": "procedure", "platform": "shared", "service": "redis",
        "problem": "redis-deploy", "summary": "部署步骤", "profile": "sop",
        "sections": {"适用场景": "a", "前置条件": "b", "步骤": "c", "验证": "d", "回滚": "e"},
    }
    data.update(updates)
    return ImportManifest.model_validate(data)


def test_manifest_sop_kind_only_for_procedure_and_rendered():
    rendered = render_document(_manifest(sop_kind="deploy"), source="manual_import", source_ref="test", source_sha256="0" * 64)
    assert "\nsop_kind: deploy\n" in rendered
    assert "sop_kind" not in render_document(_manifest(), source="manual_import", source_ref="test", source_sha256="0" * 64)
    with pytest.raises(ValueError, match="sop_kind"):
        _manifest(sop_kind="weekly")
    with pytest.raises(ValueError, match="只有 procedure"):
        _manifest(type="knowledge", profile="reference", sop_kind="deploy",
                  sections={"适用范围": "a", "关键知识": "b", "边界与注意事项": "c", "参考": "d"})


def test_context_refs_are_annotated_with_category_and_sop_kind():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE kb_documents(doc_id TEXT, doc_type TEXT, profile TEXT)")
    connection.execute("CREATE TABLE kb_doc_tags(doc_id TEXT, tag TEXT)")
    connection.executemany("INSERT INTO kb_documents VALUES (?, ?, ?)", [
        ("a.md", "procedure", "sop"), ("b.md", "knowledge", "baseline"), ("c.md", "troubleshoot", "diagnosis"),
    ])
    connection.execute("INSERT INTO kb_doc_tags VALUES ('a.md', 'sop_kind:deploy')")
    refs = [{"doc_id": "a.md"}, {"doc_id": "b.md"}, {"doc_id": "c.md"}, {"doc_id": "missing.md"}]
    RetrievalService._annotate_categories(connection, refs)
    assert [(ref.get("category"), ref.get("sop_kind")) for ref in refs] == [
        ("procedure", "deploy"), ("cluster_info", None), ("troubleshoot", None), (None, None),
    ]
