from pathlib import Path

from lib.kb.contract import ImportManifest, render_document
from scripts.check_kb_entrypoints import validate_added_document


def test_new_legacy_document_is_rejected(tmp_path):
    path = tmp_path / "alpha-system-old.md"
    path.write_text(
        "---\ntitle: Old\ntype: knowledge\nplatform: alpha\nservice: system\n"
        "status: draft\n---\n# Old\n",
        encoding="utf-8",
    )
    assert any("schema_version" in item for item in validate_added_document(path))


def test_canonical_manual_import_is_accepted(tmp_path):
    manifest = ImportManifest.model_validate({
        "title": "系统知识", "type": "knowledge", "platform": "alpha",
        "service": "system", "problem": "system-knowledge",
        "summary": "系统知识摘要。",
        "sections": {
            "适用范围": "Alpha", "关键知识": "关键事实",
            "边界与注意事项": "边界", "参考": "内部资料",
        },
    })
    path = tmp_path / "alpha-system-knowledge.md"
    path.write_text(render_document(
        manifest, source="manual_import", source_ref="source.md",
        source_sha256="a" * 64,
    ), encoding="utf-8")
    assert validate_added_document(path) == []


def test_templates_and_indexes_are_exempt(tmp_path):
    template = tmp_path / "_templates/template.md"
    template.parent.mkdir()
    template.write_text("placeholder", encoding="utf-8")
    index = tmp_path / "_index.md"
    index.write_text("index", encoding="utf-8")
    assert validate_added_document(template) == []
    assert validate_added_document(index) == []


def test_repository_files_at_knowledge_root_are_not_documents(tmp_path, monkeypatch):
    """知识库根级 CLAUDE.md 新增时不按知识文档契约校验;库内新文档仍校验。"""
    import subprocess
    from scripts.check_kb_entrypoints import run

    kb = tmp_path / "knowledge"
    (kb / "cluster").mkdir(parents=True)
    (kb / "cluster" / "kb.yaml").write_text("id: cluster\nname: 集群运维\n", encoding="utf-8")
    (kb / "CLAUDE.md").write_text("# 知识库仓说明\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(kb)], check=True)
    subprocess.run(["git", "-C", str(kb), "add", "-A"], check=True)
    monkeypatch.setenv("OPS_AGENT_KNOWLEDGE_ROOT", str(kb))
    assert run(["--repo-root", str(tmp_path)]) == 0
    legacy = kb / "cluster" / "alpha" / "system" / "alpha-system-old.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("---\ntitle: Old\ntype: knowledge\nplatform: alpha\nservice: system\n---\n# Old\n",
                      encoding="utf-8")
    subprocess.run(["git", "-C", str(kb), "add", "-A"], check=True)
    assert run(["--repo-root", str(tmp_path)]) == 1
