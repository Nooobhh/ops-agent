"""Optional external vector-store and embedding settings endpoints."""
from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from lib.kb.retrieval.settings import (
    VectorSettingsError,
    public_vector_settings,
    save_vector_settings,
)
from lib.kb.retrieval.vector_store import MilvusVectorStore
from web.server.context import RequestContext, request_context


class MilvusSettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint: str = ""
    collection: str = "ops_agent_chunks"
    vector_field: str = "vector"
    # null means preserve the saved token; an explicit empty string clears it.
    token: Optional[str] = None


class EmbeddingSettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: Literal["disabled", "local", "external"] = "disabled"
    model: str = ""
    endpoint: str = ""
    api_key: Optional[str] = None
    dimensions: Optional[int] = Field(default=None, ge=1, le=4096)
    device: str = "cpu"


class RerankSettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: Literal["disabled", "local", "external"] = "disabled"
    model: str = ""
    endpoint: str = ""
    # null means preserve the saved key; an explicit empty string clears it.
    api_key: Optional[str] = None
    device: str = "cpu"
    max_candidates: int = Field(default=20, ge=5, le=100)
    max_length: int = Field(default=512, ge=64, le=4096)


class VectorSettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    backend: Literal["embedded", "milvus", "auto"] = "embedded"
    embedded_path: str = "build/kb-vectors.sqlite"
    fallback_to_embedded: bool = True
    timeout_seconds: float = Field(default=10, ge=1, le=120)
    milvus: MilvusSettingsBody = MilvusSettingsBody()
    embedding: EmbeddingSettingsBody = EmbeddingSettingsBody()
    rerank: RerankSettingsBody = RerankSettingsBody()


def router(*, repo_root: Path) -> APIRouter:
    routes = APIRouter()
    repo_root = Path(repo_root).resolve()

    @routes.get("/api/vector-settings")
    def get_vector_settings(
        _context: RequestContext = Depends(request_context),
    ) -> dict:
        try:
            return public_vector_settings(repo_root)
        except VectorSettingsError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @routes.put("/api/vector-settings")
    def put_vector_settings(
        body: VectorSettingsBody,
        _context: RequestContext = Depends(request_context),
    ) -> dict:
        try:
            save_vector_settings(repo_root, body.model_dump(exclude={"schema_version"}))
            return public_vector_settings(repo_root)
        except (VectorSettingsError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @routes.post("/api/vector-settings/test")
    def test_vector_settings(
        body: VectorSettingsBody,
        _context: RequestContext = Depends(request_context),
    ) -> dict:
        """Test a configured Milvus endpoint without writing settings."""
        if body.backend == "embedded":
            current = public_vector_settings(repo_root)
            available = bool(current["status"]["embedded_vector_available"])
            return {
                "ok": available,
                "backend": "embedded",
                "message": "内置向量索引可用" if available else "内置向量索引尚未生成",
            }
        try:
            current = public_vector_settings(repo_root)
            configured = current["settings"]["milvus"]
            endpoint = body.milvus.endpoint.strip() or configured["endpoint"]
            collection = body.milvus.collection.strip() or configured["collection"]
            token = body.milvus.token
            if token is None:
                from lib.kb.retrieval.settings import VectorSettings
                token = VectorSettings.load(repo_root).milvus_token
            client = MilvusVectorStore(
                endpoint,
                collection,
                token=token or "",
                vector_field=body.milvus.vector_field or configured["vector_field"],
                timeout_seconds=body.timeout_seconds,
            )
            health = client.health()
            return {
                "ok": bool(health.get("available")),
                "backend": "milvus",
                "message": "Milvus 连接成功" if health.get("available") else "Milvus 连接失败",
            }
        except (VectorSettingsError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return routes
