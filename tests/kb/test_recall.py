from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import lib.kb.recall as recall_module
from lib.kb.recall import (
    RecallIndexError,
    build_recall_index,
    search_recall_index,
    search_recall_index_candidates,
)




def _write_doc(
    root: Path,
    relative: str,
    *,
    title: str,
    platform: str,
    service: str,
    problem: str,
    symptoms: list[str],
    body: str,
    doc_type: str = "troubleshoot",
    playbook: str = "",
    status: str = "verified",
) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    symptoms_yaml = "[" + ", ".join(symptoms) + "]"
    path.write_text(
        "---\n"
        f"title: {title}\n"
        f"type: {doc_type}\n"
        f"platform: {platform}\n"
        f"service: {service}\n"
        f"problem: {problem}\n"
        f"symptoms: {symptoms_yaml}\n"
        "alerts: []\n"
        f"playbook: {playbook}\n"
        f"status: {status}\n"
        "---\n\n"
        f"{body}\n",
        encoding="utf-8",
    )


@pytest.fixture
def knowledge_root(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    _write_doc(
        root,
        "alpha/yarn/alpha-yarn-pressure.md",
        title="YARN 资源压力",
        platform="alpha",
        service="yarn",
        problem="yarn-vcore-memory-pressure",
        symptoms=["CPU vcore 占用高", "容器内存不足", "等待应用增加"],
        body="## 排查\n检查 ResourceManager 指标和等待队列。",
        playbook="playbooks/alpha/yarn/pressure.sh",
    )
    _write_doc(
        root,
        "alpha/hdfs/alpha-datanode-heartbeat.md",
        title="DataNode 心跳和 IO 延迟",
        platform="alpha",
        service="hdfs",
        problem="datanode-heartbeat-timeout",
        symptoms=["DataNode 心跳超时", "磁盘 IO 等待升高"],
        body="## 排查\n检查节点资源、磁盘队列和长时间 IO 等待。",
    )
    _write_doc(
        root,
        "beta/hdfs/beta-datanode-heartbeat.md",
        title="Beta DataNode 心跳和 IO 延迟",
        platform="beta",
        service="hdfs",
        problem="datanode-heartbeat-timeout",
        symptoms=["DataNode 心跳超时", "磁盘 IO 等待升高"],
        body="## 排查\nBeta 集群节点资源检查。",
    )
    _write_doc(
        root,
        "shared/procedures/shared-hdfs-resource-check.md",
        title="HDFS 节点资源检查",
        platform="shared",
        service="hdfs",
        problem="node-resource-check",
        symptoms=["心跳超时", "IO 等待"],
        body="## 方法\n检查 CPU、内存和磁盘 IO。",
        doc_type="procedure",
    )
    _write_doc(
        root,
        "alpha/hdfs/alpha-hdfs-oplog.md",
        title="HDFS 操作记录",
        platform="alpha",
        service="hdfs",
        problem="datanode-heartbeat-timeout",
        symptoms=["DataNode 心跳超时"],
        body="不应被检索。",
        doc_type="oplog",
    )
    _write_doc(
        root,
        "alpha/hdfs/_templates/template.md",
        title="模板 DataNode",
        platform="alpha",
        service="hdfs",
        problem="datanode-heartbeat-timeout",
        symptoms=["DataNode 心跳超时"],
        body="不应被检索。",
    )
    _write_doc(
        root,
        "_eval/eval-case.md",
        title="评测 DataNode",
        platform="alpha",
        service="hdfs",
        problem="datanode-heartbeat-timeout",
        symptoms=["DataNode 心跳超时"],
        body="不应被检索。",
    )
    index = root / "alpha/hdfs/_index.md"
    index.write_text(
        "---\ntitle: HDFS 索引\nplatform: alpha\nservice: hdfs\n---\n\n"
        "DataNode 心跳超时 IO 等待\n",
        encoding="utf-8",
    )
    return root


def test_build_excludes_non_search_documents(
    knowledge_root: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "build/kb-recall.sqlite"
    build_recall_index(knowledge_root, index_path)

    hits = search_recall_index(index_path, "hdfs datanode 心跳 IO", "alpha")

    paths = {hit.path for hit in hits}
    assert "alpha/hdfs/alpha-datanode-heartbeat.md" in paths
    assert "shared/procedures/shared-hdfs-resource-check.md" in paths
    assert all(not path.endswith("_index.md") for path in paths)
    assert all("_templates" not in path and "_eval" not in path for path in paths)
    assert all(hit.doc_type != "oplog" for hit in hits)


def test_build_persists_section_chunks_for_future_vector_retrieval(
    knowledge_root: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "build/kb-recall.sqlite"
    build_recall_index(knowledge_root, index_path)

    with sqlite3.connect(index_path) as connection:
        row = connection.execute(
            "select chunk_id, path, section, title, text from kb_chunks "
            "where path = ? and section = ?",
            ("alpha/yarn/alpha-yarn-pressure.md", "排查"),
        ).fetchone()

    assert row is not None
    assert row[0] == "alpha/yarn/alpha-yarn-pressure.md#section-01"
    assert row[3] == "YARN 资源压力"
    assert "等待队列" in row[4]


def test_search_returns_ranked_top_five_with_metadata_and_plain_snippets(
    knowledge_root: Path,
    tmp_path: Path,
) -> None:
    for index in range(6):
        _write_doc(
            knowledge_root,
            f"alpha/yarn/extra-{index}.md",
            title=f"YARN 等待应用 {index}",
            platform="alpha",
            service="yarn",
            problem="yarn-waiting-applications",
            symptoms=["等待应用增加"],
            body="容器内存和 CPU vcore 资源检查。",
        )
    index_path = tmp_path / "build/kb-recall.sqlite"
    build_recall_index(knowledge_root, index_path)

    hits = search_recall_index(
        index_path,
        "调查 yarn cpu 容器内存和等待应用",
        "alpha",
    )

    assert len(hits) == 5
    assert hits[0].score > 0
    assert hits[0].snippet
    assert "<b>" not in hits[0].snippet
    assert hits[0].service == "yarn"
    assert hits[0].status == "verified"


def test_search_includes_shared_and_excludes_other_platform(
    knowledge_root: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "kb-recall.sqlite"
    build_recall_index(knowledge_root, index_path)

    hits = search_recall_index(index_path, "hdfs datanode 心跳 IO", "alpha")

    assert {hit.platform for hit in hits} == {"alpha", "shared"}


def test_search_applies_service_as_hard_filter(
    knowledge_root: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "kb-recall.sqlite"
    build_recall_index(knowledge_root, index_path)

    hits = search_recall_index(
        index_path,
        "节点资源 CPU 内存 IO",
        "alpha",
        service="hdfs",
    )

    assert hits
    assert all(hit.service == "hdfs" for hit in hits)


def test_build_atomically_replaces_existing_index_without_temp_files(
    knowledge_root: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "build/kb-recall.sqlite"
    index_path.parent.mkdir(parents=True)
    index_path.write_text("old index", encoding="utf-8")

    build_recall_index(knowledge_root, index_path)

    assert search_recall_index(index_path, "yarn cpu", "alpha")
    assert list(index_path.parent.iterdir()) == [index_path]


@pytest.mark.parametrize("kind", ["missing", "corrupt", "wrong_schema"])
def test_search_reports_bad_index_without_mutating_it(
    tmp_path: Path,
    kind: str,
) -> None:
    index_path = tmp_path / "kb-recall.sqlite"
    if kind == "corrupt":
        index_path.write_bytes(b"not sqlite")
    elif kind == "wrong_schema":
        with sqlite3.connect(index_path) as connection:
            connection.execute("CREATE TABLE kb_meta(schema_version INTEGER)")
            connection.execute("INSERT INTO kb_meta VALUES (999)")
    before = index_path.read_bytes() if index_path.exists() else None

    with pytest.raises(RecallIndexError, match="scripts/build_wiki.py"):
        search_recall_index(index_path, "datanode", "alpha")

    after = index_path.read_bytes() if index_path.exists() else None
    assert after == before


def test_search_returns_empty_when_query_has_no_effective_terms(
    knowledge_root: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "kb-recall.sqlite"
    build_recall_index(knowledge_root, index_path)

    assert search_recall_index(index_path, "调查问题以及相关", "alpha") == ()


@pytest.mark.parametrize("query", ["心跳", "IO"])
def test_search_matches_two_character_terms(
    knowledge_root: Path,
    tmp_path: Path,
    query: str,
) -> None:
    index_path = tmp_path / "kb-recall.sqlite"
    build_recall_index(knowledge_root, index_path)

    hits = search_recall_index(index_path, query, "alpha", service="hdfs")

    assert "alpha/hdfs/alpha-datanode-heartbeat.md" in [hit.path for hit in hits]


def test_short_term_fallback_ranks_all_term_matches_before_partial_matches(
    knowledge_root: Path,
    tmp_path: Path,
) -> None:
    _write_doc(
        knowledge_root,
        "alpha/hbase/a-gc-only.md",
        title="GC 告警",
        platform="alpha",
        service="hbase",
        problem="",
        symptoms=[],
        body="检查 GC 日志。",
    )
    _write_doc(
        knowledge_root,
        "alpha/hbase/z-gc-frequent.md",
        title="GC 频繁",
        platform="alpha",
        service="hbase",
        problem="",
        symptoms=[],
        body="GC 频繁时检查堆内存。",
    )
    index_path = tmp_path / "kb-recall.sqlite"
    build_recall_index(knowledge_root, index_path)

    hits = search_recall_index(index_path, "gc 频繁", "alpha", service="hbase")

    assert [hit.path for hit in hits] == ["alpha/hbase/z-gc-frequent.md", "alpha/hbase/a-gc-only.md"]


def test_short_ascii_terms_do_not_match_inside_words(
    knowledge_root: Path,
    tmp_path: Path,
) -> None:
    _write_doc(
        knowledge_root,
        "alpha/hbase/a-region.md",
        title="区域连接检查",
        platform="alpha",
        service="hbase",
        problem="",
        symptoms=[],
        body="检查 region 连接状态。",
    )
    _write_doc(
        knowledge_root,
        "alpha/hbase/b-io.md",
        title="IO 等待升高",
        platform="alpha",
        service="hbase",
        problem="",
        symptoms=[],
        body="检查磁盘 IO。",
    )
    index_path = tmp_path / "kb-recall.sqlite"
    build_recall_index(knowledge_root, index_path)

    hits = search_recall_index(index_path, "io", "alpha", service="hbase")

    assert [hit.path for hit in hits] == ["alpha/hbase/b-io.md"]


def test_two_character_chinese_words_rank_with_longer_terms(
    knowledge_root: Path,
    tmp_path: Path,
) -> None:
    _write_doc(
        knowledge_root,
        "alpha/system/alpha-memory-pressure.md",
        title="主机内存压力排查",
        platform="alpha",
        service="system",
        problem="memory-pressure",
        symptoms=["节点内存使用率"],
        body="node12 内存与 swap 检查。",
    )
    _write_doc(
        knowledge_root,
        "alpha/yarn/alpha-node-inventory.md",
        title="节点清单",
        platform="alpha",
        service="yarn",
        problem="node-inventory",
        symptoms=["节点清单"],
        body="node12 node12 node12 运行 NodeManager。",
    )
    index_path = tmp_path / "kb-recall.sqlite"
    build_recall_index(knowledge_root, index_path)

    hits = search_recall_index(index_path, "检查alpha平台node12内存情况", "alpha")

    assert hits[0].path == "alpha/system/alpha-memory-pressure.md"


def test_platform_document_outranks_shared_document_of_unnamed_service(
    knowledge_root: Path,
    tmp_path: Path,
) -> None:
    _write_doc(
        knowledge_root,
        "alpha/system/alpha-network-restart.md",
        title="Alpha 集群网络中断服务停启 SOP",
        platform="alpha",
        service="system",
        problem="cluster-network-restart",
        symptoms=[],
        body="网络中断前停止服务，恢复后检查指标。",
        doc_type="procedure",
    )
    _write_doc(
        knowledge_root,
        "shared/procedures/mysql-network-restart.md",
        title="MySQL 集群网络中断服务停启 SOP",
        platform="shared",
        service="mysql",
        problem="cluster-network-restart",
        symptoms=[],
        body="网络重启前停止服务，网络重启后检查复制指标。网络重启注意事项。",
        doc_type="procedure",
    )
    index_path = tmp_path / "kb-recall.sqlite"
    build_recall_index(knowledge_root, index_path)

    hd_paths = [
        hit.path
        for hit in search_recall_index(index_path, "alpha集群网络重启前是否需要停止服务", "alpha")
    ]
    mysql_hits = search_recall_index(index_path, "mysql集群网络重启前是否需要停止服务", "alpha")

    assert hd_paths[0] == "alpha/system/alpha-network-restart.md"
    assert hd_paths.index("shared/procedures/mysql-network-restart.md") > 0
    assert mysql_hits[0].path == "shared/procedures/mysql-network-restart.md"


def test_identifier_parts_match_sibling_alert_names(
    knowledge_root: Path,
    tmp_path: Path,
) -> None:
    _write_doc(
        knowledge_root,
        "shared/hdfs/nn-heap.md",
        title="NameNode Heap 排查",
        platform="shared",
        service="hdfs",
        problem="namenode-heap",
        symptoms=["increase_nn_heap_usage_daily"],
        body="关注堆增长率。",
    )
    index_path = tmp_path / "kb-recall.sqlite"
    build_recall_index(knowledge_root, index_path)

    hits = search_recall_index(index_path, "排查告警：increase_nn_heap_usage_weekly", "beta")

    assert hits[0].path == "shared/hdfs/nn-heap.md"


def test_candidates_without_platform_cover_every_platform(
    knowledge_root: Path,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "kb-recall.sqlite"
    build_recall_index(knowledge_root, index_path)

    hits = search_recall_index_candidates(index_path, "DataNode 心跳", None)

    assert {"alpha", "beta", "shared"} <= {hit.platform for hit in hits}


def test_recall_index_connections_are_closed(
    knowledge_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_connect = sqlite3.connect
    connections: list[sqlite3.Connection] = []

    class TrackingConnection(sqlite3.Connection):
        closed = False

        def close(self) -> None:
            self.closed = True
            super().close()

    def tracked_connect(*args, **kwargs):
        kwargs["factory"] = TrackingConnection
        connection = original_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(recall_module.sqlite3, "connect", tracked_connect)
    index_path = tmp_path / "kb-recall.sqlite"
    build_recall_index(knowledge_root, index_path)
    assert len(connections) == 1
    assert all(connection.closed for connection in connections)

    search_recall_index(index_path, "datanode", "alpha")

    assert len(connections) == 2
    assert all(connection.closed for connection in connections)
