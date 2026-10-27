"""采集一张环境包图谱(extensions.graphs 声明的采集器),写入 <数据根>/state/graphs/<id>/。stdout JSON。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.data_root import resolve_data_root
from lib.envpack import load_env_pack
from lib.graphs import GraphManager, graph_decls


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", required=True, help="图谱 id(环境包 extensions.graphs)")
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--state-dir", default="state")
    parser.add_argument("--mode", choices=("full", "incremental"), default="full")
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root)
    state_dir = Path(args.state_dir)
    if not state_dir.is_absolute():
        state_dir = resolve_data_root(repo_root) / state_dir
    try:
        pack = load_env_pack(repo_root)
        decl = next((item for item in graph_decls(pack) if item.id == args.graph), None)
        if decl is None:
            raise ValueError(f"图谱 {args.graph!r} 未在环境包 extensions.graphs 声明")
        manager = GraphManager(repo_root, state_dir, decl, pack)
        try:
            graph = manager.run_sync(args.mode)
        finally:
            manager.stop()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({
        "ok": True,
        "graph": args.graph,
        "mode": graph.get("update_mode"),
        "generated_at": graph.get("generated_at"),
        "completed_at": graph.get("completed_at"),
        "source_fingerprint": graph.get("source_fingerprint"),
        "stats": graph.get("stats"),
        "change_summary": graph.get("change_summary"),
        "warnings": graph.get("warnings"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
