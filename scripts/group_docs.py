#!/usr/bin/env python
"""分组库任务的确定性知识组装:通用文档 → 任务类型通用文档 → pid/tid 特化文档(stdout JSON)。

库与图谱由环境包 knowledge.assembly 声明;任务类型按 pid/tid 从图谱快照解析,也可用 --task-type
直接指定;只读检索快照。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.kb.retrieval.context import RetrievalService, RetrievalSnapshotError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-p", "--pid", type=int, default=None, help="流程号 PROCESS_ID")
    parser.add_argument("-t", "--tid", type=int, default=None, help="任务号 TASK_ID")
    parser.add_argument("--task-type", default=None, help="直接指定任务类型,优先于图谱解析")
    parser.add_argument("--repo-root", default=str(ROOT))
    args = parser.parse_args()
    if args.pid is None and args.tid is None and not args.task_type:
        parser.error("至少给出 --pid、--tid 或 --task-type 之一")
    try:
        payload = RetrievalService(Path(args.repo_root)).task_route(
            process_id=args.pid, task_id=args.tid, task_type=args.task_type,
        )
    except RetrievalSnapshotError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, **payload}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
