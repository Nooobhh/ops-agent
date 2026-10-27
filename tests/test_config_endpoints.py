from pathlib import Path

from lib.config import load_platform


def test_old_platform_yaml_without_new_keys_still_loads(tmp_path: Path):
    path = tmp_path / "platform-old.yaml"
    path.write_text(
        "platform: alpha\n"
        "nodes:\n"
        "  node1: {host: 198.51.100.101, components: [NAMENODE]}\n",
        encoding="utf-8",
    )

    cfg = load_platform(path)

    assert cfg.collected_at is None
    assert cfg.endpoints == {}
    assert cfg.nodes["node1"].components == ["NAMENODE"]


def test_new_platform_yaml_parses_endpoints_and_collected_at(tmp_path: Path):
    path = tmp_path / "platform-new.yaml"
    path.write_text(
        "platform: alpha\n"
        "collected_at: '2026-07-13T10:00:00Z'\n"
        "endpoints:\n"
        "  prometheus: {url: 'http://198.51.100.101:9090', source: manual}\n"
        "  hive_metastore_db: {engine: mysql, host: node1, port: 3306, db: hive, source: 'manager:hive-site/javax.jdo.option.ConnectionURL', collected_at: '2026-07-13T10:00:00Z'}\n"
        "nodes:\n"
        "  node1: {host: 198.51.100.101, components: [HIVE_METASTORE]}\n",
        encoding="utf-8",
    )

    cfg = load_platform(path)

    assert cfg.collected_at == "2026-07-13T10:00:00Z"
    assert cfg.endpoints["prometheus"]["source"] == "manual"
    assert cfg.endpoints["hive_metastore_db"]["port"] == 3306


def test_skeleton_with_empty_nodes_loads(tmp_path):
    # 采集前的骨架形态(nodes: {})必须可加载——真实 platform-beta.yaml 已被采集填充,这里用临时骨架锁行为
    p = tmp_path / "platform-beta.yaml"
    p.write_text(
        "platform: beta\ncollected_at: null\n"
        "endpoints:\n  prometheus: {url: 'http://192.0.2.53:9091', source: manual}\n"
        "nodes: {}\n",
        encoding="utf-8",
    )
    cfg = load_platform(p)

    assert cfg.platform == "beta"
    assert cfg.nodes == {}
    assert cfg.endpoints["prometheus"]["source"] == "manual"


def test_real_platform_iot_loads_after_collection():
    cfg = load_platform(Path("envs/example/config/platform-beta.yaml"))

    assert cfg.platform == "beta"
    assert cfg.endpoints["prometheus"]["url"] == "http://127.0.0.1:9090"
