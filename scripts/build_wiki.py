"""重建 routing、拓扑图、召回索引、ContextPack 快照和 build/wiki.html。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from lib.kb.rebuild import rebuild


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    # 不依赖 cwd:knowledge/ 与 build/ 由 rebuild 按数据根定位
    ap.add_argument("--repo-root", default=str(REPO_ROOT))
    a = ap.parse_args()
    out = rebuild(Path(a.repo_root))
    print(f"wiki 写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
