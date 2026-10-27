#!/usr/bin/env python
"""Build a read-only ContextPack from the local retrieval snapshot."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.envpack import KNOWLEDGE_ENV_SCOPE, active_env_pack
from lib.kb.retrieval import RetrievalRequest, build_context


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--profile", default="default")
    parser.add_argument("--platform", choices=[key for key in active_env_pack().knowledge_platforms() if key != KNOWLEDGE_ENV_SCOPE], default=None)
    parser.add_argument("--scope", default=None)
    parser.add_argument("--service", default=None)
    parser.add_argument("--component", default=None)
    parser.add_argument("--node", default=None)
    parser.add_argument("--alertname", default=None)
    parser.add_argument("--artifact-kind", default=None)
    parser.add_argument("--retrieval-stage", default="initial")
    parser.add_argument("--fallback-count", type=int, default=0)
    parser.add_argument("--query-vector", default=None, help="JSON array; optional")
    args = parser.parse_args()
    vector = None
    if args.query_vector:
        value = json.loads(args.query_vector)
        if not isinstance(value, list):
            raise SystemExit("--query-vector 必须是 JSON 数组")
        vector = tuple(float(item) for item in value)
    pack = build_context(Path(args.repo_root), RetrievalRequest(
        query=args.query,
        profile=args.profile,
        platform=args.platform,
        scope=args.scope,
        service=args.service,
        component=args.component,
        node=args.node,
        alertname=args.alertname,
        artifact_kind=args.artifact_kind,
        retrieval_stage=args.retrieval_stage,
        fallback_count=max(0, args.fallback_count),
        query_vector=vector,
    ))
    print(json.dumps(pack.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
