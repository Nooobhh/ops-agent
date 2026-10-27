import pytest

from lib.inventory import (
    KINDS, InvalidResourceRef, Inventory, InventorySourceError, Relation, Resource,
    UnknownCluster, env_ref, parse_ref, register_kind,
)


def _inventory(source_errors=None):
    env = env_ref("lab")
    alpha = env.with_cluster("alpha")
    beta = env.with_cluster("beta")
    a1 = alpha.child("host", "node1")
    a2 = alpha.child("host", "node2")
    b1 = beta.child("host", "node1")
    hdfs = alpha.child("service", "hdfs")
    nn = hdfs.child("component", "NAMENODE")
    inst = a1.child("instance", "NAMENODE")
    resources = [
        Resource(env),
        Resource(alpha, {"legacy_key": "a", "roles": ("managed", "knowledge")}),
        Resource(beta, {"legacy_key": "b", "roles": ("direct",)}),
        Resource(a1, {"key": "node1", "address": "198.51.100.101", "aliases": ("node1.alpha.lan",)}),
        Resource(a2, {"key": "node2", "address": "198.51.100.102"}),
        Resource(b1, {"key": "node1", "address": "203.0.113.1"}),
        Resource(hdfs),
        Resource(nn),
        Resource(inst),
    ]
    relations = [Relation(str(inst), "instance_of", str(nn))]
    return Inventory(resources, relations, source_errors=source_errors)


def test_ref_round_trip_and_normalization():
    ref = parse_ref("lab/alpha/host/Node1/instance/NAMENODE")
    assert str(ref) == "lab/alpha/host/node1/instance/NAMENODE"
    assert ref.kind == "instance" and ref.name == "NAMENODE"
    assert str(ref.parent()) == "lab/alpha/host/node1"
    assert str(ref.cluster_ref()) == "lab/alpha"
    assert parse_ref("lab").kind == "env"
    assert parse_ref("lab/alpha").kind == "cluster"


@pytest.mark.parametrize("text", [
    "", "Lab", "lab/", "lab//host", "lab/alpha/host", "lab/alpha/disk/x",
    "lab/alpha/component/NN", "lab/alpha/host/node 1", "lab/alpha/host/-x",
])
def test_invalid_refs_rejected(text):
    with pytest.raises(InvalidResourceRef):
        parse_ref(text)


def test_kind_registry_is_extensible_and_conflict_safe():
    register_kind("namespace", ("cluster",), case="lower")
    register_kind("workload", ("namespace",))
    register_kind("pod", ("workload",))
    ref = parse_ref("k8s-lab/c1/namespace/Default/workload/api/pod/api-7d9f")
    assert str(ref) == "k8s-lab/c1/namespace/default/workload/api/pod/api-7d9f"
    assert register_kind("pod", ("workload",)) == KINDS["pod"]
    with pytest.raises(ValueError):
        register_kind("pod", ("cluster",))
    with pytest.raises(ValueError):
        register_kind("cluster", ("env",))


def test_cluster_keys_follow_declaration_order_and_roles():
    inv = _inventory()
    assert inv.cluster_keys() == ("a", "b")
    assert inv.cluster_keys("knowledge") == ("a",)
    assert inv.cluster_keys("missing") == ()
    assert inv.cluster("b").id == "lab/beta"
    with pytest.raises(UnknownCluster):
        inv.hosts("zzz")


def test_resolve_host_within_cluster():
    inv = _inventory()
    assert inv.resolve_host("node1", "a").id == "lab/alpha/host/node1"
    assert inv.resolve_host("198.51.100.102:9100", "a").id == "lab/alpha/host/node2"
    assert inv.resolve_host("NODE1.alpha.lan", "a").id == "lab/alpha/host/node1"
    assert inv.resolve_host("node2.other.domain", "a").id == "lab/alpha/host/node2"
    assert inv.resolve_host("10.0.0", "a") is None
    assert inv.resolve_host("", "a") is None
    assert inv.resolve_host("node1", "unknown") is None


def test_resolve_host_without_cluster_requires_unique_match():
    inv = _inventory()
    assert inv.resolve_host("node1") is None          # 两个集群都叫 node1
    assert inv.resolve_host("203.0.113.1").id == "lab/beta/host/node1"
    assert inv.resolve_host("node2").id == "lab/alpha/host/node2"


def test_source_error_fails_explicitly():
    inv = _inventory(source_errors={"lab/beta": "missing file"})
    assert inv.resolve_host("node2", "a").id == "lab/alpha/host/node2"
    with pytest.raises(InventorySourceError):
        inv.hosts("b")
    with pytest.raises(InventorySourceError):
        inv.resolve_host("node2")


def test_legacy_bridge_round_trip():
    inv = _inventory()
    assert inv.host_ref("a", "node1") == "lab/alpha/host/node1"
    assert inv.host_ref("b", "node1") == "lab/beta/host/node1"
    assert inv.host_ref("a", "nodeX") is None
    assert inv.host_ref(None, "node1") is None
    assert inv.cluster_ref("b") == "lab/beta"
    assert inv.legacy("lab/beta/host/node1") == ("b", "node1")
    assert inv.legacy("lab/alpha/host/node1/instance/NAMENODE") == ("a", "node1")
    assert inv.legacy("lab/alpha/service/hdfs") == ("a", None)
    assert inv.legacy("lab") == (None, None)


def test_relations_and_instances():
    inv = _inventory()
    nn = "lab/alpha/service/hdfs/component/NAMENODE"
    assert [item.id for item in inv.instances(component=nn)] == [
        "lab/alpha/host/node1/instance/NAMENODE",
    ]
    assert [item.id for item in inv.instances(host="lab/alpha/host/node2")] == []
    assert inv.relations(nn, "instance_of", "in")[0].source.endswith("/instance/NAMENODE")


def test_inventory_rejects_structural_errors():
    env = env_ref("lab")
    with pytest.raises(ValueError, match="父资源不存在"):
        Inventory([Resource(env), Resource(env.with_cluster("a").child("host", "n1"))])
    with pytest.raises(ValueError, match="重复"):
        Inventory([Resource(env), Resource(env)])
    with pytest.raises(ValueError, match="环境"):
        Inventory([])


def test_export_shape():
    data = _inventory().to_dict()
    assert data["schema_version"] == 1 and data["env"] == "lab"
    assert {item["kind"] for item in data["resources"]} >= {"env", "cluster", "host", "instance"}
    assert data["relations"][0]["type"] == "instance_of"
