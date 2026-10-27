#!/usr/bin/env python
"""图谱任务的数据依赖:读写了哪些表、同表的上游写入者与下游读取者、流程级依赖(stdout JSON)。

图谱由环境包 knowledge.assembly 声明;只读检索快照中的图谱,结果是定义推导的参考信息,不是运行证据;
是否真并发要再经插件比对运行时间。
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
    parser.add_argument("-p", "--pid", type=int, required=True, help="流程号 PROCESS_ID")
    parser.add_argument("-t", "--tid", type=int, default=None, help="任务号 TASK_ID,缺省列出流程内全部任务")
    parser.add_argument("--repo-root", default=str(ROOT))
    args = parser.parse_args()
    try:
        payload = RetrievalService(Path(args.repo_root)).task_dependencies(process_id=args.pid, task_id=args.tid)
    except RetrievalSnapshotError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, **payload}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
