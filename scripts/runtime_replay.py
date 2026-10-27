#!/usr/bin/env python
"""新运行时零成本回放:用 A1 精简组转录驱动 lib/agent_runtime,不连集群、不调模型。

stdout 为纯 JSON;任务与 records 落在 --work(缺省临时目录),知识库与检索索引只读。
用法:scripts/runtime_replay.py <transcript.jsonl>... [--mode whitelist|auto|bypass]
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv=None) -> int:
    from lib.agent_replay.lean_replay import replay_case
    from lib.triage_agent.paths import AppPaths

    parser = argparse.ArgumentParser(description="replay A1 lean transcripts through the new runtime")
    parser.add_argument("transcripts", nargs="+")
    parser.add_argument("--mode", choices=["whitelist", "auto", "bypass"], default="whitelist")
    parser.add_argument("--work", default=None, help="任务与 records 输出目录,缺省临时目录")
    args = parser.parse_args(argv)
    paths = AppPaths.from_repo_root(ROOT)
    work_root = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="runtime-replay-"))
    cases = []
    for index, name in enumerate(args.transcripts):
        cases.append(replay_case(
            Path(name), work_root / f"case-{index}", repo_root=ROOT,
            recall_index=paths.recall_index_path, knowledge_root=paths.knowledge_root,
            command_mode=args.mode,
        ))
    print(json.dumps({"ok": True, "work": str(work_root), "mode": args.mode,
                      "cases": cases}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
