"""Optional vector-store backends for ContextPack retrieval.

The embedded backend deliberately uses only the Python standard library.  It is
adequate for the current corpus and keeps the desktop package free of a resident
database process.  Milvus is an optional HTTP backend; the knowledge source and
topology source remain local truth in both modes.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import struct
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Protocol, Sequence
from urllib import request as urlrequest


class VectorStoreError(RuntimeError):
    """A vector backend is unavailable or returned an invalid response."""


class VectorStore(Protocol):
    def health(self) -> Dict[str, Any]: ...

    def search(
        self,
        query_vector: Sequence[float],
        filters: Mapping[str, Any],
        top_k: int = 10,
    ) -> tuple["VectorHit", ...]: ...

    def upsert(self, records: Sequence[VectorRecord]) -> None: ...


@dataclass(frozen=True)
class VectorRecord:
    record_id: str
    object_type: str
    object_id: str
    doc_id: str
    chunk_id: str
    platform: str
    service: str
    graph_id: str
    snapshot_id: str
    vector: tuple[float, ...]
    content_hash: str = ""
    metadata: Optional[Mapping[str, Any]] = None

    def __post_init__(self) -> None:
        if self.metadata is None:
            object.__setattr__(self, "metadata", {})


@dataclass(frozen=True)
class VectorHit:
    record_id: str
    object_type: str
    object_id: str
    doc_id: str
    chunk_id: str
    score: float
    metadata: Dict[str, Any]


class EmbeddedVectorStore:
    """A small SQLite-backed vector store with deterministic brute-force search."""

    SCHEMA_VERSION = 1

    def __init__(self, path: Path):
        self.path = Path(path)

    @staticmethod
    def _encode(values: Sequence[float]) -> bytes:
        return struct.pack(f"<{len(values)}f", *(float(value) for value in values))

    @staticmethod
    def _decode(blob: bytes, dimension: int) -> tuple[float, ...]:
        if len(blob) != dimension * 4:
            raise VectorStoreError("内置向量记录维度或数据长度不匹配")
        return tuple(struct.unpack(f"<{dimension}f", blob))

    @staticmethod
    def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
        if len(left) != len(right):
            return -1.0
        dot = sum(a * b for a, b in zip(left, right))
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(value * value for value in right))
        if left_norm == 0 or right_norm == 0:
            return 0.0
        return dot / (left_norm * right_norm)

    @classmethod
    def initialize(cls, path: Path, records: Iterable[VectorRecord] = ()) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, raw_temp = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent,
        )
        os.close(fd)
        temporary = Path(raw_temp)
        try:
            with closing(sqlite3.connect(temporary)) as connection:
                with connection:
                    connection.execute(
                        "CREATE TABLE vector_meta(schema_version INTEGER NOT NULL, dimension INTEGER NOT NULL)"
                    )
                    materialized = list(records)
                    dimension = len(materialized[0].vector) if materialized else 0
                    if any(len(item.vector) != dimension for item in materialized):
                        raise VectorStoreError("内置向量记录维度不一致")
                    connection.execute(
                        "INSERT INTO vector_meta VALUES (?, ?)",
                        (cls.SCHEMA_VERSION, dimension),
                    )
                    connection.execute(
                        "CREATE TABLE vectors("
                        "record_id TEXT PRIMARY KEY, object_type TEXT NOT NULL, object_id TEXT NOT NULL, "
                        "doc_id TEXT NOT NULL, chunk_id TEXT NOT NULL, platform TEXT NOT NULL, service TEXT NOT NULL, "
                        "graph_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, content_hash TEXT NOT NULL, "
                        "vector BLOB NOT NULL, metadata_json TEXT NOT NULL)"
                    )
                    connection.executemany(
                        "INSERT INTO vectors VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            (
                                item.record_id, item.object_type, item.object_id,
                                item.doc_id, item.chunk_id, item.platform, item.service,
                                item.graph_id, item.snapshot_id, item.content_hash,
                                cls._encode(item.vector),
                                json.dumps(dict(item.metadata), ensure_ascii=False, sort_keys=True),
                            )
                            for item in materialized
                        ),
                    )
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def health(self) -> Dict[str, Any]:
        return {
            "backend": "embedded",
            "available": self.path.is_file(),
            "path": str(self.path),
        }

    def records(self) -> list[VectorRecord]:
        """Return every stored record; a missing or older-schema index is empty."""
        current: list[VectorRecord] = []
        if not self.path.is_file():
            return current
        try:
            with closing(sqlite3.connect(f"file:{self.path.resolve()}?mode=ro", uri=True)) as connection:
                meta = connection.execute(
                    "SELECT schema_version, dimension FROM vector_meta"
                ).fetchone()
                if meta and int(meta[0]) == self.SCHEMA_VERSION:
                    for row in connection.execute(
                        "SELECT record_id, object_type, object_id, doc_id, chunk_id, platform, service, "
                        "graph_id, snapshot_id, content_hash, vector, metadata_json FROM vectors"
                    ):
                        current.append(VectorRecord(
                            record_id=str(row[0]), object_type=str(row[1]), object_id=str(row[2]),
                            doc_id=str(row[3]), chunk_id=str(row[4]), platform=str(row[5]),
                            service=str(row[6]), graph_id=str(row[7]), snapshot_id=str(row[8]),
                            vector=self._decode(row[10], int(meta[1])), content_hash=str(row[9]),
                            metadata=json.loads(row[11] or "{}"),
                        ))
        except (OSError, sqlite3.Error, ValueError, VectorStoreError) as exc:
            raise VectorStoreError("内置向量索引不可读") from exc
        return current

    def upsert(self, records: Sequence[VectorRecord]) -> None:
        """Replace the embedded snapshot with existing rows plus ``records``."""
        merged = {item.record_id: item for item in self.records()}
        merged.update({item.record_id: item for item in records})
        self.initialize(self.path, merged.values())

    def search(
        self,
        query_vector: Sequence[float],
        filters: Mapping[str, Any],
        top_k: int = 10,
    ) -> tuple[VectorHit, ...]:
        if not self.path.is_file():
            raise VectorStoreError("内置向量索引不存在")
        try:
            with closing(sqlite3.connect(f"file:{self.path.resolve()}?mode=ro", uri=True)) as connection:
                meta = connection.execute(
                    "SELECT schema_version, dimension FROM vector_meta"
                ).fetchone()
                if not meta or meta[0] != self.SCHEMA_VERSION:
                    raise VectorStoreError("内置向量索引版本不匹配")
                dimension = int(meta[1])
                if len(query_vector) != dimension:
                    raise VectorStoreError("查询向量维度不匹配")
                rows = connection.execute(
                    "SELECT record_id, object_type, object_id, doc_id, chunk_id, platform, service, "
                    "graph_id, snapshot_id, content_hash, vector, metadata_json FROM vectors"
                ).fetchall()
        except VectorStoreError:
            raise
        except sqlite3.Error as exc:
            raise VectorStoreError("内置向量索引不可读") from exc

        scored: list[VectorHit] = []
        for row in rows:
            keys = {
                "platform": row[5], "service": row[6], "graph_id": row[7],
                "snapshot_id": row[8], "object_type": row[1],
            }
            if any(value is not None and keys.get(key) != value for key, value in filters.items()):
                continue
            metadata = json.loads(row[11] or "{}")
            scored.append(VectorHit(
                record_id=str(row[0]), object_type=str(row[1]), object_id=str(row[2]),
                doc_id=str(row[3]), chunk_id=str(row[4]),
                score=round(self._cosine(query_vector, self._decode(row[10], dimension)), 6),
                metadata=metadata,
            ))
        return tuple(sorted(scored, key=lambda item: (-item.score, item.doc_id, item.chunk_id))[:max(0, int(top_k))])


class MilvusVectorStore:
    """Minimal Milvus REST adapter; optional and never the source of truth."""

    def __init__(
        self,
        endpoint: str,
        collection: str,
        *,
        token: str = "",
        vector_field: str = "vector",
        timeout_seconds: float = 10.0,
    ) -> None:
        self.endpoint = str(endpoint).rstrip("/")
        self.collection = collection
        self.token = token
        self.vector_field = vector_field
        self.timeout_seconds = float(timeout_seconds)

    def _request(self, path: str, payload: Optional[Mapping[str, Any]] = None) -> Any:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urlrequest.Request(
            f"{self.endpoint}{path}", data=body,
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
            },
            method="POST" if body is not None else "GET",
        )
        try:
            with urlrequest.urlopen(request, timeout=self.timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # urllib has several platform-specific errors
            raise VectorStoreError("Milvus 请求失败") from exc
        if isinstance(result, Mapping) and result.get("code") not in (None, 0):
            raise VectorStoreError("Milvus 返回错误")
        return result

    def health(self) -> Dict[str, Any]:
        try:
            self._request("/v2/vectordb/collections/describe", {
                "collectionName": self.collection,
            })
        except VectorStoreError:
            return {"backend": "milvus", "available": False, "collection": self.collection}
        return {"backend": "milvus", "available": True, "collection": self.collection}

    def search(
        self,
        query_vector: Sequence[float],
        filters: Mapping[str, Any],
        top_k: int = 10,
    ) -> tuple[VectorHit, ...]:
        expressions = []
        for key, value in filters.items():
            if value is None:
                continue
            if isinstance(value, (int, float)):
                expressions.append(f"{key} == {value}")
            else:
                escaped = str(value).replace('\\', '\\\\').replace('"', '\\"')
                expressions.append(f'{key} == "{escaped}"')
        result = self._request("/v2/vectordb/entities/search", {
            "collectionName": self.collection,
            "data": [list(map(float, query_vector))],
            "annsField": self.vector_field,
            "limit": max(0, int(top_k)),
            "filter": " and ".join(expressions),
            "outputFields": [
                "record_id", "object_type", "object_id", "doc_id", "chunk_id",
                "platform", "service", "graph_id", "snapshot_id", "metadata_json",
            ],
        })
        rows = result.get("data", []) if isinstance(result, Mapping) else []
        hits = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            metadata = row.get("metadata_json", {})
            if isinstance(metadata, str):
                metadata = json.loads(metadata or "{}")
            hits.append(VectorHit(
                record_id=str(row.get("record_id") or ""),
                object_type=str(row.get("object_type") or ""),
                object_id=str(row.get("object_id") or ""),
                doc_id=str(row.get("doc_id") or ""),
                chunk_id=str(row.get("chunk_id") or ""),
                score=float(row.get("distance") or row.get("score") or 0.0),
                metadata=dict(metadata or {}),
            ))
        return tuple(hits)

    def upsert(self, records: Sequence[VectorRecord]) -> None:
        if not records:
            return
        self._request("/v2/vectordb/entities/upsert", {
            "collectionName": self.collection,
            "data": [
                {
                    "record_id": item.record_id,
                    "object_type": item.object_type,
                    "object_id": item.object_id,
                    "doc_id": item.doc_id,
                    "chunk_id": item.chunk_id,
                    "platform": item.platform,
                    "service": item.service,
                    "graph_id": item.graph_id,
                    "snapshot_id": item.snapshot_id,
                    "content_hash": item.content_hash,
                    self.vector_field: list(map(float, item.vector)),
                    "metadata_json": json.dumps(dict(item.metadata or {}), ensure_ascii=False, sort_keys=True),
                }
                for item in records
            ],
        })
