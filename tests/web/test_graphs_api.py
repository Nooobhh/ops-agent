from __future__ import annotations

import json
import sys

import yaml
from fastapi.testclient import TestClient

from tests.envpack_helpers import as_env_pack
from web.server.app import create_app


def _repo(tmp_path, monkeypatch, graphs):
    monkeypatch.setattr(sys, "path", list(sys.path))
    root = as_env_pack(tmp_path)
    env = yaml.safe_load((root / "env.yaml").read_text(encoding="utf-8"))
    env.update(python="python", extensions={"graphs": graphs})
    (root / "env.yaml").write_text(yaml.safe_dump(env, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (root / "python").mkdir()
    (root / "python" / "fake_graph_api.py").write_text("def collect(context):\n    raise RuntimeError('no')\n",
                                                      encoding="utf-8")
    return root


def test_graph_list_status_and_missing_snapshot(tmp_path, monkeypatch):
    root = _repo(tmp_path, monkeypatch, [{"id": "jobs", "title": "作业", "collector": "fake_graph_api:collect"}])
    with TestClient(create_app(state_dir=root / "state", repo_root=root)) as client:
        assert client.get("/api/knowledge/graphs").json() == {"graphs": [{"id": "jobs", "title": "作业"}]}
        status = client.get("/api/knowledge/graphs/jobs/status")
        assert status.status_code == 200 and status.json()["running"] is False
        response = client.get("/api/knowledge/graphs/jobs")
        assert response.status_code == 503 and "尚未生成" in response.json()["detail"]
        assert client.get("/api/knowledge/graphs/other").status_code == 404


def test_graph_returns_the_published_snapshot(tmp_path, monkeypatch):
    root = _repo(tmp_path, monkeypatch, [{"id": "jobs", "collector": "fake_graph_api:collect"}])
    path = root / "state" / "graphs" / "jobs" / "graph-latest.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": 1, "topology_kind": "jobs", "nodes": [], "edges": []}),
                    encoding="utf-8")
    with TestClient(create_app(state_dir=root / "state", repo_root=root)) as client:
        response = client.get("/api/knowledge/graphs/jobs")
        assert response.status_code == 200 and response.json()["topology_kind"] == "jobs"


def test_broken_declaration_is_reported_not_fatal(tmp_path, monkeypatch):
    root = _repo(tmp_path, monkeypatch, [{"id": "jobs"}])
    with TestClient(create_app(state_dir=root / "state", repo_root=root)) as client:
        response = client.get("/api/knowledge/graphs")
        assert response.status_code == 500 and "图谱声明不可用" in response.json()["detail"]
        assert client.get("/api/health").status_code in (200, 404)
