from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.kb.recall import build_recall_index
from lib.kb.retrieval import RetrievalRequest, RetrievalService, build_context, build_retrieval_index
from lib.kb.retrieval.contracts import normalize_context_pack
from lib.kb.retrieval.vector_store import EmbeddedVectorStore, VectorRecord


def _doc(
    root: Path,
    relative: str,
    *,
    title: str,
    platform: str,
    service: str,
    problem: str,
    profile: str,
    doc_type: str,
    summary: str,
    body: str,
    symptoms: list[str] | None = None,
    alerts: list[str] | None = None,
) -> None:
    path = root / "knowledge" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        "schema_version: 2",
        "source: manual_import",
        "source_ref: test",
        f"title: {title}",
        f"type: {doc_type}",
        f"platform: {platform}",
        f"service: {service}",
        f"problem: {problem}",
        f"summary: {summary}",
        "knowledge_layer: basic_info" if profile != "diagnosis" else "knowledge_layer: alert",
        "entrypoints: [manual_task]",
        f"profile: {profile}",
        "execution_scope: agent_readonly",
        "confidence: observed",
        "related: []",
    ]
    if doc_type == "troubleshoot":
        lines.extend([
            f"symptoms: {json.dumps(symptoms or [], ensure_ascii=False)}",
            f"alerts: {json.dumps(alerts or [], ensure_ascii=False)}",
            "playbooks: []",
            "diagnostic_paths: []",
        ])
    lines.extend(["---", "", f"# {title}", "", "## 摘要", "", summary, "", body, ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def _repo(tmp_path: Path) -> Path:
    root = tmp_path
    _doc(
        root,
        "alpha/hdfs/alpha-hdfs-baseline.md",
        title="HDFS 服务基线",
        platform="alpha",
        service="hdfs",
        problem="baseline",
        profile="baseline",
        doc_type="knowledge",
        summary="DataNode 节点、关键配置和服务事实基线。",
        body="## 组件与节点\n\nDataNode 位于 node4~node17。\n\n## 关键配置\n\ndfs.replication=3。",
    )
    _doc(
        root,
        "alpha/hdfs/datanode/alpha-datanode-health.md",
        title="DataNode 心跳健康排查",
        platform="alpha",
        service="hdfs",
        problem="datanode-health",
        profile="diagnosis",
        doc_type="troubleshoot",
        summary="DataNode 心跳异常时检查进程、端口、磁盘和 OOM。",
        body="## 关键判据\n\nDataNode 心跳超时需要确认端口和日志。\n\n## 排查步骤\n\n检查 /var/log/hadoop/hdfs。",
        symptoms=["DataNode 心跳超时"],
        alerts=["DataNode Health Summary"],
    )
    _doc(
        root,
        "shared/procedures/logs.md",
        title="日志排查流程",
        platform="shared",
        service="system",
        problem="logs",
        profile="sop",
        doc_type="procedure",
        summary="按登记的日志目录和时间窗口读取日志。",
        body="## 步骤\n\n先确认日志目录和节点范围。",
    )
    _doc(
        root,
        "beta/hdfs/beta-datanode-health.md",
        title="Beta DataNode 健康排查",
        platform="beta",
        service="hdfs",
        problem="datanode-health",
        profile="diagnosis",
        doc_type="troubleshoot",
        summary="Beta DataNode 心跳检查。",
        body="## 排查步骤\n\n检查 Beta 节点。",
        symptoms=["DataNode 心跳超时"],
    )
    config = root / "config"
    config.mkdir()
    (config / "service-baseline.yaml").write_text(
        "service_map:\n  HDFS: hdfs\n"
        "config_whitelist:\n  HDFS:\n    hdfs-site: [dfs.replication, dfs.datanode.data.dir]\n"
        "probes: {}\n",
        encoding="utf-8",
    )
    (config / "log-catalog.yaml").write_text(
        "version: 1\nplatforms:\n  alpha:\n    collected_at: test\n    services:\n"
        "      hdfs:\n        - path: /var/log/hadoop/hdfs\n          nodes: all\n",
        encoding="utf-8",
    )
    build = root / "build"
    build.mkdir()
    (build / "kb-topology.json").write_text(json.dumps({
        "schema_version": 1,
        "generated_at": "2026-09-01T00:00:00Z",
        "source_fingerprint": "platform-fingerprint",
        "warnings": [],
        "nodes": [
            {"id": "service:alpha:hdfs", "kind": "service", "name": "hdfs", "platform": "alpha", "properties": {}, "provenance": []},
            {"id": "component:alpha:DATANODE", "kind": "component", "name": "DATANODE", "platform": "alpha", "properties": {}, "provenance": []},
            {"id": "document:alpha/hdfs/alpha-hdfs-baseline.md", "kind": "document", "name": "HDFS 服务基线", "platform": "alpha", "properties": {}, "provenance": []},
            {"id": "document:alpha/hdfs/datanode/alpha-datanode-health.md", "kind": "document", "name": "DataNode 心跳健康排查", "platform": "alpha", "properties": {}, "provenance": []},
        ],
        "edges": [
            {"id": "component-datanode-hdfs", "source": "component:alpha:DATANODE", "target": "service:alpha:hdfs", "relation": "component_of", "properties": {}, "provenance": []},
            {"id": "baseline-doc-hdfs", "source": "document:alpha/hdfs/alpha-hdfs-baseline.md", "target": "service:alpha:hdfs", "relation": "documents", "properties": {}, "provenance": []},
            {"id": "health-doc-hdfs", "source": "document:alpha/hdfs/datanode/alpha-datanode-health.md", "target": "service:alpha:hdfs", "relation": "documents", "properties": {}, "provenance": []},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    (root / "state").mkdir()
    (root / "state" / "graphs" / "jobs").mkdir(parents=True)
    build_recall_index(root / "knowledge", build / "kb-recall.sqlite")
    return root


def test_investigation_context_contains_baseline_topology_and_chunk_refs(tmp_path: Path):
    root = _repo(tmp_path)
    from lib.kb.retrieval import build_retrieval_index

    build_retrieval_index(root)
    pack = build_context(root, RetrievalRequest(
        query="排查Alpha集群偶发DataNode心跳断开问题",
        profile="investigation",
        platform="alpha",
        scope="cluster",
    )).to_dict()

    assert pack["scope"]["service"] == "hdfs"
    assert "alpha/hdfs/alpha-hdfs-baseline.md" in [
        item["doc_id"] for item in pack["baseline_context"]["sources"]
    ]
    assert any(item["key"] == "hdfs-site.dfs.replication" for item in pack["baseline_context"]["config_refs"])
    assert any(item["key"] == "/var/log/hadoop/hdfs" for item in pack["baseline_context"]["log_locations"])
    assert "component:alpha:DATANODE" in pack["topology_context"]["anchors"]
    assert any("component_of" in item["relations"] for item in pack["topology_context"]["paths"])
    assert any(item["doc_id"] == "alpha/hdfs/datanode/alpha-datanode-health.md" for item in pack["knowledge_context"]["diagnosis"])
    assert all(item["chunk_ids"] for bucket in pack["knowledge_context"].values() for item in bucket)

    def contains_large_text(value):
        if isinstance(value, dict):
            assert "text" not in value
            return any(contains_large_text(item) for item in value.values())
        if isinstance(value, list):
            return any(contains_large_text(item) for item in value)
        return False

    assert not contains_large_text(pack)


def test_service_scope_reduces_broad_investigation_baseline_context(tmp_path: Path):
    root = _repo(tmp_path)
    _doc(
        root,
        "alpha/yarn/alpha-yarn-baseline.md",
        title="YARN 服务基线",
        platform="alpha",
        service="yarn",
        problem="baseline",
        profile="baseline",
        doc_type="knowledge",
        summary="YARN 节点和资源配置基线。",
        body="## 关键配置\n\nyarn.nodemanager.resource.memory-mb=1024。",
    )
    from lib.kb.retrieval import build_retrieval_index

    build_retrieval_index(root)
    request = {
        "query": "排查 node1 当前资源异常",
        "profile": "investigation",
        "platform": "alpha",
        "scope": "node",
        "node": "node1",
    }
    broad = build_context(root, RetrievalRequest(**request)).to_dict()
    scoped = build_context(
        root,
        RetrievalRequest(**request, service="hdfs", component="datanode"),
    ).to_dict()

    broad_sources = {
        item["doc_id"] for item in broad["baseline_context"]["sources"]
    }
    scoped_sources = {
        item["doc_id"] for item in scoped["baseline_context"]["sources"]
    }
    assert "alpha/yarn/alpha-yarn-baseline.md" in broad_sources
    assert "alpha/yarn/alpha-yarn-baseline.md" not in scoped_sources
    assert scoped["scope"]["service"] == "hdfs"
    assert len(scoped_sources) < len(broad_sources)
    # 未定服务时全平台基线只作参考,不进必读;定了服务才把该服务基线列为必读
    assert not {item["doc_id"] for item in broad["required_reads"]} & broad_sources
    assert "alpha/hdfs/alpha-hdfs-baseline.md" in {
        item["doc_id"] for item in scoped["required_reads"]
    }


def test_required_reads_rank_by_relevance_and_drop_off_scope_procedures(tmp_path: Path):
    root = _repo(tmp_path)
    _doc(
        root,
        "shared/procedures/redis-sentinel-deploy.md",
        title="Redis 哨兵缓存部署 SOP",
        platform="shared",
        service="redis",
        problem="redis-sentinel-deploy",
        profile="sop",
        doc_type="procedure",
        summary="哨兵心跳检查脚本确认本机是 master 后才持有 VIP。",
        body="## 步骤\n\n节点重启后检查哨兵心跳。",
    )
    build_recall_index(root / "knowledge", root / "build" / "kb-recall.sqlite")
    from lib.kb.retrieval import build_retrieval_index

    build_retrieval_index(root)
    pack = build_context(root, RetrievalRequest(
        query="排查Alpha集群偶发DataNode心跳断开问题",
        profile="investigation",
        platform="alpha",
        scope="cluster",
    )).to_dict()

    reads = [item["doc_id"] for item in pack["required_reads"]]
    # 最贴近症状的故障文档先于服务基线；只靠通用词沾边的他服务 SOP 不进必读
    assert reads[0] == "alpha/hdfs/datanode/alpha-datanode-health.md"
    assert reads.index("alpha/hdfs/alpha-hdfs-baseline.md") > 0
    assert "shared/procedures/redis-sentinel-deploy.md" not in reads


def test_platform_is_inferred_only_from_a_service_registered_on_one_platform(tmp_path: Path):
    root = _repo(tmp_path)
    topology_path = root / "build" / "kb-topology.json"
    topology = json.loads(topology_path.read_text(encoding="utf-8"))
    topology["nodes"] += [
        {"id": "service:alpha:jobs", "kind": "service", "name": "jobs", "platform": "alpha", "properties": {}, "provenance": []},
        {"id": "service:beta:hdfs", "kind": "service", "name": "hdfs", "platform": "beta", "properties": {}, "provenance": []},
    ]
    topology_path.write_text(json.dumps(topology, ensure_ascii=False), encoding="utf-8")
    from lib.kb.retrieval import build_retrieval_index

    build_retrieval_index(root)
    unique = build_context(root, RetrievalRequest(query="JobScheduler 任务一直失败", profile="qa")).to_dict()
    ambiguous = build_context(root, RetrievalRequest(query="HDFS 副本数配置", profile="qa")).to_dict()

    assert unique["scope"]["platform"] == "alpha"
    assert "未确定平台" not in unique["gaps"]
    platform_entity = next(item for item in unique["entities"] if item["kind"] == "platform")
    assert platform_entity["match_type"] == "inferred_from_service"
    assert ambiguous["scope"]["platform"] is None
    assert "未确定平台" in ambiguous["gaps"]


def test_context_pack_v2_is_reference_only_and_drops_legacy_body_fields():
    pack = normalize_context_pack({
        "schema_version": 1,
        "baseline_context": {
            "sources": [{
                "doc_id": "alpha/system/baseline.md",
                "summary": "legacy body-like summary",
                "chunk_ids": ["alpha/system/baseline.md#section-01"],
            }],
        },
        "knowledge_context": {
            "diagnosis": [{
                "doc_id": "alpha/hdfs/health.md",
                "chunk_ids": ["alpha/hdfs/health.md#section-02"],
                "text": "must never reach the Agent",
                "highlights": ["also forbidden"],
                "key_facts": ["also forbidden"],
            }],
        },
        "required_reads": ["alpha/hdfs/health.md"],
        "retrieval_meta": {},
    })

    assert pack["schema_version"] == 2
    assert pack["retrieval_meta"]["reference_only"] is True
    assert pack["required_reads"][0]["doc_id"] == "alpha/hdfs/health.md"
    source = pack["baseline_context"]["sources"][0]
    assert source["doc_id"] == "alpha/system/baseline.md"
    assert "summary" not in source
    diagnosis = pack["knowledge_context"]["diagnosis"][0]
    assert diagnosis["chunk_ids"] == ["alpha/hdfs/health.md#section-02"]
    assert "text" not in diagnosis
    assert "highlights" not in diagnosis
    assert "key_facts" not in diagnosis


def test_context_pack_v2_rejects_body_shaped_reference_fields():
    pack = normalize_context_pack({
        "schema_version": 2,
        "knowledge_context": {
            "diagnosis": [{
                "doc_id": "doc-a",
                "chunk_ids": ["doc-a#section-01"],
                "content": "not allowed",
            }],
        },
        "required_reads": [],
    })
    ref = pack["knowledge_context"]["diagnosis"][0]
    assert "content" not in ref
    assert ref["doc_id"] == "doc-a"


def test_context_pack_rejects_absolute_and_traversal_document_refs():
    with pytest.raises(ValueError):
        normalize_context_pack({
            "schema_version": 2,
            "knowledge_context": {"diagnosis": [{"path": "/etc/passwd"}]},
        })
    with pytest.raises(ValueError):
        normalize_context_pack({
            "schema_version": 2,
            "knowledge_context": {"diagnosis": [{"doc_id": "../secret.md"}]},
        })


def test_qa_profile_does_not_add_diagnosis_or_procedures(tmp_path: Path):
    root = _repo(tmp_path)
    from lib.kb.retrieval import build_retrieval_index

    build_retrieval_index(root)
    pack = build_context(root, RetrievalRequest(
        query="Alpha HDFS DataNode 基础信息", profile="qa", platform="alpha",
    )).to_dict()
    assert pack["knowledge_context"]["diagnosis"] == []
    assert pack["knowledge_context"]["procedures"] == []


def test_alert_fallback_switches_to_investigation_profile(tmp_path: Path):
    root = _repo(tmp_path)
    from lib.kb.retrieval import build_retrieval_index

    build_retrieval_index(root)
    pack = build_context(root, RetrievalRequest(
        query="DataNode Health Summary",
        profile="alert",
        platform="alpha",
        alertname="DataNode Health Summary",
        retrieval_stage="alert_fallback",
        fallback_count=1,
        execution_summary={"failure_reason": "insufficient"},
    )).to_dict()
    assert pack["retrieval_profile"] == "investigation"
    assert pack["retrieval_meta"]["fallback_requested"] is True
    assert pack["knowledge_context"]["diagnosis"]


def test_read_chunks_is_bounded_and_preserves_ids(tmp_path: Path):
    root = _repo(tmp_path)
    from lib.kb.retrieval import build_retrieval_index

    build_retrieval_index(root)
    service = RetrievalService(root)
    pack = service.build_context(RetrievalRequest(
        query="DataNode 心跳", profile="investigation", platform="alpha",
    )).to_dict()
    chunk_id = pack["knowledge_context"]["diagnosis"][0]["chunk_ids"][0]
    chunks = service.read_chunks([chunk_id], max_chars=80)
    assert chunks[0]["chunk_id"] == chunk_id
    assert len(chunks[0]["text"]) <= 80


def test_read_chunks_applies_total_output_limit(tmp_path: Path):
    root = _repo(tmp_path)
    from lib.kb.retrieval import build_retrieval_index

    build_retrieval_index(root)
    service = RetrievalService(root)
    pack = service.build_context(RetrievalRequest(
        query="DataNode 心跳", profile="investigation", platform="alpha",
    )).to_dict()
    chunk_ids = pack["knowledge_context"]["diagnosis"][0]["chunk_ids"]
    chunks = service.read_chunks(chunk_ids, max_chars=100)
    assert sum(len(item["text"]) for item in chunks) <= 100


def test_embedded_vector_store_filters_and_sorts(tmp_path: Path):
    path = tmp_path / "vectors.sqlite"
    EmbeddedVectorStore.initialize(path, [
        VectorRecord("a", "chunk", "a", "doc-a", "doc-a#section-01", "alpha", "hdfs", "g", "s", (1.0, 0.0)),
        VectorRecord("b", "chunk", "b", "doc-b", "doc-b#section-01", "beta", "hdfs", "g", "s", (0.0, 1.0)),
    ])
    hits = EmbeddedVectorStore(path).search((1.0, 0.0), {"platform": "alpha"}, 5)
    assert [item.doc_id for item in hits] == ["doc-a"]


class _FakeReranker:
    model_id = "test-cross-encoder"
    max_candidates = 20

    def __init__(self):
        self.calls: list[tuple[str, list[str]]] = []

    def rerank(self, query: str, documents: list[str]) -> tuple[float, ...]:
        self.calls.append((query, documents))
        return tuple(1.0 if "心跳" in document else 0.0 for document in documents)


class _BrokenReranker(_FakeReranker):
    def rerank(self, query: str, documents: list[str]) -> tuple[float, ...]:
        self.calls.append((query, documents))
        raise RuntimeError("test model failure")


def test_cross_encoder_rerank_is_opt_in_and_returns_scores_without_full_text(tmp_path: Path):
    root = _repo(tmp_path)
    from lib.kb.retrieval import build_retrieval_index

    build_retrieval_index(root)
    reranker = _FakeReranker()
    pack = RetrievalService(root, rerank_provider=reranker).build_context(RetrievalRequest(
        query="排查Alpha集群偶发DataNode心跳断开问题",
        profile="investigation",
        platform="alpha",
    )).to_dict()

    assert pack["retrieval_meta"]["rerank_used"] is True
    assert pack["retrieval_meta"]["rerank_status"] == "ok"
    assert pack["retrieval_meta"]["rerank_candidate_count"] == len(reranker.calls[0][1])
    refs = pack["knowledge_context"]["diagnosis"]
    assert any("cross_encoder_match" in item["retrieval_reasons"] for item in refs)
    assert all("rerank" in item["scores"] for item in refs)
    assert all("text" not in item and "body" not in item for item in refs)

    qa = RetrievalService(root, rerank_provider=reranker).build_context(RetrievalRequest(
        query="Alpha HDFS DataNode 基础信息", profile="qa", platform="alpha",
    )).to_dict()
    assert qa["retrieval_meta"]["rerank_used"] is False
    assert qa["retrieval_meta"]["rerank_status"] == "disabled"


def test_cross_encoder_failure_falls_back_to_existing_scores(tmp_path: Path):
    root = _repo(tmp_path)
    from lib.kb.retrieval import build_retrieval_index

    build_retrieval_index(root)
    reranker = _BrokenReranker()
    pack = RetrievalService(root, rerank_provider=reranker).build_context(RetrievalRequest(
        query="排查Alpha集群偶发DataNode心跳断开问题",
        profile="investigation",
        platform="alpha",
    )).to_dict()

    assert pack["retrieval_meta"]["rerank_used"] is False
    assert pack["retrieval_meta"]["rerank_status"] == "error"
    refs = [item for bucket in pack["knowledge_context"].values() for item in bucket]
    assert refs
    assert all(item["scores"]["rerank"] is None for item in refs)


def test_initial_standard_alert_does_not_call_cross_encoder(tmp_path: Path):
    root = _repo(tmp_path)
    from lib.kb.retrieval import build_retrieval_index

    build_retrieval_index(root)
    reranker = _FakeReranker()
    pack = RetrievalService(root, rerank_provider=reranker).build_context(RetrievalRequest(
        query="DataNode Health Summary",
        profile="alert",
        platform="alpha",
        alertname="DataNode Health Summary",
    )).to_dict()

    assert reranker.calls == []
    assert pack["retrieval_meta"]["rerank_status"] == "disabled"


def test_alert_fallback_can_use_cross_encoder_in_second_investigation_pass(tmp_path: Path):
    root = _repo(tmp_path)
    from lib.kb.retrieval import build_retrieval_index

    build_retrieval_index(root)
    reranker = _FakeReranker()
    pack = RetrievalService(root, rerank_provider=reranker).build_context(RetrievalRequest(
        query="DataNode Health Summary",
        profile="alert",
        platform="alpha",
        alertname="DataNode Health Summary",
        retrieval_stage="alert_fallback",
        fallback_count=1,
        execution_summary={"failure_reason": "insufficient"},
    )).to_dict()

    assert pack["retrieval_profile"] == "investigation"
    assert pack["retrieval_meta"]["rerank_used"] is True
    assert reranker.calls


class _CountingEmbedder:
    model_id = "fake"

    def __init__(self):
        self.texts: list[str] = []

    def embed(self, texts):
        self.texts.extend(texts)
        return tuple((float(len(text)), 1.0) for text in texts)


def _vector_repo(tmp_path: Path, monkeypatch, model: str = "fake-model"):
    from lib.kb.retrieval import vector_sync

    root = _repo(tmp_path)
    (root / "config" / "vector.local.yaml").write_text(
        f"embedding:\n  backend: external\n  model: {model}\n  endpoint: http://127.0.0.1:9\n",
        encoding="utf-8",
    )
    embedder = _CountingEmbedder()
    monkeypatch.setattr(vector_sync, "create_embedding_provider", lambda _root: embedder)
    build_retrieval_index(root)
    return root, embedder


def test_vector_sync_embeds_only_new_or_changed_chunks(tmp_path: Path, monkeypatch):
    from lib.kb.retrieval.vector_sync import sync_vectors

    root, embedder = _vector_repo(tmp_path, monkeypatch)
    first = sync_vectors(root)
    total = first["records"]
    assert first["status"] == "ok" and first["embedded"] == total > 0
    vectors = root / "build" / "kb-vectors.sqlite"
    mtime = vectors.stat().st_mtime_ns

    second = sync_vectors(root)
    assert (second["embedded"], second["reused"], second["removed"]) == (0, total, 0)
    assert vectors.stat().st_mtime_ns == mtime  # 无变化不重写

    embedder.texts.clear()
    logs = root / "knowledge" / "shared/procedures/logs.md"
    logs.write_text(logs.read_text(encoding="utf-8").replace("先确认日志目录", "先确认日志目录与保留期"),
                    encoding="utf-8")
    (root / "knowledge" / "beta/hdfs/beta-datanode-health.md").unlink()
    build_retrieval_index(root)
    third = sync_vectors(root)
    assert third["removed"] > 0
    assert 0 < third["embedded"] < total
    assert all("Beta" not in text for text in embedder.texts)
    stored = EmbeddedVectorStore(vectors).records()
    assert len(stored) == third["records"]
    assert not any(item.doc_id.startswith("beta/") for item in stored)


def test_vector_sync_reembeds_everything_when_model_changes(tmp_path: Path, monkeypatch):
    from lib.kb.retrieval.vector_sync import sync_vectors

    root, _ = _vector_repo(tmp_path, monkeypatch)
    total = sync_vectors(root)["records"]
    (root / "config" / "vector.local.yaml").write_text(
        "embedding:\n  backend: external\n  model: other-model\n  endpoint: http://127.0.0.1:9\n",
        encoding="utf-8",
    )
    result = sync_vectors(root)
    assert (result["embedded"], result["reused"]) == (total, 0)


def test_vector_sync_is_noop_without_embedding_config(tmp_path: Path):
    from lib.kb.retrieval.vector_sync import sync_vectors

    root = _repo(tmp_path)
    build_retrieval_index(root)
    assert sync_vectors(root) == {"status": "disabled"}
    assert not (root / "build" / "kb-vectors.sqlite").exists()


def _with_hbase_depends_on_hdfs(root: Path) -> None:
    topology_path = root / "build" / "kb-topology.json"
    topology = json.loads(topology_path.read_text(encoding="utf-8"))
    topology["nodes"].append({"id": "service:alpha:hbase", "kind": "service", "name": "hbase",
                              "platform": "alpha", "properties": {}, "provenance": []})
    topology["edges"].append({"id": "hbase-depends-hdfs", "source": "service:alpha:hbase",
                              "target": "service:alpha:hdfs", "relation": "depends_on",
                              "properties": {}, "provenance": [{"kind": "snapshot_stack", "path": "x"}]})
    topology_path.write_text(json.dumps(topology, ensure_ascii=False), encoding="utf-8")
    build_retrieval_index(root)


def test_investigation_follows_depends_on_to_upstream_diagnosis(tmp_path: Path):
    root = _repo(tmp_path)
    _with_hbase_depends_on_hdfs(root)
    pack = build_context(root, RetrievalRequest(
        query="HBase 写入失败,心跳超时", profile="investigation", platform="alpha",
    )).to_dict()
    paths = pack["topology_context"]["dependency_paths"]
    assert [(p["from"], p["to"], p["hop"]) for p in paths] == [("service:alpha:hbase", "service:alpha:hdfs", 1)]
    diagnosis = {item["doc_id"]: item for item in pack["knowledge_context"]["diagnosis"]}
    upstream = diagnosis["alpha/hdfs/datanode/alpha-datanode-health.md"]
    assert "upstream_dependency" in upstream["retrieval_reasons"]
    assert pack["retrieval_meta"]["dependency_hops"] == 2


def test_upstream_documents_still_need_query_overlap_and_qa_does_not_expand(tmp_path: Path):
    root = _repo(tmp_path)
    _with_hbase_depends_on_hdfs(root)
    unrelated = build_context(root, RetrievalRequest(
        query="HBase Region 分裂过多", profile="investigation", platform="alpha",
    )).to_dict()
    assert unrelated["retrieval_meta"]["dependency_candidate_count"] >= 1   # 扩展到了上游
    assert "alpha/hdfs/datanode/alpha-datanode-health.md" not in {                # 但内容不沾边不收
        item["doc_id"] for item in unrelated["knowledge_context"]["diagnosis"]
    }
    qa = build_context(root, RetrievalRequest(
        query="HBase 写入失败,心跳超时", profile="qa", platform="alpha",
    )).to_dict()
    assert qa["topology_context"].get("dependency_paths") is None
    assert qa["retrieval_meta"]["dependency_hops"] == 0
