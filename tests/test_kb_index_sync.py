from pathlib import Path

import pytest

from lib.kb.index_sync import END, START, sync_indexes


def _doc(root: Path, relative: str, *, title: str, platform: str,
         service: str, problem: str, status: str = "draft",
         alerts: str = "[]", doc_type: str = "troubleshoot") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"title: {title}\n"
        f"type: {doc_type}\nplatform: {platform}\nservice: {service}\n"
        f"problem: {problem}\nsymptoms: [症状]\nalerts: {alerts}\n"
        f"playbook: \nstatus: {status}\n---\n\n# {title}\n",
        encoding="utf-8",
    )
    return path


def _fixture(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    _doc(root, "alpha/hdfs/alpha-hdfs-baseline.md", title="HDFS 服务基线",
         platform="alpha", service="hdfs", problem="baseline", doc_type="knowledge")
    _doc(root, "alpha/hdfs/namenode/alpha-namenode-health.md",
         title="NameNode 健康排查", platform="alpha", service="hdfs",
         problem="namenode-health", alerts="[NameNodeDown]")
    _doc(root, "beta/emqx/beta-emqx-memory.md", title="EMQX 内存排查",
         platform="beta", service="emqx", problem="memory", status="verified")
    (root / "_index.md").write_text(
        "---\ntitle: _index\nplatform: shared\n---\n"
        "# 旧根索引\n\n人工入口说明。\n\n## Alpha 平台\n\n旧路由。\n",
        encoding="utf-8",
    )
    service = root / "alpha/hdfs/_index.md"
    service.write_text(
        "---\ntitle: _index\nplatform: alpha\nservice: hdfs\n---\n"
        "# HDFS\n\n## 概述\n\n人工维护的 HA 架构说明。\n\n"
        "## 相关排查\n\n旧静态表。\n\n## 端口\n\n8020。\n",
        encoding="utf-8",
    )
    return root


def test_sync_preserves_manual_sections_and_replaces_legacy_lists(tmp_path):
    root = _fixture(tmp_path)
    changed = sync_indexes(root)
    assert "_index.md" in changed
    service = (root / "alpha/hdfs/_index.md").read_text(encoding="utf-8")
    assert "人工维护的 HA 架构说明" in service
    assert "## 端口\n\n8020" in service
    assert "旧静态表" not in service
    assert START in service and END in service
    assert "HDFS 服务基线" in service
    assert "NameNode 健康排查" in service
    assert "组件概览" in service


def test_sync_creates_missing_service_and_component_indexes(tmp_path):
    root = _fixture(tmp_path)
    sync_indexes(root)
    emqx = root / "beta/emqx/_index.md"
    namenode = root / "alpha/hdfs/namenode/_index.md"
    assert emqx.is_file() and namenode.is_file()
    assert "EMQX 内存排查" in emqx.read_text(encoding="utf-8")
    component = namenode.read_text(encoding="utf-8")
    assert "NameNode 健康排查" in component
    assert START in component


def test_root_index_is_current_and_generated_from_documents(tmp_path):
    root = _fixture(tmp_path)
    sync_indexes(root)
    text = (root / "_index.md").read_text(encoding="utf-8")
    assert "人工入口说明" in text
    assert "旧路由" not in text
    assert "| HDFS | 2 | 1 | 1 | 2 | 0 |" in text
    assert "| EMQX | 1 | 1 | 0 | 0 | 1 |" in text
    assert "NameNodeDown" in text
    assert "alpha/hdfs/namenode/alpha-namenode-health" in text
    assert "[[alpha/hdfs/_index\\|打开]]" in text


def test_generated_table_wikilinks_resolve_with_escaped_alias_separator(tmp_path):
    from lib.kb.browse import resolve_wikilinks

    root = _fixture(tmp_path)
    sync_indexes(root)
    text = (root / "_index.md").read_text(encoding="utf-8")
    link = next(link for link in resolve_wikilinks(text, root)
                if link.target == "alpha/hdfs/_index")
    assert link.label == "打开"
    assert link.resolved_path == "alpha/hdfs/_index.md"


def test_service_index_renders_observed_placement_from_platform_config(tmp_path):
    root = _fixture(tmp_path)
    config = tmp_path / "config"
    config.mkdir()
    (config / "platform-alpha.yaml").write_text(
        "platform: alpha\ncollected_at: '2026-07-13T15:55:30+00:00'\nnodes:\n"
        "  node12: {host: 198.51.100.112, components: [DATANODE, JOURNALNODE]}\n"
        "  node2: {host: 198.51.100.102, components: [DATANODE, KAFKA_BROKER]}\n"
        "  node1: {host: 198.51.100.101, components: [NAMENODE, UNKNOWN_THING]}\n",
        encoding="utf-8",
    )
    sync_indexes(root, config)
    hdfs = (root / "alpha/hdfs/_index.md").read_text(encoding="utf-8")
    generated = hdfs.split(START, 1)[1]
    assert "## 组件分布" in generated
    assert "`config/platform-alpha.yaml` 生成（采集于 2026-07-13）" in generated
    assert "| DATANODE | 2 | node2, node12 |" in generated
    assert "| JOURNALNODE | 1 | node12 |" in generated
    assert "| NAMENODE | 1 | node1 |" in generated
    assert "KAFKA_BROKER" not in generated and "UNKNOWN_THING" not in generated
    assert generated.index("## 组件分布") < generated.index("## 自动文档目录")
    # 无 platform 配置的平台不生成分布表,也不报错
    assert "## 组件分布" not in (root / "beta/emqx/_index.md").read_text(encoding="utf-8")
    assert sync_indexes(root, config) == ()


def test_service_index_renders_dependencies_and_ports_from_topology(tmp_path):
    import json

    root = _fixture(tmp_path)
    _doc(root, "alpha/zookeeper/alpha-zookeeper-x.md", title="ZK", platform="alpha",
         service="zookeeper", problem="x")
    config = tmp_path / "config"
    (config / "topology").mkdir(parents=True)
    (config / "platform-alpha.yaml").write_text(
        "platform: alpha\ncollected_at: '2026-10-12'\nnodes:\n"
        "  node1: {host: 198.51.100.101, components: [NAMENODE]}\n", encoding="utf-8")
    (config / "topology" / "snapshot-alpha.json").write_text(json.dumps({
        "schema_version": 3, "collected_at": "2026-10-12T08:00:00Z", "source_system": "Demo",
        "component_ports": [{"component": "NAMENODE", "port": 8020, "purpose": "rpc"},
                            {"component": "NAMENODE", "port": 50070, "purpose": "http"}],
        "stack_dependencies": [{"source_service": "hdfs", "target_service": "zookeeper"},
                               {"source_service": "hbase", "target_service": "hdfs"}],
        "config_relations": [{"source_service": "hdfs", "target_service": "zookeeper",
                              "relation": "depends_on",
                              "properties": {"config_type": "core-site", "config_key": "ha.zookeeper.quorum"}}],
    }), encoding="utf-8")
    (config / "topology" / "manual-dependencies.yaml").write_text(
        "alpha:\n  - {source: impala, target: hdfs, purpose: 读写数据文件}\n", encoding="utf-8")
    sync_indexes(root, config)
    generated = (root / "alpha/hdfs/_index.md").read_text(encoding="utf-8").split(START, 1)[1]
    assert "| NAMENODE | 1 | node1 | 8020 rpc / 50070 http |" in generated
    assert "| 上游 | [[alpha/zookeeper/_index\\|ZooKeeper]] | 依赖 | 栈声明、配置 core-site/ha.zookeeper.quorum |" in generated
    assert "| 下游 | HBase | 依赖 | 栈声明 |" in generated            # 无概览页的服务不加链接
    assert "生成（采集于 2026-10-12）：Demo 栈声明、配置推断与人工登记。" in generated  # 来源系统名取快照
    assert "| 下游 | Impala | 依赖 | 人工登记：读写数据文件 |" in generated
    assert generated.index("## 组件分布") < generated.index("## 服务依赖") < generated.index("## 自动文档目录")


def test_sync_is_byte_idempotent(tmp_path):
    root = _fixture(tmp_path)
    sync_indexes(root)
    snapshot = {path: path.read_bytes() for path in root.rglob("_index.md")}
    assert sync_indexes(root) == ()
    assert snapshot == {path: path.read_bytes() for path in root.rglob("_index.md")}


def test_sync_restores_every_index_if_one_write_fails(tmp_path, monkeypatch):
    from lib.kb import index_sync

    root = _fixture(tmp_path)
    before = {path: path.read_bytes() for path in root.rglob("_index.md")}
    actual = index_sync.atomic_write_text
    calls = 0
    failed = False

    def flaky(path, text):
        nonlocal calls, failed
        calls += 1
        if calls == 2 and not failed:
            failed = True
            raise OSError("disk full")
        actual(path, text)

    monkeypatch.setattr(index_sync, "atomic_write_text", flaky)
    with pytest.raises(OSError, match="disk full"):
        sync_indexes(root)
    after = {path: path.read_bytes() for path in root.rglob("_index.md")}
    assert after == before
    assert not (root / "beta/emqx/_index.md").exists()
    assert not (root / "alpha/hdfs/namenode/_index.md").exists()
