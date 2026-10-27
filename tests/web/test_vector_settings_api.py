from __future__ import annotations

import yaml
from fastapi.testclient import TestClient

from web.server.app import create_app


def _payload(
    *,
    token: str | None = "secret-token",
    api_key: str | None = "secret-key",
    rerank_api_key: str | None = "rerank-secret",
):
    milvus = {
        "endpoint": "http://milvus.internal:9091",
        "collection": "ops_agent_chunks",
        "vector_field": "vector",
    }
    if token is not None:
        milvus["token"] = token
    embedding = {
        "backend": "external",
        "model": "embedding-model",
        "endpoint": "http://embedding.internal:8000/v1",
        "dimensions": 384,
        "device": "cpu",
    }
    if api_key is not None:
        embedding["api_key"] = api_key
    rerank = {
        "backend": "local",
        "model": "local-reranker",
        "endpoint": "",
        "max_candidates": 24,
        "max_length": 384,
        "device": "cpu",
    }
    if rerank_api_key is not None:
        rerank["api_key"] = rerank_api_key
    return {
        "schema_version": 1,
        "backend": "milvus",
        "embedded_path": "build/kb-vectors.sqlite",
        "fallback_to_embedded": True,
        "timeout_seconds": 10,
        "milvus": milvus,
        "embedding": embedding,
        "rerank": rerank,
    }


def test_vector_settings_are_sanitized_and_tokens_are_preserved(tmp_path, monkeypatch):
    from web.server import app as app_module

    monkeypatch.setattr(app_module.sys, "platform", "linux")
    app = create_app(state_dir=tmp_path / "state", repo_root=tmp_path)
    with TestClient(app) as client:
        initial = client.get("/api/vector-settings")
        assert initial.status_code == 200
        assert initial.json()["settings"]["backend"] == "embedded"
        assert initial.json()["settings"]["milvus"]["token_configured"] is False
        assert initial.json()["settings"]["rerank"]["backend"] == "disabled"

        saved = client.put("/api/vector-settings", json=_payload())
        assert saved.status_code == 200
        assert "secret-token" not in saved.text
        assert "secret-key" not in saved.text
        assert saved.json()["settings"]["milvus"]["token_configured"] is True
        assert saved.json()["settings"]["embedding"]["api_key_configured"] is True
        assert saved.json()["settings"]["rerank"]["backend"] == "local"
        assert saved.json()["settings"]["rerank"]["max_candidates"] == 24

        config = yaml.safe_load((tmp_path / "config" / "vector.local.yaml").read_text())
        assert config["milvus"]["token"] == "secret-token"
        assert config["embedding"]["api_key"] == "secret-key"
        assert config["rerank"]["api_key"] == "rerank-secret"

        preserved = client.put("/api/vector-settings", json=_payload(token=None, api_key=None, rerank_api_key=None))
        assert preserved.status_code == 200
        config = yaml.safe_load((tmp_path / "config" / "vector.local.yaml").read_text())
        assert config["milvus"]["token"] == "secret-token"
        assert config["embedding"]["api_key"] == "secret-key"
        assert config["rerank"]["api_key"] == "rerank-secret"


def test_vector_settings_validate_endpoint_and_embedded_test_is_read_only(tmp_path, monkeypatch):
    from web.server import app as app_module

    monkeypatch.setattr(app_module.sys, "platform", "linux")
    app = create_app(state_dir=tmp_path / "state", repo_root=tmp_path)
    with TestClient(app) as client:
        invalid = _payload()
        invalid["milvus"]["endpoint"] = "http://user:pass@example.internal:9091"
        response = client.put("/api/vector-settings", json=invalid)
        assert response.status_code == 422

        embedded = _payload()
        embedded["backend"] = "embedded"
        embedded["embedding"] = {"backend": "disabled"}
        result = client.post("/api/vector-settings/test", json=embedded)
        assert result.status_code == 200
        assert result.json()["backend"] == "embedded"
        assert result.json()["ok"] is False
