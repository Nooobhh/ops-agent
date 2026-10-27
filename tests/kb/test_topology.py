from __future__ import annotations

import json
from pathlib import Path

import yaml

from lib.kb.topology import build_topology, write_topology
from lib.kb.topology_view import render_topology_html


def _write_doc(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    config = tmp_path / "config"
    knowledge = tmp_path / "knowledge"
    config.mkdir()
    (config / "topology").mkdir()
    (config / "topology" / "manual-dependencies.yaml").write_text(
        "alpha:\n"
        "  - {source: hdfs, target: zookeeper, purpose: HA 选举}\n"
        "  - {source: yarn, target: hdfs, purpose: 任务数据}\n",
        encoding="utf-8",
    )
    (config / "platform-alpha.yaml").write_text(
        yaml.safe_dump(
            {
                "platform": "alpha",
                "cluster_name": "test-hd",
                "collected_at": "2026-08-31T00:00:00Z",
                "endpoints": {
                    "manager": {
                        "url": "http://node1.example.internal:8080",
                        "source": "manual",
                    },
                    "rm_web": {
                        "url": "http://198.51.100.102:8088",
                        "source": "manager",
                    },
                },
                "nodes": {
                    "node1": {
                        "host": "198.51.100.101",
                        "components": ["NAMENODE", "ZOOKEEPER_SERVER"],
                    },
                    "node2": {
                        "host": "198.51.100.102",
                        "components": ["RESOURCEMANAGER", "UNKNOWN_COMPONENT"],
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (config / "platform-beta.yaml").write_text(
        "platform: beta\ncluster_name: test-iot\nnodes: {}\nendpoints: {}\n",
        encoding="utf-8",
    )
    _write_doc(
        knowledge,
        "alpha/hdfs/_index.md",
        """---
title: _index
platform: alpha
service: hdfs
---
# HDFS
""",
    )
    _write_doc(
        knowledge,
        "alpha/hdfs/alpha-hdfs-health.md",
        """---
title: HDFS 健康排查
type: troubleshoot
platform: alpha
service: hdfs
problem: hdfs-health
symptoms: [DataNode 心跳超时]
alerts: [datanode_health]
playbook: playbooks/alpha/hdfs/check.sh
related: ["[[alpha/hdfs/_index]]"]
status: draft
---
# HDFS 健康排查

## 摘要

检查 HDFS 健康。
""",
    )
    _write_doc(
        knowledge,
        "alpha/hdfs/alpha-hdfs-baseline.md",
        """---
title: HDFS 服务基线
type: knowledge
platform: alpha
service: hdfs
problem: baseline
symptoms: []
alerts: []
playbook:
status: draft
---
# HDFS 服务基线

## 组件分布

| 组件 | 节点数 | 节点 |
|---|---:|---|
| NAMENODE | 1 | node1.example.internal |

## 关键配置

| 配置文件 | 键 | 值 |
|---|---|---|
| hdfs-site | dfs.replication | `` 3 `` |

## 实测状态

| 节点 | 组件 | 进程 | 端口 |
|---|---|---|---|
| node1.example.internal | NAMENODE | ✓ | 8020:✓ |
""",
    )
    return config, knowledge


def _edges(graph: dict, relation: str) -> set[tuple[str, str]]:
    return {
        (edge["source"], edge["target"])
        for edge in graph["edges"]
        if edge["relation"] == relation
    }


def test_build_topology_compiles_nodes_components_endpoints_and_dependencies(tmp_path: Path):
    config, knowledge = _fixture(tmp_path)

    graph = build_topology(config, knowledge)
    node_ids = {node["id"] for node in graph["nodes"]}

    assert "platform:alpha" in node_ids
    assert "cluster:alpha:test-hd" in node_ids
    assert "node:alpha:node1" in node_ids
    assert "component:alpha:NAMENODE" in node_ids
    assert "service:alpha:hdfs" in node_ids
    assert "service:alpha:yarn" in node_ids
    assert "endpoint:alpha:manager" in node_ids
    assert "alert:alpha:datanode_health" in node_ids
    assert "document:alpha/hdfs/alpha-hdfs-health.md" in node_ids
    namenode = next(
        node for node in graph["nodes"]
        if node["id"] == "component:alpha:NAMENODE"
    )
    assert namenode["properties"]["baseline_node_count"] == 1
    assert namenode["properties"]["observed_ports"] == ["8020"]
    hdfs = next(node for node in graph["nodes"] if node["id"] == "service:alpha:hdfs")
    assert hdfs["properties"]["baseline_configs"][0]["key"] == "dfs.replication"
    assert graph["coverage"]["service_baseline"]["with_baseline"] == 1
    assert graph["coverage"]["component_observation"]["observed_in_platform_config"] == 4
    assert any(
        item["kind"] == "platform_config"
        for edge in graph["edges"]
        if edge["relation"] == "hosts_component"
        and edge["source"] == "node:alpha:node1"
        and edge["target"] == "component:alpha:NAMENODE"
        for item in edge["provenance"]
    )

    contains = _edges(graph, "contains_node")
    assert ("cluster:alpha:test-hd", "node:alpha:node1") in contains
    hosts = _edges(graph, "hosts_component")
    assert ("node:alpha:node1", "component:alpha:NAMENODE") in hosts
    component_of = _edges(graph, "component_of")
    assert ("component:alpha:NAMENODE", "service:alpha:hdfs") in component_of
    served_by = _edges(graph, "served_by")
    assert ("endpoint:alpha:manager", "node:alpha:node1") in served_by
    dependencies = _edges(graph, "depends_on")
    assert ("service:alpha:hdfs", "service:alpha:zookeeper") in dependencies
    assert ("service:alpha:yarn", "service:alpha:hdfs") in dependencies

    assert any("UNKNOWN_COMPONENT" in warning for warning in graph["warnings"])
    assert graph["stats"]["node_kinds"]["node"] == 2
    assert graph["stats"]["edge_relations"]["hosts_component"] == 4


def test_topology_output_is_deterministic_and_atomic(tmp_path: Path):
    config, knowledge = _fixture(tmp_path)
    first = build_topology(config, knowledge)
    second = build_topology(config, knowledge)
    assert first == second

    output = tmp_path / "build" / "kb-topology.json"
    write_topology(first, output)
    assert json.loads(output.read_text(encoding="utf-8")) == first
    assert [path.name for path in output.parent.iterdir()] == [output.name]
    html = render_topology_html(first)
    assert "服务依赖图" in html
    assert "topology-data" in html


def test_missing_platform_config_is_reported_but_kb_documents_remain(tmp_path: Path):
    config, knowledge = _fixture(tmp_path)
    (config / "platform-alpha.yaml").unlink()

    graph = build_topology(config, knowledge)

    assert "document:alpha/hdfs/alpha-hdfs-health.md" in {
        node["id"] for node in graph["nodes"]
    }
    assert any("platform-alpha.yaml" in warning for warning in graph["warnings"])


def test_topology_snapshot_merges_config_evidence_into_topology(tmp_path: Path):
    config, knowledge = _fixture(tmp_path)
    snapshot_root = tmp_path / "state" / "topology"
    snapshot_root.mkdir(parents=True)
    (snapshot_root / "snapshot-alpha.json").write_text(
        json.dumps({
            "schema_version": 3,
            "platform": "alpha",
            "cluster_name": "test-hd",
            "collected_at": "2026-09-01T00:00:00Z",
            "nodes": {},
            "services": [{"native_service": "HUE", "service": "hue"}],
            "config_relations": [{
                "source_service": "hue",
                "target_service": "hdfs",
                "relation": "uses_service",
                "properties": {"confidence": "high", "config_key": "webhdfs_url"},
                "provenance": [{
                    "kind": "snapshot_config",
                    "path": "mgmt://alpha/test-hd/config/hue-hadoop-site/webhdfs_url",
                    "collected_at": "2026-09-01T00:00:00Z",
                }],
            }],
            "warnings": [],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    graph = build_topology(config, knowledge, snapshot_root)

    relation = next(edge for edge in graph["edges"] if edge["relation"] == "uses_service")
    assert (relation["source"], relation["target"]) == (
        "service:alpha:hue", "service:alpha:hdfs",
    )
    assert any(item["kind"] == "snapshot_config" for item in relation["provenance"])
    assert graph["coverage"]["service_dependencies"]["config_relation_counts"]["uses_service"] == 1


def test_v3_snapshot_adds_stack_and_component_dependencies_and_ports(tmp_path: Path):
    config, knowledge = _fixture(tmp_path)
    snapshot_root = config / "topology"
    stack_source = {"kind": "snapshot_stack", "path": "mgmt://alpha/stack/DEMO/services/YARN/required_services"}
    (snapshot_root / "snapshot-alpha.json").write_text(
        json.dumps({
            "schema_version": 3,
            "platform": "alpha",
            "cluster_name": "test-hd",
            "collected_at": "2026-10-12T00:00:00Z",
            "nodes": {"node1": {"host": "198.51.100.101", "components": ["NAMENODE", "ZOOKEEPER_SERVER"]}},
            "services": [],
            "config_relations": [],
            "stack_dependencies": [{
                "source_service": "yarn", "target_service": "hdfs", "relation": "depends_on",
                "properties": {"confidence": "high"}, "provenance": [stack_source],
            }],
            "component_dependencies": [{
                "source_component": "NAMENODE", "target_component": "ZOOKEEPER_SERVER",
                "relation": "depends_on", "properties": {"scope": "cluster"}, "provenance": [stack_source],
            }],
            "component_ports": [{"component": "NAMENODE", "port": 8020, "purpose": "rpc",
                                 "provenance": [{"kind": "snapshot_config", "path": "x"}]}],
            "warnings": [],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    graph = build_topology(config, knowledge, snapshot_root)

    edges = {(edge["source"], edge["target"]): edge for edge in graph["edges"] if edge["relation"] == "depends_on"}
    merged = edges[("service:alpha:yarn", "service:alpha:hdfs")]
    assert {item["kind"] for item in merged["provenance"]} == {"snapshot_stack", "manual"}
    component = edges[("component:alpha:NAMENODE", "component:alpha:ZOOKEEPER_SERVER")]
    assert component["properties"]["scope"] == "cluster"
    namenode = next(node for node in graph["nodes"] if node["id"] == "component:alpha:NAMENODE")
    assert namenode["properties"]["ports"][0]["port"] == 8020
    coverage = graph["coverage"]["service_dependencies"]
    assert coverage["stack_declared_edge_count"] == 1
    assert coverage["manual_declared_edge_count"] == 2
    assert coverage["component_dependency_edge_count"] == 1
    assert coverage["components_with_ports"] == 1


def test_explicit_v2_document_relation_is_compiled_without_inference(tmp_path: Path):
    config, knowledge = _fixture(tmp_path)
    _write_doc(
        knowledge,
        "alpha/hue/alpha-hue-routing.md",
        """---
schema_version: 2
source: manual_import
source_ref: relation.md
source_sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
title: Hue 路由关系
type: knowledge
platform: alpha
service: hue
problem: routing
summary: Hue 到 HDFS 的明确路由关系。
knowledge_layer: basic_info
entrypoints: [basic_info, manual_task]
profile: topology
execution_scope: agent_readonly
confidence: observed
related: []
status: draft
relations:
  - target: hdfs
    relation: routes_to
    properties:
      purpose: WebHDFS
---
# Hue 路由关系

## 摘要

Hue 到 HDFS 的明确路由关系。

## 适用范围

拓扑关系。

## 实体与关系

证据。

## 边界与时效

只使用明确声明的关系。

## 参考

relation.md
""",
    )

    graph = build_topology(config, knowledge)

    assert ("service:alpha:hue", "service:alpha:hdfs") in _edges(graph, "routes_to")
