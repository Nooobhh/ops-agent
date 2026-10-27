import inspect
import os
from pathlib import Path

import pytest

import lib.envpack as envpack
from lib.config import load_platform
from lib.envpack import EnvPackError, load_env_pack, parse_env_pack
from lib.inventory import InventorySourceError, load_inventory
from lib.inventory.sources.legacy import to_platform_config
from lib.triage_agent import targets

REPO = Path(__file__).resolve().parents[2]
REAL_PACK = REPO / "envs" / "example"
REAL_FILES = {
    "alpha": "config/platform-alpha.yaml",
    "beta": "config/platform-beta.yaml",
    "db": "config/topology-db.yaml",
    "shared": "config/topology-misc.yaml",
    "cache": "config/topology-cache.yaml",
    "edge": "config/topology-edge.yaml",
}


def _write_pack(root: Path, body: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "env.yaml").write_text(body, encoding="utf-8")
    return root


def _point_to(repo_root: Path, pack: Path) -> None:
    (repo_root / "config").mkdir(parents=True, exist_ok=True)
    (repo_root / "config" / "paths.local.yaml").write_text(f"env_pack: {pack}\n", encoding="utf-8")


PACK = """\
version: 1
id: lab
display_name: Lab
timezone: UTC
clusters:
  - id: alpha
    legacy_key: a
    roles: [managed, knowledge]
    sources: {hosts: "config/alpha.yaml"}
  - id: beta
    legacy_key: b
    roles: [mysql_direct]
    sources: {hosts: "hosts/beta.yaml"}
"""


@pytest.fixture(autouse=True)
def _no_data_root(monkeypatch):
    monkeypatch.delenv("OPS_AGENT_DATA_ROOT", raising=False)


def test_real_pack_roles_reproduce_legacy_platform_sets():
    pack = load_env_pack(REPO)
    assert pack.id == "example" and pack.timezone == "UTC"
    assert pack.cluster_keys() == ("alpha", "beta", "db", "shared", "cache", "edge")
    assert pack.cluster_keys("managed") == ("alpha", "beta")
    assert pack.cluster_keys("mysql_direct") == ("db", "shared")
    assert pack.knowledge_platforms() == ("alpha", "beta", "db", "misc", "cache", "edge", "shared")
    assert pack.knowledge_key("shared") == "misc" and pack.knowledge_key("alpha") == "alpha"
    assert pack.knowledge_cluster("misc").legacy_key == "shared"
    assert pack.knowledge_cluster("shared") is None


def test_real_inventory_projects_identical_platform_configs():
    inventory = load_inventory(REPO)
    assert not inventory.source_errors
    for key, relative in REAL_FILES.items():
        assert to_platform_config(inventory, key) == load_platform(REAL_PACK / relative)
    counts = {key: len(inventory.hosts(key)) for key in inventory.cluster_keys()}
    assert counts == {"alpha": 3, "beta": 3, "db": 2, "shared": 2, "cache": 3, "edge": 1}
    assert inventory.cluster("shared").id == "example/misc"


def test_real_inventory_relations():
    inventory = load_inventory(REPO)
    namenode = "example/alpha/service/hdfs/component/NAMENODE"
    assert [item.id for item in inventory.instances(component=namenode)] == [
        "example/alpha/host/node1/instance/NAMENODE",
        "example/alpha/host/node2/instance/NAMENODE",
    ]
    manual = [
        item for item in inventory.relations("example/alpha/service/spark", "depends_on")
        if item.target == "example/alpha/service/hive"
    ]
    assert manual and "manual" in manual[0].attrs["basis"]
    mysql_instances = inventory.instances(host="example/db/host/db1")
    assert {item.name for item in mysql_instances} == {"MYSQL_SERVER"}


def _alias_forms(key: str, info) -> list:
    forms = [key, key.upper(), f"{key}.example.lan", info.host, f"{info.host}:9100", *info.aliases]
    return forms + [f"{alias}:3306" for alias in info.aliases]


def test_resolve_host_matches_legacy_resolver_for_every_registered_alias(monkeypatch):
    # 旧解析器每次调用都重读 yaml;按目标缓存只为提速,不改其匹配规则
    loaded = {key: targets.load_target(REPO, key) for key in REAL_FILES}
    monkeypatch.setattr(targets, "load_target", lambda _root, key: loaded[key])
    inventory = load_inventory(REPO)
    probes = {"", "node99", "192.0.2.250", "nfs-client", "node1.alpha.example.internal"}
    for key, relative in REAL_FILES.items():
        for name, info in load_platform(REAL_PACK / relative).nodes.items():
            probes.update(_alias_forms(name, info))
    for key in REAL_FILES:
        for probe in probes:
            host = inventory.resolve_host(probe, key)
            got = None if host is None else inventory.legacy(host.ref)[1]
            assert got == targets.resolve_node(REPO, key, probe), (key, probe)
    for probe in probes:
        host = inventory.resolve_host(probe)
        got = None if host is None else inventory.legacy(host.ref)[0]
        assert got == targets.infer_target_for_node(REPO, probe), probe


def test_pack_located_by_paths_local_and_sources_resolved(tmp_path):
    pack = _write_pack(tmp_path / "pack", PACK)
    (pack / "hosts").mkdir()
    (pack / "hosts" / "beta.yaml").write_text(
        "platform: b\nnodes:\n  db1: {host: 203.0.113.91, components: [MYSQL_SERVER]}\n", encoding="utf-8",
    )
    repo = tmp_path / "repo"
    _point_to(repo, pack)
    (pack / "config").mkdir()
    (pack / "config" / "alpha.yaml").write_text(
        "platform: a\ncluster_name: alpha-native\nnodes:\n"
        "  n1: {host: 203.0.113.81, aliases: [n1.alpha.lan], components: [NAMENODE]}\n",
        encoding="utf-8",
    )
    inventory = load_inventory(repo)
    assert inventory.env.id == "lab"
    assert inventory.cluster("a").attrs["native_name"] == "alpha-native"
    assert inventory.resolve_host("n1.alpha.lan").id == "lab/alpha/host/n1"
    assert inventory.resolve_host("203.0.113.91").id == "lab/beta/host/db1"
    assert load_env_pack(repo).knowledge_platforms() == ("a", "shared")


def test_missing_host_source_fails_only_that_cluster(tmp_path):
    pack = _write_pack(tmp_path / "pack", PACK)
    repo = tmp_path / "repo"
    _point_to(repo, pack)
    (pack / "config").mkdir()
    (pack / "config" / "alpha.yaml").write_text(
        "platform: a\nnodes:\n  n1: {host: 203.0.113.81}\n", encoding="utf-8",
    )
    inventory = load_inventory(repo)
    assert inventory.resolve_host("n1", "a").id == "lab/alpha/host/n1"
    assert "lab/beta" in inventory.source_errors
    with pytest.raises(InventorySourceError):
        inventory.hosts("b")


def test_inventory_cache_follows_source_changes(tmp_path):
    pack = _write_pack(tmp_path / "pack", PACK)
    repo = tmp_path / "repo"
    _point_to(repo, pack)
    (pack / "config").mkdir()
    alpha = pack / "config" / "alpha.yaml"
    alpha.write_text("platform: a\nnodes:\n  n1: {host: 203.0.113.81}\n", encoding="utf-8")
    first = load_inventory(repo)
    assert load_inventory(repo) is first
    alpha.write_text("platform: a\nnodes:\n  n1: {host: 203.0.113.81}\n  n2: {host: 203.0.113.82}\n",
                     encoding="utf-8")
    os.utime(alpha, ns=(1, 1))
    assert [item.name for item in load_inventory(repo).hosts("a")] == ["n1", "n2"]


@pytest.mark.parametrize("patch, message", [
    (("version: 1", "version: 2"), "version"),
    (("timezone: UTC", "timezone: Mars/Base"), "时区"),
    (("roles: [mysql_direct]", "roles: [writer]"), "未知角色"),
    (("legacy_key: b", "legacy_key: a"), "重复"),
    (("id: lab", "id: Lab"), "非法环境"),
    # 知识库键:knowledge 集群不能与环境级范围键重名,也不能彼此重复
    (("legacy_key: a\n", "legacy_key: shared\n"), "知识库键与环境级"),
    (("roles: [mysql_direct]", "roles: [knowledge]\n    knowledge_key: a"), "knowledge_key 重复"),
    (("timezone: UTC", "timezone: UTC\nknowledge: {services: [yarn, yarn]}"), "重复项"),
    (("timezone: UTC", "timezone: UTC\nknowledge: {task_types: SHELL}"), "字符串列表"),
    # 路径一律落在环境包内:已停用的 repo: 前缀、绝对路径与越界都拒
    (("hosts/beta.yaml", "repo:config/beta.yaml"), "相对环境包"),
    (("hosts/beta.yaml", "/etc/beta.yaml"), "相对环境包"),
    (("hosts/beta.yaml", "../beta.yaml"), "相对环境包"),
    (("timezone: UTC", "timezone: UTC\nextensions: {node_credentials: lab_ext:x}"), "python 目录"),
    (("timezone: UTC", "timezone: UTC\npython: py\nextensions: {bastion: lab_ext:x}"), "未知扩展点"),
    (("timezone: UTC", "timezone: UTC\nrequires_core: \">=999\""), "要求核心"),
    (("timezone: UTC", "timezone: UTC\nrequires_core: \"~1\""), "无法识别"),
])
def test_invalid_pack_rejected(tmp_path, patch, message):
    pack = _write_pack(tmp_path / "pack", PACK.replace(*patch))
    with pytest.raises(EnvPackError, match=message):
        parse_env_pack(pack)


def test_pack_location_order(tmp_path, monkeypatch):
    """env_pack(可相对数据根)→ <数据根>/env/ → 自带示例包;配了却不是环境包时显式失败,不往下退。"""
    monkeypatch.setattr(envpack, "resolve_env_pack_dir", inspect.unwrap(envpack.resolve_env_pack_dir))
    monkeypatch.delenv("OPS_AGENT_DATA_ROOT", raising=False)
    data = tmp_path / "data"
    data.mkdir()

    def located():
        root = envpack.resolve_env_pack_dir(data)
        return root, envpack.env_pack_origin(data, root)

    assert located() == (envpack.EXAMPLE_PACK_DIR.resolve(), "example")
    _write_pack(data / "env", PACK)
    assert located() == ((data / "env").resolve(), "data_root")
    _write_pack(tmp_path / "packs" / "lab", PACK)
    (data / "config").mkdir()
    (data / "config" / "paths.local.yaml").write_text("env_pack: ../packs/lab\n", encoding="utf-8")
    assert located() == ((tmp_path / "packs" / "lab").resolve(), "configured")
    assert load_env_pack(data).id == "lab"
    (data / "config" / "paths.local.yaml").write_text(f"env_pack: {tmp_path / 'nope'}\n", encoding="utf-8")
    with pytest.raises(EnvPackError, match="缺少"):
        load_env_pack(data)


def test_pack_commit_reads_git_head_without_git_binary(tmp_path):
    sha = "0123456789abcdef0123456789abcdef01234567"
    pack = _write_pack(tmp_path / "pack", PACK)
    assert envpack.env_pack_commit(pack) is None
    (pack / ".git" / "refs" / "heads").mkdir(parents=True)
    (pack / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (pack / ".git" / "packed-refs").write_text(f"# pack-refs\n{sha} refs/heads/main\n", encoding="utf-8")
    assert envpack.env_pack_commit(pack) == "0123456"
    (pack / ".git" / "refs" / "heads" / "main").write_text("f" * 40 + "\n", encoding="utf-8")
    assert envpack.env_pack_commit(pack) == "fffffff"
    (pack / ".git" / "HEAD").write_text(sha + "\n", encoding="utf-8")       # 分离头指针
    assert envpack.env_pack_commit(pack) == "0123456"


def test_knowledge_key_maps_legacy_key_and_vocabulary_is_optional(tmp_path):
    body = PACK.replace("legacy_key: a\n", "legacy_key: shared\n    knowledge_key: alpha\n")
    pack = parse_env_pack(_write_pack(tmp_path / "pack", body))
    assert pack.knowledge_platforms() == ("alpha", "shared")
    assert pack.knowledge_key("shared") == "alpha"          # 任务旧短键 → 知识库键
    assert pack.knowledge_key("b") == "b"                   # 非 knowledge 集群原样返回
    assert pack.knowledge_cluster("alpha").id == "alpha" and pack.knowledge_cluster("shared") is None
    assert (pack.services, pack.task_types) == ((), ())


def test_requires_core_accepts_current_core(tmp_path):
    body = PACK.replace("timezone: UTC", f'timezone: UTC\nrequires_core: ">={envpack.core_version().split("-")[0]},<999"')
    assert parse_env_pack(_write_pack(tmp_path / "pack", body)).id == "lab"


def test_config_and_playbook_paths_stay_inside_pack(tmp_path):
    pack = parse_env_pack(_write_pack(tmp_path / "pack", PACK))
    assert pack.config_file("topology/x.json") == pack.root / "config" / "topology" / "x.json"
    assert pack.playbook_path("playbooks/a/check.sh") == pack.root / "playbooks" / "a" / "check.sh"
    for bad in ("scripts/x.sh", "playbooks/../env.yaml", "/abs/playbooks/x.sh", ""):
        with pytest.raises(EnvPackError):
            pack.playbook_path(bad)
