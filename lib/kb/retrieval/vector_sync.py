"""Keep the vector index aligned with the retrieval chunk snapshot.

Chunks are re-embedded only when their text or the configured embedding model
changed; chunks that disappeared from the snapshot are dropped.  The embedded
store is rewritten atomically, so a failed run leaves the previous index.
"""
from __future__ import annotations

import dataclasses
import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from lib.data_root import resolve_data_root

from .settings import VectorSettings, create_embedding_provider, create_vector_store
from .vector_store import EmbeddedVectorStore, VectorRecord, VectorStoreError


def _chunk_text(row) -> str:
    return "\n".join(
        part for part in (
            str(row[4] or ""), str(row[5] or ""), str(row[3] or ""),
            str(row[6] or ""), str(row[7] or ""),
        ) if part
    )


def _load_chunks(retrieval_path: Path) -> tuple[str, list]:
    if not retrieval_path.is_file():
        raise VectorStoreError("检索快照不存在，请先运行 scripts/build_wiki.py")
    with closing(sqlite3.connect(f"file:{retrieval_path}?mode=ro", uri=True)) as connection:
        row = connection.execute(
            "SELECT value FROM retrieval_meta WHERE key='knowledge_fingerprint'"
        ).fetchone()
        rows = connection.execute(
            "SELECT chunk_id, doc_id, platform, service, section, title, summary, text, profile, confidence "
            "FROM kb_chunks ORDER BY chunk_id"
        ).fetchall()
    return str(row[0] if row else ""), rows


def sync_vectors(repo_root: Path, *, full: bool = False, batch_size: int = 32) -> dict[str, Any]:
    """Embed new or changed chunks; return a JSON-safe summary.

    Without ``full`` only the embedded store is synchronized, because a remote
    backend cannot be pruned or diffed cheaply; ``full`` re-embeds everything
    and also covers Milvus.
    """
    root = Path(repo_root).resolve()
    provider = create_embedding_provider(root)
    if provider is None:
        return {"status": "disabled"}
    model = VectorSettings.load(root).embedding_model
    store = create_vector_store(root)
    embedded = isinstance(store, EmbeddedVectorStore)
    if not embedded and not full:
        return {"status": "skipped", "reason": "remote_backend_requires_full"}
    snapshot_id, rows = _load_chunks(resolve_data_root(root) / "build" / "kb-retrieval.sqlite")

    stored = store.records() if embedded else []
    existing = {} if full else {
        item.chunk_id: item for item in stored
        if (item.metadata or {}).get("model") == model
    }
    kept: list[VectorRecord] = []
    pending: list[tuple[Any, str, str]] = []
    for row in rows:
        text = _chunk_text(row)
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        previous = existing.get(str(row[0]))
        if previous is not None and previous.content_hash == content_hash:
            kept.append(dataclasses.replace(previous, snapshot_id=snapshot_id))
        else:
            pending.append((row, text, content_hash))

    fresh: list[VectorRecord] = []
    size = max(1, min(128, int(batch_size)))
    for start in range(0, len(pending), size):
        batch = pending[start:start + size]
        vectors = provider.embed([text for _, text, _ in batch])
        for (row, _, content_hash), vector in zip(batch, vectors):
            chunk_id, doc_id, platform, service = map(str, row[:4])
            fresh.append(VectorRecord(
                record_id=hashlib.sha256(chunk_id.encode("utf-8")).hexdigest(),
                object_type="chunk", object_id=chunk_id, doc_id=doc_id,
                chunk_id=chunk_id, platform=platform, service=service,
                graph_id="knowledge", snapshot_id=snapshot_id,
                vector=tuple(vector), content_hash=content_hash,
                metadata={"section": row[4], "profile": row[8],
                          "confidence": row[9], "model": model},
            ))

    stale = {item.chunk_id for item in stored} - {str(row[0]) for row in rows}
    if embedded:
        if fresh or stale or not store.path.is_file():
            store.initialize(store.path, [*kept, *fresh])
    else:
        store.upsert(fresh)
    return {
        "status": "ok",
        "backend": store.health().get("backend"),
        "model": model,
        "records": len(kept) + len(fresh),
        "embedded": len(fresh),
        "reused": len(kept),
        "removed": len(stale),
    }
