"""Optional vector backend settings for the standalone retrieval layer."""
from __future__ import annotations

import os
import tempfile
import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

import yaml

from lib.data_root import local_config_path, resolve_data_root
from lib.file_lock import FileLock
from .embedding import (
    EmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
    SentenceTransformerEmbeddingProvider,
)
from .reranker import (
    OpenAICompatibleRerankProvider,
    RerankProvider,
    SentenceTransformerCrossEncoderProvider,
)
from .vector_store import EmbeddedVectorStore, MilvusVectorStore, VectorStore, VectorStoreError


class VectorSettingsError(ValueError):
    pass


DEFAULT_LOCAL_EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
DEFAULT_LOCAL_RERANK_MODEL = "BAAI/bge-reranker-v2-m3"


def _config_path(repo_root: Path) -> Path:
    return local_config_path(Path(repo_root).resolve(), "vector.local.yaml")


def _read_raw(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise VectorSettingsError("向量配置不可读") from exc
    if not isinstance(value, Mapping):
        raise VectorSettingsError("向量配置必须是 mapping")
    return dict(value)


def _valid_url(value: str, field: str, *, required: bool = False) -> str:
    stripped = str(value or "").strip().rstrip("/")
    if not stripped and not required:
        return ""
    parsed = urlparse(stripped)
    try:
        valid_port = parsed.port is None or 0 < parsed.port <= 65535
    except ValueError as exc:
        raise VectorSettingsError(f"{field} 端口不合法") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or not valid_port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise VectorSettingsError(f"{field} 必须是无凭证的 HTTP(S) 地址")
    return stripped


def _valid_name(value: str, field: str) -> str:
    stripped = str(value or "").strip()
    if not stripped or len(stripped) > 128 or not all(
        char.isalnum() or char in "_-" for char in stripped
    ) or not (stripped[0].isalnum() or stripped[0] == "_"):
        raise VectorSettingsError(f"{field} 格式不合法")
    return stripped


def _safe_embedded_path(repo_root: Path, value: str) -> str:
    """相对路径按数据根解析,且必须落在数据根内(数据根未设时即仓库根)。"""
    raw = str(value or "build/kb-vectors.sqlite").strip()
    path = Path(raw)
    data_root = resolve_data_root(repo_root).resolve()
    resolved = (data_root / path).resolve() if not path.is_absolute() else path.resolve()
    if resolved == Path(resolved.anchor) or resolved == data_root:
        raise VectorSettingsError("内置向量索引路径不能是文件系统根或数据根")
    try:
        relative = resolved.relative_to(data_root)
    except ValueError as exc:
        raise VectorSettingsError("内置向量索引路径必须位于数据根目录内") from exc
    if resolved.suffix.lower() != ".sqlite":
        raise VectorSettingsError("内置向量索引路径必须以 .sqlite 结尾")
    return relative.as_posix()


@dataclass(frozen=True)
class VectorSettings:
    backend: str = "embedded"
    embedded_path: Optional[Path] = None
    fallback_to_embedded: bool = True
    milvus_endpoint: str = ""
    milvus_collection: str = "ops_agent_chunks"
    milvus_token: str = ""
    milvus_vector_field: str = "vector"
    timeout_seconds: float = 10.0
    embedding_backend: str = "disabled"
    embedding_model: str = ""
    embedding_endpoint: str = ""
    embedding_api_key: str = ""
    embedding_dimensions: Optional[int] = None
    embedding_device: str = "cpu"
    rerank_backend: str = "disabled"
    rerank_model: str = ""
    rerank_endpoint: str = ""
    rerank_api_key: str = ""
    rerank_device: str = "cpu"
    rerank_max_candidates: int = 20
    rerank_max_length: int = 512

    @classmethod
    def load(cls, repo_root: Path) -> "VectorSettings":
        root = Path(repo_root).resolve()
        raw: Mapping[str, Any] = _read_raw(_config_path(root))
        backend = str(raw.get("backend") or "embedded").strip().lower()
        if backend not in {"embedded", "milvus", "auto"}:
            raise VectorSettingsError("vector backend 必须为 embedded、milvus 或 auto")
        data_root = resolve_data_root(root)
        embedded_path = Path(raw.get("embedded_path") or data_root / "build" / "kb-vectors.sqlite")
        if not embedded_path.is_absolute():
            embedded_path = data_root / embedded_path
        milvus = raw.get("milvus") if isinstance(raw.get("milvus"), Mapping) else {}
        embedding = raw.get("embedding") if isinstance(raw.get("embedding"), Mapping) else {}
        rerank = raw.get("rerank") if isinstance(raw.get("rerank"), Mapping) else {}
        endpoint = str(milvus.get("endpoint") or raw.get("milvus_endpoint") or "").strip()
        collection = str(milvus.get("collection") or raw.get("milvus_collection") or "ops_agent_chunks").strip()
        token = str(milvus.get("token") or raw.get("milvus_token") or "")
        vector_field = str(milvus.get("vector_field") or "vector").strip()
        if backend in {"milvus", "auto"} and (not endpoint or not collection):
            raise VectorSettingsError("milvus/auto 模式必须配置 endpoint 和 collection")
        embedding_backend = str(embedding.get("backend") or raw.get("embedding_backend") or "disabled").strip().lower()
        if embedding_backend not in {"disabled", "local", "external"}:
            raise VectorSettingsError("embedding backend 必须为 disabled、local 或 external")
        configured_model = str(embedding.get("model") or raw.get("embedding_model") or "").strip()
        if embedding_backend == "local" and not configured_model:
            configured_model = DEFAULT_LOCAL_EMBEDDING_MODEL
        rerank_backend = str(rerank.get("backend") or raw.get("rerank_backend") or "disabled").strip().lower()
        if rerank_backend not in {"disabled", "local", "external"}:
            raise VectorSettingsError("rerank backend 必须为 disabled、local 或 external")
        rerank_model = str(rerank.get("model") or raw.get("rerank_model") or "").strip()
        if rerank_backend == "local" and not rerank_model:
            rerank_model = DEFAULT_LOCAL_RERANK_MODEL
        rerank_endpoint = str(rerank.get("endpoint") or raw.get("rerank_endpoint") or "").strip()
        rerank_api_key = str(rerank.get("api_key") or raw.get("rerank_api_key") or "")
        try:
            rerank_max_candidates = int(rerank.get("max_candidates") or 20)
            rerank_max_length = int(rerank.get("max_length") or 512)
        except (TypeError, ValueError) as exc:
            raise VectorSettingsError("rerank 候选数或最大长度格式不合法") from exc
        if not 5 <= rerank_max_candidates <= 100:
            raise VectorSettingsError("Rerank 候选数必须在 5..100 之间")
        if not 64 <= rerank_max_length <= 4096:
            raise VectorSettingsError("Rerank 最大长度必须在 64..4096 之间")
        return cls(
            backend=backend,
            embedded_path=embedded_path.resolve(),
            fallback_to_embedded=bool(raw.get("fallback_to_embedded", True)),
            milvus_endpoint=endpoint,
            milvus_collection=collection,
            milvus_token=token,
            milvus_vector_field=vector_field,
            timeout_seconds=float(raw.get("timeout_seconds", 10.0)),
            embedding_backend=embedding_backend,
            embedding_model=configured_model,
            embedding_endpoint=str(embedding.get("endpoint") or raw.get("embedding_endpoint") or "").strip(),
            embedding_api_key=str(embedding.get("api_key") or raw.get("embedding_api_key") or ""),
            embedding_dimensions=(
                int(embedding["dimensions"])
                if embedding.get("dimensions") not in (None, "")
                else None
            ),
            embedding_device=str(embedding.get("device") or "cpu"),
            rerank_backend=rerank_backend,
            rerank_model=rerank_model,
            rerank_endpoint=rerank_endpoint,
            rerank_api_key=rerank_api_key,
            rerank_device=str(rerank.get("device") or "cpu"),
            rerank_max_candidates=rerank_max_candidates,
            rerank_max_length=rerank_max_length,
        )


def save_vector_settings(repo_root: Path, payload: Mapping[str, Any]) -> VectorSettings:
    """Validate and atomically save the machine-local vector configuration."""
    root = Path(repo_root).resolve()
    data = dict(payload)
    backend = str(data.get("backend") or "embedded").strip().lower()
    if backend not in {"embedded", "milvus", "auto"}:
        raise VectorSettingsError("vector backend 必须为 embedded、milvus 或 auto")
    embedded_path = _safe_embedded_path(root, str(data.get("embedded_path") or "build/kb-vectors.sqlite"))
    timeout = float(data.get("timeout_seconds", 10.0))
    if not 1 <= timeout <= 120:
        raise VectorSettingsError("timeout_seconds 必须在 1..120 之间")
    milvus = dict(data.get("milvus") or {})
    endpoint = _valid_url(milvus.get("endpoint", ""), "Milvus endpoint", required=backend in {"milvus", "auto"})
    collection = _valid_name(milvus.get("collection", "ops_agent_chunks"), "Milvus collection")
    vector_field = _valid_name(milvus.get("vector_field", "vector"), "Milvus vector_field")
    embedding = dict(data.get("embedding") or {})
    embedding_backend = str(embedding.get("backend") or "disabled").strip().lower()
    if embedding_backend not in {"disabled", "local", "external"}:
        raise VectorSettingsError("embedding backend 必须为 disabled、local 或 external")
    embedding_model = str(embedding.get("model") or "").strip()
    if embedding_backend == "local" and not embedding_model:
        embedding_model = DEFAULT_LOCAL_EMBEDDING_MODEL
    if embedding_backend != "disabled" and not embedding_model:
        raise VectorSettingsError("启用 embedding 时必须填写模型名称或本地模型路径")
    embedding_endpoint = _valid_url(embedding.get("endpoint", ""), "Embedding endpoint", required=embedding_backend == "external")
    dimensions = embedding.get("dimensions")
    if dimensions not in (None, "") and not 1 <= int(dimensions) <= 4096:
        raise VectorSettingsError("Embedding 维度必须在 1..4096 之间")
    if backend in {"milvus", "auto"} and not endpoint:
        raise VectorSettingsError("Milvus/auto 模式必须填写 endpoint")

    rerank = dict(data.get("rerank") or {})
    rerank_backend = str(rerank.get("backend") or "disabled").strip().lower()
    if rerank_backend not in {"disabled", "local", "external"}:
        raise VectorSettingsError("rerank backend 必须为 disabled、local 或 external")
    rerank_model = str(rerank.get("model") or "").strip()
    if rerank_backend == "local" and not rerank_model:
        rerank_model = DEFAULT_LOCAL_RERANK_MODEL
    if rerank_backend != "disabled" and not rerank_model:
        raise VectorSettingsError("启用 rerank 时必须填写模型名称或本地模型路径")
    rerank_endpoint = _valid_url(
        rerank.get("endpoint", ""),
        "Rerank endpoint",
        required=rerank_backend == "external",
    )
    rerank_max_candidates = int(rerank.get("max_candidates", 20))
    if not 5 <= rerank_max_candidates <= 100:
        raise VectorSettingsError("Rerank 候选数必须在 5..100 之间")
    rerank_max_length = int(rerank.get("max_length", 512))
    if not 64 <= rerank_max_length <= 4096:
        raise VectorSettingsError("Rerank 最大长度必须在 64..4096 之间")

    path = _config_path(root)
    lock_path = path.with_name(f".{path.name}.lock")
    raw = _read_raw(path)
    previous_milvus = raw.get("milvus") if isinstance(raw.get("milvus"), Mapping) else {}
    previous_embedding = raw.get("embedding") if isinstance(raw.get("embedding"), Mapping) else {}
    previous_rerank = raw.get("rerank") if isinstance(raw.get("rerank"), Mapping) else {}
    milvus_config = {
        "endpoint": endpoint,
        "collection": collection,
        "vector_field": vector_field,
    }
    if milvus.get("token") is not None:
        milvus_config["token"] = str(milvus["token"])
    elif previous_milvus.get("token"):
        milvus_config["token"] = str(previous_milvus["token"])
    embedding_config = {
        "backend": embedding_backend,
        "model": embedding_model,
        "endpoint": embedding_endpoint,
        "dimensions": None if dimensions in (None, "") else int(dimensions),
        "device": str(embedding.get("device") or "cpu").strip(),
    }
    if embedding.get("api_key") is not None:
        embedding_config["api_key"] = str(embedding["api_key"])
    elif previous_embedding.get("api_key"):
        embedding_config["api_key"] = str(previous_embedding["api_key"])
    rerank_config = {
        "backend": rerank_backend,
        "model": rerank_model,
        "endpoint": rerank_endpoint,
        "device": str(rerank.get("device") or "cpu").strip(),
        "max_candidates": rerank_max_candidates,
        "max_length": rerank_max_length,
    }
    if rerank.get("api_key") is not None:
        rerank_config["api_key"] = str(rerank["api_key"])
    elif previous_rerank.get("api_key"):
        rerank_config["api_key"] = str(previous_rerank["api_key"])
    raw.update({
        "backend": backend,
        "embedded_path": embedded_path,
        "fallback_to_embedded": bool(data.get("fallback_to_embedded", True)),
        "timeout_seconds": timeout,
        "milvus": milvus_config,
        "embedding": embedding_config,
        "rerank": rerank_config,
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(lock_path)):
        fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(raw_temp)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                yaml.safe_dump(raw, stream, allow_unicode=True, sort_keys=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return VectorSettings.load(root)


def public_vector_settings(repo_root: Path) -> dict[str, Any]:
    """Return settings safe for a browser; secrets are represented by booleans."""
    root = Path(repo_root).resolve()
    settings = VectorSettings.load(root)
    data_root = resolve_data_root(root).resolve()
    build_dir = data_root / "build"
    embedded_path = settings.embedded_path or build_dir / "kb-vectors.sqlite"
    retrieval_path = build_dir / "kb-retrieval.sqlite"
    vector_count = 0
    vector_dimension = None
    if embedded_path.is_file():
        try:
            import sqlite3
            with sqlite3.connect(f"file:{embedded_path}?mode=ro", uri=True) as connection:
                row = connection.execute("SELECT dimension FROM vector_meta").fetchone()
                vector_dimension = int(row[0]) if row else None
                vector_count = int(connection.execute("SELECT count(*) FROM vectors").fetchone()[0])
        except (OSError, sqlite3.Error, ValueError):
            vector_count = 0
            vector_dimension = None
    local_runtime_available = importlib.util.find_spec("sentence_transformers") is not None
    local_rerank_runtime_available = local_runtime_available
    model_ready = bool(settings.embedding_model) and (
        settings.embedding_backend != "local" or local_runtime_available
    )
    return {
        "schema_version": 1,
        "settings": {
            "backend": settings.backend,
            "embedded_path": embedded_path.relative_to(data_root).as_posix()
            if embedded_path.is_relative_to(data_root) else str(embedded_path),
            "fallback_to_embedded": settings.fallback_to_embedded,
            "timeout_seconds": settings.timeout_seconds,
            "milvus": {
                "endpoint": settings.milvus_endpoint,
                "collection": settings.milvus_collection,
                "vector_field": settings.milvus_vector_field,
                "token_configured": bool(settings.milvus_token),
            },
            "embedding": {
                "backend": settings.embedding_backend,
                "model": settings.embedding_model,
                "endpoint": settings.embedding_endpoint,
                "api_key_configured": bool(settings.embedding_api_key),
                "dimensions": settings.embedding_dimensions,
                "device": settings.embedding_device,
            },
            "rerank": {
                "backend": settings.rerank_backend,
                "model": settings.rerank_model,
                "endpoint": settings.rerank_endpoint,
                "api_key_configured": bool(settings.rerank_api_key),
                "device": settings.rerank_device,
                "max_candidates": settings.rerank_max_candidates,
                "max_length": settings.rerank_max_length,
            },
        },
        "status": {
            "retrieval_index_available": retrieval_path.is_file(),
            "embedded_vector_available": embedded_path.is_file(),
            "embedded_vector_count": vector_count,
            "embedded_vector_dimension": vector_dimension,
            "local_embedding_runtime_available": local_runtime_available,
            "embedding_model_configured": bool(settings.embedding_model),
            "embedding_model": settings.embedding_model,
            "local_rerank_runtime_available": local_rerank_runtime_available,
            "rerank_model_configured": bool(settings.rerank_model),
            "rerank_model": settings.rerank_model,
            "rerank_ready": bool(
                settings.rerank_backend != "disabled"
                and settings.rerank_model
                and (settings.rerank_backend != "local" or local_rerank_runtime_available)
            ),
            "semantic_ready": bool(
                model_ready
                and settings.embedding_backend != "disabled"
                and (settings.backend == "embedded" and embedded_path.is_file()
                     or settings.backend in {"milvus", "auto"})
            ),
            "milvus_connection": "not_tested",
            "config_file": str(_config_path(root).relative_to(data_root)),
        },
    }


class FallbackVectorStore:
    """Use the configured remote store first and an explicit local fallback."""

    def __init__(self, primary: VectorStore, fallback: VectorStore):
        self.primary = primary
        self.fallback = fallback
        self.last_backend = "primary"

    def health(self) -> dict[str, Any]:
        primary = self.primary.health()
        fallback = self.fallback.health()
        return {
            "backend": primary.get("backend", "primary"),
            "available": bool(primary.get("available")) or bool(fallback.get("available")),
            "primary": primary,
            "fallback": fallback,
        }

    def search(self, query_vector, filters, top_k=10):
        try:
            result = self.primary.search(query_vector, filters, top_k)
            self.last_backend = "primary"
            return result
        except Exception as primary_error:
            try:
                result = self.fallback.search(query_vector, filters, top_k)
                self.last_backend = "fallback"
                return result
            except Exception as fallback_error:
                raise VectorStoreError("向量主后端和内置后端均不可用") from fallback_error

    def upsert(self, records):
        self.primary.upsert(records)


def create_vector_store(repo_root: Path) -> VectorStore:
    settings = VectorSettings.load(repo_root)
    embedded = EmbeddedVectorStore(
        settings.embedded_path or resolve_data_root(Path(repo_root)) / "build" / "kb-vectors.sqlite"
    )
    if settings.backend == "embedded":
        return embedded
    remote = MilvusVectorStore(
        settings.milvus_endpoint,
        settings.milvus_collection,
        token=settings.milvus_token,
        vector_field=settings.milvus_vector_field,
        timeout_seconds=settings.timeout_seconds,
    )
    if settings.backend == "auto" and settings.fallback_to_embedded:
        return FallbackVectorStore(remote, embedded)
    return remote


def create_embedding_provider(repo_root: Path) -> Optional[EmbeddingProvider]:
    settings = VectorSettings.load(repo_root)
    if settings.embedding_backend == "disabled" or not settings.embedding_model:
        return None
    if settings.embedding_backend == "local":
        model_id = settings.embedding_model
        model_path = Path(model_id)
        if not model_path.is_absolute() and (Path(repo_root).resolve() / model_path).is_dir():
            model_id = str((Path(repo_root).resolve() / model_path).resolve())
        return SentenceTransformerEmbeddingProvider(
            model_id, device=settings.embedding_device,
        )
    if not settings.embedding_endpoint:
        raise VectorSettingsError("external embedding 必须配置 endpoint")
    return OpenAICompatibleEmbeddingProvider(
        settings.embedding_endpoint,
        settings.embedding_model,
        api_key=settings.embedding_api_key,
        dimensions=settings.embedding_dimensions,
        timeout_seconds=settings.timeout_seconds,
    )


def create_rerank_provider(repo_root: Path) -> Optional[RerankProvider]:
    settings = VectorSettings.load(repo_root)
    if settings.rerank_backend == "disabled" or not settings.rerank_model:
        return None
    if settings.rerank_backend == "local":
        model_id = settings.rerank_model
        model_path = Path(model_id)
        if not model_path.is_absolute() and (Path(repo_root).resolve() / model_path).is_dir():
            model_id = str((Path(repo_root).resolve() / model_path).resolve())
        return SentenceTransformerCrossEncoderProvider(
            model_id,
            device=settings.rerank_device,
            max_candidates=settings.rerank_max_candidates,
            max_length=settings.rerank_max_length,
        )
    if not settings.rerank_endpoint:
        raise VectorSettingsError("external rerank 必须配置 endpoint")
    return OpenAICompatibleRerankProvider(
        settings.rerank_endpoint,
        settings.rerank_model,
        api_key=settings.rerank_api_key,
        max_candidates=settings.rerank_max_candidates,
        timeout_seconds=settings.timeout_seconds,
    )
