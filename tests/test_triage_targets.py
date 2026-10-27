from pathlib import Path

from lib.triage_agent.targets import infer_target_for_node, resolve_node


ROOT = Path(__file__).resolve().parents[1]


def test_ip_is_canonicalised_to_hd_node_key():
    assert resolve_node(ROOT, "alpha", "192.0.2.13") == "node3"


def test_mysql_topology_and_shared_vm_are_registered_targets():
    assert resolve_node(ROOT, "db", "203.0.113.12") == "db2"
    assert resolve_node(ROOT, "shared", "203.0.113.21") == "vm-app01"
    assert infer_target_for_node(ROOT, "203.0.113.21") == "shared"


def test_exporter_instance_port_is_not_part_of_the_host_identity():
    assert resolve_node(ROOT, "db", "203.0.113.12:9104") == "db2"
    assert infer_target_for_node(ROOT, "203.0.113.12:9104") == "db"
    assert resolve_node(ROOT, "alpha", "node3:8042") == "node3"
