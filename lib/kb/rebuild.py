"""知识库派生产物统一重建入口。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from lib.data_root import resolve_data_root
from lib.envpack import load_env_pack

from .bases import resolve_knowledge_root
from .recall import build_recall_index
from .index_sync import sync_indexes
from .routing import build_routing
from .retrieval import build_retrieval_index
from .retrieval.embedding import EmbeddingError
from .retrieval.settings import VectorSettingsError
from .retrieval.vector_store import VectorStoreError
from .retrieval.vector_sync import sync_vectors
from .topology import build_topology_artifacts
from .wiki import render_wiki


def rebuild(repo_root: Path) -> Path:
    """重建 routing、wiki、拓扑图、召回/检索索引与向量索引，返回 wiki 路径。

    向量索引可选:未配置 embedding 时跳过;同步失败只告警并保留旧索引,
    不让知识写入因向量层回滚。
    """
    repo_root = Path(repo_root)
    kb_root = resolve_knowledge_root(repo_root)
    sync_indexes(kb_root, load_env_pack(repo_root).config_dir)
    idx = build_routing(kb_root)
    build_dir = resolve_data_root(repo_root) / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / "routing.json").write_text(
        json.dumps(idx, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8")
    out = build_dir / "wiki.html"
    out.write_text(render_wiki(idx, kb_root, load_env_pack(repo_root).knowledge_platforms()), encoding="utf-8")
    build_topology_artifacts(
        repo_root,
        output=build_dir / "kb-topology.json",
        html_output=build_dir / "kb-topology.html",
    )
    build_recall_index(kb_root, build_dir / "kb-recall.sqlite")
    build_retrieval_index(repo_root, build_dir / "kb-retrieval.sqlite")
    try:
        sync_vectors(repo_root)
    except (EmbeddingError, VectorSettingsError, VectorStoreError) as exc:
        print(f"[rebuild] 向量索引未更新,沿用旧索引: {exc}", file=sys.stderr)
    return out
