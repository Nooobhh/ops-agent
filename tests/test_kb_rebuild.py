"""lib/kb/rebuild.py:routing + wiki 统一重建。"""
from pathlib import Path

from lib.kb.rebuild import rebuild


def test_rebuild_writes_all_artifacts(tmp_path):
    kb = tmp_path / "knowledge" / "alpha" / "system"
    kb.mkdir(parents=True)
    (kb / "alpha-system-demo-case.md").write_text(
        "---\ntitle: alpha-system-demo-case\ntype: troubleshoot\nplatform: alpha\n"
        "service: system\nproblem: demo-case\nsymptoms: []\nalerts: [demo_alert]\n"
        "playbook: \nstatus: draft\n---\n\n## 现象\n\nx\n", encoding="utf-8")
    out = rebuild(tmp_path)
    assert out == tmp_path / "build" / "wiki.html"
    assert (tmp_path / "build" / "routing.json").exists()
    assert (tmp_path / "build" / "kb-topology.json").exists()
    assert (tmp_path / "build" / "kb-topology.html").exists()
    assert (tmp_path / "build" / "kb-recall.sqlite").exists()
    assert "demo_alert" in out.read_text(encoding="utf-8")
    assert (kb / "_index.md").exists()
    assert "demo_alert" in (kb / "_index.md").read_text(encoding="utf-8")


def test_rebuild_keeps_going_when_vector_sync_fails(tmp_path, monkeypatch, capsys):
    """向量层是可选派生物:同步失败只告警,不能让知识写入跟着回滚。"""
    from lib.kb import rebuild as rebuild_module
    from lib.kb.retrieval.embedding import EmbeddingError

    def broken(_root):
        raise EmbeddingError("本地 embedding 推理失败")

    monkeypatch.setattr(rebuild_module, "sync_vectors", broken)
    kb = tmp_path / "knowledge" / "alpha" / "system"
    kb.mkdir(parents=True)
    (kb / "alpha-system-demo-case.md").write_text(
        "---\ntitle: alpha-system-demo-case\ntype: troubleshoot\nplatform: alpha\n"
        "service: system\nproblem: demo-case\nsymptoms: []\nalerts: []\n"
        "playbook: \nstatus: draft\n---\n\n## 现象\n\nx\n", encoding="utf-8")
    assert rebuild(tmp_path) == tmp_path / "build" / "wiki.html"
    assert "向量索引未更新" in capsys.readouterr().err
