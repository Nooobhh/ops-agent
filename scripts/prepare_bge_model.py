#!/usr/bin/env python
"""Download and save the optional local BGE embedding model."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="BAAI/bge-small-zh-v1.5")
    parser.add_argument("--output", default="models/bge-small-zh-v1.5")
    args = parser.parse_args()
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise SystemExit(
            "未安装本地 embedding 依赖，请先执行: "
            ".venv/bin/pip install -r requirements-vector.txt"
        ) from exc
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    model = SentenceTransformer(args.model, device="cpu")
    model.save(str(output))
    print(f"BGE 模型已保存到 {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
