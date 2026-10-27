#!/usr/bin/env python
"""知识库 1.0 格式迁移:platform 改资源引用、正文 schema 升 v3(缺省 dry-run,--apply 才写)。

--knowledge-root 必须显式给出,不读环境变量与 paths.local,避免误写真知识库;
只改文件,不重建索引、不做 git 操作——之后自行跑 `python -m lib.kb` 与 build_wiki。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.kb.migrate_v1 import apply_migration, plan_migration


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--knowledge-root", required=True, help="知识库根(含各库 kb.yaml 的目录)")
    parser.add_argument("--apply", action="store_true", help="写入变更;缺省只列清单")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出清单")
    args = parser.parse_args(argv)

    root = Path(args.knowledge_root).expanduser().resolve()
    if not root.is_dir():
        parser.error(f"知识库根不存在: {root}")
    plan = plan_migration(root)
    if args.json:
        print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
    else:
        for item in plan.changes:
            print(f"{item.path}: " + "; ".join(f"{name} {old} → {new}" for name, old, new in item.fields))
        for path, reason in plan.skipped:
            print(f"跳过 {path}: {reason}")
        for path, reason in plan.errors:
            print(f"错误 {path}: {reason}", file=sys.stderr)
        summary = plan.to_dict()["summary"]
        print(f"变更 {summary['changed']} 个文件,跳过 {summary['skipped']},错误 {summary['errors']}"
              + ("" if args.apply else "(dry-run,未写入)"))
    if plan.errors:
        return 1
    if args.apply:
        apply_migration(root, plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
