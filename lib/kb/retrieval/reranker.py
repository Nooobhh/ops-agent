"""Optional Cross-Encoder rerank providers.

Reranking is deliberately separate from vector search.  The vector/FTS/topology
layers produce a bounded candidate set first; a Cross-Encoder then scores the
query together with each candidate chunk.  The dependency is imported lazily so
the default retrieval path and the desktop package do not pay its startup cost.
"""
from __future__ import annotations

import json
import math
import threading
from typing import Any, Mapping, Protocol, Sequence
from urllib import request as urlrequest

# 与 embedding 同理:Cross-Encoder 按 (model_id, device, max_length) 进程级复用
_LOCAL_MODELS: dict[tuple[str, str, int], Any] = {}
_LOCAL_MODELS_LOCK = threading.Lock()


class RerankError(RuntimeError):
    """A rerank backend is unavailable or returned invalid scores."""


class RerankProvider(Protocol):
    model_id: str
    max_candidates: int

    def rerank(self, query: str, documents: Sequence[str]) -> tuple[float, ...]: ...


def normalize_model_score(value: float) -> float:
    """Map logits or probability-like scores to a stable 0..1 range."""
    score = float(value)
    if not math.isfinite(score):
        raise RerankError("rerank 返回了非有限分数")
    if 0.0 <= score <= 1.0:
        return score
    # Most Cross-Encoder checkpoints return a single relevance logit.
    score = max(-60.0, min(60.0, score))
    return 1.0 / (1.0 + math.exp(-score))


class SentenceTransformerCrossEncoderProvider:
    """Lazy local ``sentence-transformers`` Cross-Encoder provider."""

    def __init__(
        self,
        model_id: str,
        *,
        device: str = "cpu",
        max_candidates: int = 20,
        max_length: int = 512,
    ) -> None:
        self.backend = "local"
        self.model_id = str(model_id)
        self.device = str(device or "cpu")
        self.max_candidates = int(max_candidates)
        self.max_length = int(max_length)
        self._model = None

    def _get_model(self):
        if self._model is None:
            key = (self.model_id, self.device, self.max_length)
            with _LOCAL_MODELS_LOCK:
                if key not in _LOCAL_MODELS:
                    try:
                        from sentence_transformers import CrossEncoder
                    except ImportError as exc:
                        raise RerankError(
                            "未安装 sentence-transformers，无法使用 local Cross-Encoder"
                        ) from exc
                    try:
                        _LOCAL_MODELS[key] = CrossEncoder(
                            self.model_id,
                            device=self.device,
                            max_length=self.max_length,
                        )
                    except Exception as exc:
                        raise RerankError("本地 Cross-Encoder 模型加载失败") from exc
                self._model = _LOCAL_MODELS[key]
        return self._model

    def rerank(self, query: str, documents: Sequence[str]) -> tuple[float, ...]:
        if not documents:
            return ()
        if len(documents) > self.max_candidates:
            raise RerankError("rerank 候选数超过配置上限")
        pairs = [(str(query), str(document)) for document in documents]
        try:
            raw_scores = self._get_model().predict(
                pairs,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
            values = list(raw_scores) if hasattr(raw_scores, "__iter__") else [raw_scores]
            if len(values) != len(documents):
                raise RerankError("Cross-Encoder 返回数量不一致")
            return tuple(normalize_model_score(float(value)) for value in values)
        except RerankError:
            raise
        except Exception as exc:
            raise RerankError("本地 Cross-Encoder 推理失败") from exc


class OpenAICompatibleRerankProvider:
    """Call a service exposing a common ``POST /rerank`` contract."""

    def __init__(
        self,
        endpoint: str,
        model_id: str,
        *,
        api_key: str = "",
        max_candidates: int = 20,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.backend = "external"
        self.endpoint = str(endpoint).rstrip("/")
        self.model_id = str(model_id)
        self.api_key = str(api_key or "")
        self.max_candidates = int(max_candidates)
        self.timeout_seconds = float(timeout_seconds)

    def rerank(self, query: str, documents: Sequence[str]) -> tuple[float, ...]:
        if not documents:
            return ()
        if len(documents) > self.max_candidates:
            raise RerankError("rerank 候选数超过配置上限")
        payload = {
            "model": self.model_id,
            "query": str(query),
            "documents": list(documents),
            "top_n": len(documents),
            "return_documents": False,
        }
        url = self.endpoint if self.endpoint.endswith("/rerank") else f"{self.endpoint}/rerank"
        request = urlrequest.Request(
            url,
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
            raise RerankError("外部 rerank 服务请求失败") from exc
        rows: Any = result.get("results") if isinstance(result, Mapping) else None
        if rows is None and isinstance(result, Mapping):
            rows = result.get("data")
        if not isinstance(rows, list):
            raise RerankError("rerank 服务未返回 results 或 data")
        ordered: list[float | None] = [None] * len(documents)
        for position, row in enumerate(rows):
            if not isinstance(row, Mapping):
                raise RerankError("rerank 返回项格式不合法")
            index = int(row.get("index", position))
            if index < 0 or index >= len(documents):
                raise RerankError("rerank 返回了越界 index")
            raw = row.get("relevance_score", row.get("score"))
            if raw is None:
                raise RerankError("rerank 返回项缺少 score")
            ordered[index] = normalize_model_score(float(raw))
        if any(value is None for value in ordered):
            raise RerankError("rerank 返回数量不完整")
        return tuple(float(value) for value in ordered)
