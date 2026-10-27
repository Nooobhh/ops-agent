"""Optional embedding providers for the standalone retrieval layer."""
from __future__ import annotations

import json
import threading
from typing import Any, Mapping, Protocol, Sequence
from urllib import request as urlrequest

# 本地模型按 (model_id, device) 进程级复用:中台每次构建 ContextPack 都新建
# provider,不复用则每次重新加载模型(秒级),远超检索本身的耗时。
_LOCAL_MODELS: dict[tuple[str, str], Any] = {}
_LOCAL_MODELS_LOCK = threading.Lock()


class EmbeddingError(RuntimeError):
    """An embedding backend is unavailable or returned invalid vectors."""


class EmbeddingProvider(Protocol):
    model_id: str

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]: ...


class OpenAICompatibleEmbeddingProvider:
    """Call an OpenAI-compatible ``/embeddings`` endpoint."""

    def __init__(
        self,
        endpoint: str,
        model_id: str,
        *,
        api_key: str = "",
        dimensions: int | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.endpoint = str(endpoint).rstrip("/")
        self.model_id = str(model_id)
        self.api_key = api_key
        self.dimensions = dimensions
        self.timeout_seconds = float(timeout_seconds)

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        if not texts:
            return ()
        payload: dict[str, Any] = {"model": self.model_id, "input": list(texts)}
        if self.dimensions is not None:
            payload["dimensions"] = int(self.dimensions)
        request = urlrequest.Request(
            f"{self.endpoint}/embeddings",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
            },
            method="POST",
        )
        try:
            with urlrequest.urlopen(request, timeout=self.timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise EmbeddingError("Embedding 服务请求失败") from exc
        rows = result.get("data") if isinstance(result, Mapping) else None
        if not isinstance(rows, list):
            raise EmbeddingError("Embedding 服务未返回 data")
        ordered = sorted(
            (row for row in rows if isinstance(row, Mapping)),
            key=lambda row: int(row.get("index", 0)),
        )
        vectors = []
        for row in ordered:
            value = row.get("embedding")
            if not isinstance(value, list) or not value:
                raise EmbeddingError("Embedding 返回向量为空")
            vectors.append(tuple(float(item) for item in value))
        if len(vectors) != len(texts) or len({len(item) for item in vectors}) != 1:
            raise EmbeddingError("Embedding 返回数量或维度不一致")
        return tuple(vectors)


class SentenceTransformerEmbeddingProvider:
    """Lazy local provider; the optional dependency is not imported at startup."""

    def __init__(self, model_id: str, *, device: str = "cpu") -> None:
        self.model_id = str(model_id)
        self.device = device
        self._model = None

    def _get_model(self):
        if self._model is None:
            key = (self.model_id, self.device)
            with _LOCAL_MODELS_LOCK:
                if key not in _LOCAL_MODELS:
                    try:
                        from sentence_transformers import SentenceTransformer
                    except ImportError as exc:
                        raise EmbeddingError(
                            "未安装 sentence-transformers，无法使用 local embedding"
                        ) from exc
                    _LOCAL_MODELS[key] = SentenceTransformer(self.model_id, device=self.device)
                self._model = _LOCAL_MODELS[key]
        return self._model

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        if not texts:
            return ()
        try:
            values = self._get_model().encode(
                list(texts), normalize_embeddings=True, convert_to_numpy=True,
            )
            return tuple(tuple(float(item) for item in row) for row in values)
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError("本地 embedding 推理失败") from exc
