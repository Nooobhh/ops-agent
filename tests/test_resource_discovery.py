"""资源采集经 Prometheus 插件(MCP):快照 / 采集目标 / 具名查询 → 中台资源模型。"""
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from lib.acquisition import resources
from lib.acquisition.resources import ResourceSourceError, collect_resource_snapshots
from lib.config import NodeInfo, PlatformConfig
from lib.plugins import PluginCallError

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 22, 14, 47, 30, tzinfo=timezone.utc)  # CST 22:47:30
AT = {"server": "2026-10-22 22:47:30", "real": "2026-10-22 22:47:30"}


def _host(ip, platform, **fields):
    return {"node": ip, "platform": platform, "collector_up": 1, **fields}


HOSTS = [
    _host("192.0.2.11", "alpha", cpu_used_ratio=0.25, cpu_cores=64, load1=1.5, memory_total_bytes=100,
          memory_available_bytes=40, swap_used_bytes=5,
          filesystems=[
              {"mountpoint": "/", "device": "/dev/sda1", "fstype": "xfs", "avail_bytes": 30, "size_bytes": 100},
              {"mountpoint": "/run", "device": "tmpfs", "fstype": "tmpfs", "avail_bytes": 1, "size_bytes": 2},
              {"mountpoint": "/mfs", "device": "mfs#1", "fstype": "fuse.mfs", "avail_bytes": 5, "size_bytes": 10},
          ],
          jvms=[{"system": "hdfs", "component": "namenode", "instance": "192.0.2.11:50070",
                 "heap_used_bytes": 60, "heap_max_bytes": 100, "gc_time_ratio": 0.02},
                {"system": "mfs-gp-hbase", "component": "server", "instance": "192.0.2.11:1",
                 "heap_used_bytes": 1, "heap_max_bytes": 4}],
          processes=[{"groupname": "hdfs/namenode", "rss_bytes": 70, "cpu_cores": 0.1}]),
    _host("192.0.2.12", "alpha", collector_up=0),
    _host("192.0.2.13", "alpha", cpu_used_ratio=0.1, filesystems=[
        {"mountpoint": "/mfs", "device": "mfs#1", "fstype": "fuse.mfs", "avail_bytes": 5, "size_bytes": 10},
    ]),
    _host("198.51.100.3", "mysql", cpu_used_ratio=0.5),
    _host("192.0.2.82", "infra", cpu_used_ratio=0.9),
]


def _target(ip, platform, system, component, job, health="up"):
    return {"labels": {"node": ip, "platform": platform, "system": system, "component": component,
                       "job": job, "instance": f"{ip}:1"}, "health": health}


TARGETS = {
    "alpha": [
        _target("192.0.2.11", "alpha", "hdfs", "namenode", "jmx_namenode"),
        _target("192.0.2.11", "alpha", "hdfs", "namenode", "blackbox_tcp_port_check_hd"),
        _target("192.0.2.12", "alpha", "yarn", "resourcemanager", "jmx_resourcemanager", "down"),
        _target("192.0.2.12", "alpha", "hive", "metastore", "blackbox_tcp_port_check_hd"),
    ],
    "mysql": [
        _target("198.51.100.3", "mysql", "mysql", "mysqld", "mysql_exporter2"),
        _target("198.51.100.4", "mysql", "mysql", "mysqld", "mysql_exporter2", "down"),
    ],
}


class Feed:
    def __init__(self, *, fail=None, missing_queries=(), truncated=False):
        self.calls = []
        self.fail = fail
        self.missing = set(missing_queries)
        self.truncated = truncated

    def __call__(self, role, arguments):
        self.calls.append((role, arguments))
        if role == self.fail:
            raise PluginCallError("UPSTREAM_UNAVAILABLE", "连不上(数据源 victoriametrics)")
        if role == "snapshot":
            return {"data": {"at": AT, "hosts": HOSTS}, "warnings": []}
        if role == "targets":
            platform = arguments["filters"]["platform"]
            return {"data": {"targets": TARGETS.get(platform, []), "truncated": self.truncated}, "warnings": []}
        if arguments["name"] in self.missing:
            raise PluginCallError("NOT_FOUND", f"没有具名查询 {arguments['name']}")
        return {"data": {"at": AT, "series": [
            {"labels": {"node": "198.51.100.3", "job": "mysql_exporter2", "platform": "mysql",
                        "master_host": "198.51.100.6"}, "value": 38},
        ]}, "warnings": []}


@pytest.fixture
def collect(monkeypatch, use_env_timezone):
    use_env_timezone("Etc/GMT-8")  # 插件时间按环境时区(此处 UTC+8)传
    monkeypatch.setattr(resources, "_load_platforms", lambda _root: {"alpha": PlatformConfig(platform="alpha", nodes={
        "node1": NodeInfo(host="192.0.2.11", components=["NAMENODE", "HIVE_METASTORE", "PIG"]),
        "node2": NodeInfo(host="192.0.2.12", components=["RESOURCEMANAGER", "HIVE_METASTORE"]),
        "node3": NodeInfo(host="192.0.2.13", components=["FLUME_HANDLER"]),
        "node4": NodeInfo(host="192.0.2.14"),
    })})

    def run(feed, **kwargs):
        return collect_resource_snapshots(REPO, now=NOW, feed_factory=lambda _root: (feed, "mcp:prometheus", "http://mcp"),
                                          **kwargs)
    return run


def test_snapshot_maps_platforms_nodes_and_statuses(collect):
    feed = Feed()
    snapshots, _ = collect(feed)
    by_platform = {row["platform"]: row for row in snapshots}

    assert set(by_platform) == {"alpha", "mysql"}, "infra 在排除名单;平台名取 platform 标签值"
    assert feed.calls[0] == ("snapshot", {"at": "2026-10-22 22:47:30"})
    assert [arguments["filters"] for role, arguments in feed.calls if role == "targets"] == [
        {"platform": "alpha"}, {"platform": "mysql"},
    ]
    nodes = {row["node"]: row for row in by_platform["alpha"]["nodes"]}
    assert {name: row["status"] for name, row in nodes.items()} == {
        "node1": "ok", "node2": "target_down", "node3": "ok", "node4": "target_missing",
    }
    node1 = nodes["node1"]
    assert node1["metrics"]["cpu_used_pct"] == 25.0 and node1["metrics"]["memory_used_pct"] == 60.0
    assert node1["metrics"]["swap_used_pct"] is None, "插件没给 swap 总量:缺失而不是 0"
    assert "major_page_faults_total" not in node1["metrics"]
    assert nodes["node2"]["metrics"] == {} and nodes["node2"]["filesystems"] == []
    assert by_platform["mysql"]["nodes"][0]["node"] == "198.51.100.3", "未登记主机以 IP 作节点名"
    assert by_platform["alpha"]["source"] == "mcp:prometheus" and by_platform["alpha"]["source_url"] == "http://mcp"
    assert by_platform["alpha"]["status"] == "partial" and by_platform["alpha"]["coverage"]["observed_nodes"] == 2


def test_filesystems_keep_old_avail_based_usage_and_dedupe_shared(collect):
    snapshots, _ = collect(Feed())
    nodes = {row["node"]: row for row in next(row for row in snapshots if row["platform"] == "alpha")["nodes"]}

    fs = {item["mountpoint"]: item for item in nodes["node1"]["filesystems"]}
    assert set(fs) == {"/", "/mfs"}, "tmpfs 不在登记类型里"
    assert fs["/"]["used_pct"] == 70.0 and fs["/"]["kind"] == "local"
    assert fs["/"]["inode_used_pct"] is None and "readonly" not in fs["/"]
    assert fs["/mfs"]["kind"] == "shared"
    assert nodes["node3"]["filesystems"] == [], "共享盘同平台只记一次"


def test_jvm_rows_and_catalog_use_label_mappings(collect):
    snapshots, catalog = collect(Feed())
    alpha = next(row for row in snapshots if row["platform"] == "alpha")
    services = {row["job"]: row for row in alpha["services"]}

    namenode = services["hdfs/namenode"]
    assert (namenode["service"], namenode["component"], namenode["status"]) == ("hdfs", "NAMENODE", "ok")
    assert namenode["metrics"]["heap_used_pct"] == 60.0 and namenode["metrics"]["gc_time_pct"] == 2.0
    assert namenode["metrics"]["process_resident_bytes"] == 70
    assert namenode["old_gen_after_gc_bytes"] == {}, "插件未给老年代 GC 后占用"
    assert services["mfs-gp-hbase/server"]["component"] == "MFS_GP_HBASE_SERVER"

    status = {(row["node"], row["component"]): row["status"] for row in catalog["services"]}
    assert status[("node1", "NAMENODE")] == "ok"
    assert status[("node2", "RESOURCEMANAGER")] == "target_down"
    assert status[("node2", "HIVE_METASTORE")] == "unsupported", "只有端口探测不算在采"
    assert {row["component"] for row in catalog["unmonitored_components"]} == {"FLUME_HANDLER"}
    assert {(item["node"], item["component"]) for item in alpha["missing"]} >= {
        ("node2", "RESOURCEMANAGER"), ("node1", "HIVE_METASTORE"),
    }


def test_business_metrics_missing_named_queries_are_explicit(collect):
    config = yaml.safe_load((REPO / "envs" / "example" / "config" / "resource-metrics.yaml").read_text(encoding="utf-8"))
    missing = [cfg["query"] for cfg in config["business_metrics"]["mysql"]["metrics"].values()
               if cfg["query"] != "mysql_replication_lag_seconds"]
    snapshots, _ = collect(Feed(missing_queries=missing))
    rows = next(row for row in snapshots if row["platform"] == "mysql")["business_metrics"]

    lag = [row for row in rows if row["metric"] == "replication_lag_seconds"]
    assert lag == [{
        "platform": "mysql", "node": "198.51.100.3", "service": "MySQL", "job": "mysql_exporter2",
        "metric": "replication_lag_seconds", "label": "主从延迟", "unit": "seconds", "value": 38.0,
        "status": "ok", "severity": "critical", "sample_at": "2026-10-22T14:47:30+00:00",
        "details": {"master_host": "198.51.100.6"},
    }]
    usage = {(row["node"], row["status"]) for row in rows if row["metric"] == "connection_usage_pct"}
    assert usage == {("198.51.100.3", "source_error")}, "插件没有该具名查询:显式 source_error,不填 0"
    assert all(row["value"] is None for row in rows if row["status"] != "ok")


@pytest.mark.parametrize("fail", ["snapshot", "targets"])
def test_plugin_failure_fails_the_whole_collection(collect, fail):
    with pytest.raises(ResourceSourceError, match="UPSTREAM_UNAVAILABLE"):
        collect(Feed(fail=fail))


def test_truncated_targets_fail_instead_of_misreporting(collect):
    with pytest.raises(ResourceSourceError, match="截断"):
        collect(Feed(truncated=True))
