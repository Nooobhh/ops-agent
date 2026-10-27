#!/usr/bin/env python
"""Download and save an optional local Cross-Encoder rerank model."""
from __future__ import annotations

import argparse
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="BAAI/bge-reranker-v2-m3")
    parser.add_argument("--output", default="models/bge-reranker-v2-m3")
    parser.add_argument("--max-length", type=int, default=512)
    args = parser.parse_args()
    try:
        from sentence_transformers import CrossEncoder
    except ImportError as exc:
        raise SystemExit(
            "未安装本地 rerank 依赖，请先执行: "
            ".venv/bin/pip install -r requirements-vector.txt"
        ) from exc
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    model = CrossEncoder(args.model, device="cpu", max_length=args.max_length)
    model.save(str(output))
    print(f"Cross-Encoder 模型已保存到 {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
