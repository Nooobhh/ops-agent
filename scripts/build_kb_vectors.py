#!/usr/bin/env python
"""Embed retrieval chunks into the configured embedded store or Milvus.

默认增量:只重编码内容或模型变化的段并剔除已删除的段(统一 rebuild 也会调);
--full 全量重编码,远端 Milvus 只能走全量。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.kb.retrieval.embedding import EmbeddingError
from lib.kb.retrieval.vector_store import VectorStoreError
from lib.kb.retrieval.vector_sync import sync_vectors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--full", action="store_true", help="忽略已有向量全量重编码")
    args = parser.parse_args()
    try:
        result = sync_vectors(Path(args.repo_root), full=args.full, batch_size=args.batch_size)
    except (EmbeddingError, VectorStoreError) as exc:
        raise SystemExit(str(exc)) from exc
    if result["status"] == "disabled":
        raise SystemExit("未配置 embedding provider，请在 config/vector.local.yaml 开启 local 或 external")
    print(json.dumps({"ok": result["status"] == "ok", **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
