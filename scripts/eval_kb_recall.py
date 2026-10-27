#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.data_root import ambient_data_root
from lib.kb.bases import resolve_knowledge_root
from lib.kb.recall_eval import (
    evaluate,
    load_corpus,
    production_schemes,
    load_search_documents,
    render_markdown,
    report_to_dict,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate local knowledge recall")
    parser.add_argument("--corpus", default=None, help="默认 <知识库根>/_eval/recall-corpus.yaml")
    parser.add_argument("--knowledge-root", default=None, help="默认按 config/paths.local.yaml 解析")
    parser.add_argument("--repo-root", default=str(ROOT), help="线上索引所在的 ops-agent 仓库根")
    parser.add_argument("--json-report", default=str(ambient_data_root() / "build" / "kb-recall-eval.json"))
    parser.add_argument("--markdown-report", default=str(ambient_data_root() / "build" / "kb-recall-eval.md"))
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    knowledge_root = Path(args.knowledge_root) if args.knowledge_root else resolve_knowledge_root(repo_root)
    corpus_path = Path(args.corpus) if args.corpus else knowledge_root / "_eval" / "recall-corpus.yaml"
    corpus = load_corpus(corpus_path, knowledge_root)
    docs = load_search_documents(knowledge_root)
    report = evaluate(
        corpus, docs, knowledge_root,
        production_schemes(repo_root),
    )

    json_path = Path(args.json_report)
    markdown_path = Path(args.markdown_report)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(report_to_dict(report), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": True,
                "preliminary": report.preliminary,
                "cases": report.case_count,
                "json_report": str(json_path),
                "markdown_report": str(markdown_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
