"""生成 build/routing.json:从 knowledge/ frontmatter 编译。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from lib.data_root import ambient_data_root
from lib.kb.bases import resolve_knowledge_root
from lib.kb.routing import build_routing


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb-root", default=None, help="缺省按 resolve_knowledge_root 解析")
    ap.add_argument("--out", default=str(ambient_data_root() / "build" / "routing.json"))
    a = ap.parse_args()
    idx = build_routing(Path(a.kb_root) if a.kb_root else resolve_knowledge_root(REPO_ROOT))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(idx, ensure_ascii=False, indent=2, sort_keys=True),
                   encoding="utf-8")
    alert_count = sum(len(entries) for entries in idx["alerts"].values())
    print(f"routing 索引写入 {out}: {alert_count} alerts, {len(idx['docs'])} docs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
