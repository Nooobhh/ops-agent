"""设数据根时,设置页写入的本机私有配置落 <数据根>/config/,仓库 config/ 不被写。"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from lib.data_root import DATA_ROOT_ENV
from web.server.app import create_app


def test_settings_writes_land_in_data_root_config(tmp_path, monkeypatch):
    from web.server import app as app_module

    monkeypatch.setattr(app_module.sys, "platform", "linux")
    repo, data = tmp_path / "repo", tmp_path / "data"
    repo.mkdir()
    monkeypatch.setenv(DATA_ROOT_ENV, str(data))
    app = create_app(state_dir=data / "state", repo_root=repo)
    with TestClient(app) as client:
        created = client.post("/api/model-settings/models", json={
            "title": "Primary", "base_url": "https://m.test/v1", "api_key": "k",
            "model": "gpt-test", "default_reasoning_effort": "high",
        })
        assert created.status_code == 201
        saved = client.put("/api/credentials/profiles/manager:alpha", json={"user": "u", "password": "p"})
        assert saved.status_code == 200

    config = data.resolve() / "config"
    assert (config / "model.local.yaml").is_file()
    stored = json.loads((config / "auth-profiles.json").read_text(encoding="utf-8"))
    assert stored["profiles"]["manager:alpha"]["password"] == "p"
    assert not (repo / "config").exists()
