"""图谱(环境包 graphs 扩展):声明校验、快照读取、旧状态目录迁移与单飞采集执行器。"""
import json
import sys

import pytest
import yaml

from lib.envpack import EnvPackError, load_env_pack
from lib.graphs import GraphDecl, GraphManager, graph_decls, graph_path, migrate_legacy, read_graph
from tests.envpack_helpers import as_env_pack

EXT = '''
from lib.atomic_file import atomic_write_json

CONTEXTS = []


def collect(context):
    CONTEXTS.append(context)
    graph = {"schema_version": 1, "topology_kind": "jobs", "update_mode": context.mode,
             "nodes": [], "edges": [], "view": {"count_kind": "job", "count": 0}}
    atomic_write_json(context.graph_dir / "graph-latest.json", graph)
    return graph


def broken(context):
    raise RuntimeError("upstream down")
'''


def _pack(tmp_path, monkeypatch, graphs):
    monkeypatch.setattr(sys, "path", list(sys.path))
    root = as_env_pack(tmp_path)
    env = yaml.safe_load((root / "env.yaml").read_text(encoding="utf-8"))
    env.update(python="python", extensions={"graphs": graphs})
    (root / "env.yaml").write_text(yaml.safe_dump(env, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (root / "python").mkdir()
    (root / "python" / "fake_graph_ext.py").write_text(EXT, encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def _drop_module():
    yield
    sys.modules.pop("fake_graph_ext", None)


def test_declarations_are_validated(tmp_path, monkeypatch):
    root = _pack(tmp_path, monkeypatch, [{"id": "jobs", "title": "作业", "collector": "fake_graph_ext:collect",
                                          "refresh": "weekly", "legacy_state_dir": "old-jobs"}])
    assert graph_decls(load_env_pack(root)) == (
        GraphDecl(id="jobs", title="作业", collector="fake_graph_ext:collect", refresh="weekly",
                  legacy_state_dir="old-jobs"),
    )
    for bad in ([{"id": "jobs"}], [{"id": "Bad Id", "collector": "m:f"}],
                [{"id": "jobs", "collector": "m:f", "refresh": "hourly"}],
                [{"id": "jobs", "collector": "m:f"}, {"id": "jobs", "collector": "m:g"}]):
        root = _pack(tmp_path / str(len(str(bad))), monkeypatch, bad)
        with pytest.raises(EnvPackError):
            graph_decls(load_env_pack(root))


def test_read_graph_rejects_missing_or_foreign_snapshots(tmp_path):
    state = tmp_path / "state"
    assert read_graph(state, "jobs") is None
    path = graph_path(state, "jobs")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": 1, "topology_kind": "other"}), encoding="utf-8")
    assert read_graph(state, "jobs") is None
    path.write_text(json.dumps({"schema_version": 1, "topology_kind": "jobs", "nodes": []}), encoding="utf-8")
    assert read_graph(state, "jobs")["nodes"] == []


def test_legacy_state_is_copied_once_and_left_for_the_previous_version(tmp_path):
    state = tmp_path / "state"
    legacy = state / "old-jobs"
    legacy.mkdir(parents=True)
    (legacy / "graph-latest.json").write_text("v1", encoding="utf-8")
    decl = GraphDecl(id="jobs", title="作业", collector="m:f", legacy_state_dir="old-jobs")
    migrate_legacy(state, decl)
    assert graph_path(state, "jobs").read_text(encoding="utf-8") == "v1"
    assert (legacy / "graph-latest.json").is_file()  # 回退到上一版时它仍读旧目录
    (legacy / "graph-latest.json").write_text("v2", encoding="utf-8")
    migrate_legacy(state, decl)
    assert graph_path(state, "jobs").read_text(encoding="utf-8") == "v1"


def test_manager_runs_the_pack_collector_and_records_failures(tmp_path, monkeypatch):
    root = _pack(tmp_path, monkeypatch, [{"id": "jobs", "collector": "fake_graph_ext:collect"},
                                         {"id": "broken", "collector": "fake_graph_ext:broken"}])
    pack = load_env_pack(root)
    good, bad = (GraphManager(root, root / "state", decl, pack) for decl in graph_decls(pack))
    try:
        assert good.status()["graph"] is None and good.status()["running"] is False
        graph = good.run_sync("incremental")
        context = sys.modules["fake_graph_ext"].CONTEXTS[-1]
        assert context.graph_dir == root.resolve() / "state" / "graphs" / "jobs" and context.mode == "incremental"
        assert read_graph(root / "state", "jobs") == graph
        assert good.status()["state"] == "ready"
        with pytest.raises(RuntimeError, match="upstream down"):
            bad.run_sync()
        assert bad.status()["last_error"] == "RuntimeError: upstream down"
        with pytest.raises(ValueError):
            good.trigger("bogus")
    finally:
        good.stop()
        bad.stop()
