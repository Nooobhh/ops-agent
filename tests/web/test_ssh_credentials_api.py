from dataclasses import dataclass

from fastapi.testclient import TestClient

from web.server.app import create_app


@dataclass
class Result:
    exit_code: int = 0
    timed_out: bool = False


def test_ssh_credentials_probe_only_accepts_registered_node(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    (config / "platform-alpha.yaml").write_text(
        "platform: alpha\nnodes:\n  node1:\n    host: 198.51.100.101\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "web.server.routes.credentials.probe_ssh_connection",
        lambda repo_root, platform, node: Result(),
    )
    client = TestClient(create_app(state_dir=tmp_path / "state", repo_root=tmp_path))

    response = client.post("/api/credentials/test/ssh", json={
        "platform": "alpha", "node": "node1",
    })
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["message"] == "SSH 连接成功"

    response = client.post("/api/credentials/test/ssh", json={
        "platform": "alpha", "node": "node99",
    })
    assert response.status_code == 422
