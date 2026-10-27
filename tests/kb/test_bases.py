"""多知识库布局:knowledge 根下 cluster/ 与 jobs/ 两个库,文档 ID 带库前缀。"""
from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from lib.kb.bases import (
    LEGACY_BASE,
    KnowledgeBaseError,
    discover_bases,
    doc_id_aliases,
    iter_documents,
    place_document,
    redlines_path,
    resolve_doc_id,
    resolve_knowledge_root,
    split_doc_path,
)


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _doc(title: str, platform: str, service: str, problem: str, body: str = "") -> str:
    return (
        "---\n"
        f"title: {title}\ntype: troubleshoot\nplatform: {platform}\nservice: {service}\n"
        f"problem: {problem}\nsymptoms: [症状]\nalerts: []\nplaybook: \nstatus: draft\n"
        f"related: []\n---\n\n# {title}\n\n{body}\n"
    )


@pytest.fixture
def knowledge(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    _write(root, "cluster/kb.yaml", "id: cluster\nname: 集群运维\nlayout: platform_service\n")
    _write(root, "jobs/kb.yaml",
           "id: jobs\nname: JobScheduler 任务\nlayout: flat\n"
           "platform: alpha\nservice: jobs\ndescription: 任务告警与问答\n")
    _write(root, "CLAUDE.md", "# 知识库仓说明(不是知识文档)\n")
    _write(root, "cluster/alpha/hdfs/alpha-hdfs-nn-heap.md",
           _doc("NameNode Heap", "alpha", "hdfs", "nn-heap",
                "见 [[jobs/task/alpha-jobs-failure-diagnosis|失败下钻]]"))
    _write(root, "cluster/alpha/hdfs/namenode/alpha-hdfs-nn-rpc.md",
           _doc("NameNode RPC", "alpha", "hdfs", "nn-rpc"))
    _write(root, "jobs/task/alpha-jobs-failure-diagnosis.md",
           _doc("任务失败下钻", "alpha", "jobs", "failure-diagnosis",
                "下游见 [[cluster/alpha/hdfs/alpha-hdfs-nn-heap]]"))
    _write(root, "_redirects.yaml",
           "prefixes:\n  alpha/: cluster/alpha/\n  oplog/: cluster/oplog/\n"
           "docs:\n  alpha/jobs/alpha-jobs-failure-diagnosis.md: "
           "jobs/task/alpha-jobs-failure-diagnosis.md\n")
    return root


def test_discovery_and_split(knowledge: Path):
    bases = discover_bases(knowledge)
    assert [base.id for base in bases] == ["cluster", "jobs"]
    base, inner = split_doc_path("cluster/alpha/hdfs/x.md", bases)
    assert (base.id, inner) == ("cluster", PurePosixPath("alpha/hdfs/x.md"))
    base, inner = split_doc_path("_templates/x.md", bases)
    assert base is None
    # 没有 kb.yaml 的根是旧单库布局,文档 ID 不带前缀
    legacy = discover_bases(knowledge / "cluster" / "alpha")
    assert legacy == (LEGACY_BASE,)


def test_manifest_id_must_match_directory(tmp_path: Path):
    _write(tmp_path, "cluster/kb.yaml", "id: other\nname: x\n")
    with pytest.raises(KnowledgeBaseError, match="目录名"):
        discover_bases(tmp_path)


def test_iter_documents_skips_root_repository_files(knowledge: Path):
    ids = [item.doc_id for item in iter_documents(knowledge)]
    assert "CLAUDE.md" not in ids
    assert "cluster/alpha/hdfs/alpha-hdfs-nn-heap.md" in ids
    assert "jobs/task/alpha-jobs-failure-diagnosis.md" in ids


def test_knowledge_root_resolution(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OPS_AGENT_KNOWLEDGE_ROOT", raising=False)
    repo = tmp_path / "ops-agent"
    assert resolve_knowledge_root(repo) == repo.resolve() / "knowledge"
    _write(repo, "config/paths.local.yaml", "knowledge_root: ../knowledge\n")
    assert resolve_knowledge_root(repo) == (tmp_path / "knowledge").resolve()
    monkeypatch.setenv("OPS_AGENT_KNOWLEDGE_ROOT", str(tmp_path / "elsewhere"))
    assert resolve_knowledge_root(repo) == (tmp_path / "elsewhere").resolve()


def test_redirects_and_aliases(knowledge: Path):
    moved = "jobs/task/alpha-jobs-failure-diagnosis.md"
    assert resolve_doc_id(knowledge, "alpha/jobs/alpha-jobs-failure-diagnosis.md") == moved
    assert resolve_doc_id(knowledge, "alpha/hdfs/alpha-hdfs-nn-heap.md") == "cluster/alpha/hdfs/alpha-hdfs-nn-heap.md"
    assert resolve_doc_id(knowledge, moved) == moved
    assert resolve_doc_id(knowledge, "beta/x.md") == "beta/x.md"     # 无映射原样返回
    assert doc_id_aliases(knowledge, "cluster/alpha/hdfs/alpha-hdfs-nn-heap.md") == {
        "cluster/alpha/hdfs/alpha-hdfs-nn-heap.md", "alpha/hdfs/alpha-hdfs-nn-heap.md",
    }
    assert "alpha/jobs/alpha-jobs-failure-diagnosis.md" in doc_id_aliases(knowledge, moved)


def test_place_document_defaults_to_cluster_and_flattens_flat_bases(knowledge: Path):
    relative = Path("alpha/jobs/alpha-jobs-p1-t2-oom.md")
    assert place_document(knowledge, relative) == Path("cluster") / relative
    assert place_document(knowledge, relative, "jobs") == \
        Path("jobs/alpha-jobs-p1-t2-oom.md")
    with pytest.raises(KnowledgeBaseError):
        place_document(knowledge, relative, "missing")
    assert redlines_path(knowledge) == knowledge / "cluster/shared/procedures/redlines.md"


def test_lint_layouts_per_base(knowledge: Path):
    from lib.kb.lint import lint_doc, lint_kb

    results = {path.relative_to(knowledge).as_posix(): errors
               for path, errors in lint_kb(knowledge, repo_root=knowledge.parent)}
    assert "CLAUDE.md" not in results
    assert all(not errors for errors in results.values()), results
    bad = _write(knowledge, "jobs/a/b/alpha-jobs-deep.md",
                 _doc("太深", "alpha", "jobs", "deep"))
    assert any("一级子目录" in error for error in lint_doc(bad, knowledge, knowledge.parent))
    wrong = _write(knowledge, "jobs/alpha-yarn-x.md", _doc("错服务", "alpha", "yarn", "x"))
    assert any("service='jobs'" in error for error in lint_doc(wrong, knowledge, knowledge.parent))


def test_indexes_tree_and_wikilinks_use_prefixed_ids(knowledge: Path):
    from lib.kb.browse import build_tree, resolve_wikilinks
    from lib.kb.index_sync import sync_indexes

    sync_indexes(knowledge)
    cluster_root = (knowledge / "cluster/_index.md").read_text(encoding="utf-8")
    assert "[[cluster/alpha/hdfs/_index\\|打开]]" in cluster_root
    hdfs = (knowledge / "cluster/alpha/hdfs/_index.md").read_text(encoding="utf-8")
    assert "[[cluster/alpha/hdfs/namenode/_index\\|打开]]" in hdfs
    ts_root = (knowledge / "jobs/_index.md").read_text(encoding="utf-8")
    assert ts_root.startswith("---\ntitle: _index\nplatform: example/alpha\nservice: jobs\n")
    assert "# JobScheduler 任务" in ts_root and "任务告警与问答" in ts_root
    assert "jobs/task/alpha-jobs-failure-diagnosis" in ts_root
    assert not (knowledge / "_index.md").exists()

    bases, warnings = build_tree(knowledge)
    assert warnings == ()
    cluster, ts = bases
    assert (cluster.id, ts.id, ts.layout) == ("cluster", "jobs", "flat")
    hdfs_node = cluster.platforms[0].services[0]
    assert hdfs_node.index_doc == "cluster/alpha/hdfs/_index.md"
    assert [doc.component for doc in hdfs_node.docs] == [None, "namenode"]
    ts_service = ts.platforms[0].services[0]
    assert ts_service.index_doc == "jobs/_index.md"
    assert [doc.component for doc in ts_service.docs] == ["task"]

    body = (knowledge / "cluster/alpha/hdfs/alpha-hdfs-nn-heap.md").read_text(encoding="utf-8")
    link = resolve_wikilinks(body, knowledge)[0]
    assert link.resolved_path == "jobs/task/alpha-jobs-failure-diagnosis.md"


def test_rebuild_end_to_end_with_relocated_knowledge(knowledge: Path, tmp_path: Path, monkeypatch):
    from lib.kb.rebuild import rebuild
    from lib.kb.recall import search_recall_index

    repo = tmp_path / "ops-agent"
    _write(repo, "config/paths.local.yaml", f"knowledge_root: {knowledge}\n")
    monkeypatch.delenv("OPS_AGENT_KNOWLEDGE_ROOT", raising=False)
    rebuild(repo)
    hits = search_recall_index(repo / "build/kb-recall.sqlite", "任务失败下钻", "alpha")
    assert hits[0].path == "jobs/task/alpha-jobs-failure-diagnosis.md"
    assert (repo / "build/routing.json").is_file()


def test_oplog_reference_accepts_legacy_and_current_ids(knowledge: Path, tmp_path: Path, monkeypatch):
    from lib.record import OplogMissing, _validate_oplogs

    repo = tmp_path / "ops-agent"
    _write(repo, "config/paths.local.yaml", f"knowledge_root: {knowledge}\n")
    monkeypatch.delenv("OPS_AGENT_KNOWLEDGE_ROOT", raising=False)
    _write(knowledge, "cluster/oplog/beta/2026-09-04-hbase-rit.md", "x")
    _validate_oplogs(["knowledge/cluster/oplog/beta/2026-09-04-hbase-rit.md"], repo)
    _validate_oplogs(["knowledge/oplog/beta/2026-09-04-hbase-rit.md"], repo)
    with pytest.raises(OplogMissing):
        _validate_oplogs(["knowledge/cluster/alpha/hdfs/alpha-hdfs-nn-heap.md"], repo)
    with pytest.raises(OplogMissing):
        _validate_oplogs(["knowledge/oplog/beta/missing.md"], repo)


def test_manifest_platform_accepts_resource_ref_and_share_scope(knowledge: Path):
    _write(knowledge, "jobs/kb.yaml",
           "id: jobs\nname: JobScheduler 任务\nlayout: flat\n"
           "platform: example/alpha\nservice: jobs\nshare_scope: shared\n")
    bases = {base.id: base for base in discover_bases(knowledge)}
    assert bases["jobs"].platform == "alpha"
    assert (bases["jobs"].share_scope, bases["cluster"].share_scope) == ("shared", "private")
    _write(knowledge, "jobs/kb.yaml",
           "id: jobs\nname: JobScheduler 任务\nlayout: flat\nshare_scope: public\n")
    with pytest.raises(KnowledgeBaseError, match="share_scope"):
        discover_bases(knowledge)
