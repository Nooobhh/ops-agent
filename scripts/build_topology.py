"""从平台配置和知识库编译全量运维拓扑图。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.data_root import resolve_data_root
from lib.envpack import load_env_pack
from lib.kb.bases import resolve_knowledge_root
from lib.kb.topology import TopologyError, build_topology, write_topology
from lib.kb.topology_view import write_topology_html


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--config-root", default=None)
    parser.add_argument("--knowledge-root", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--html-out", default=None)
    parser.add_argument(
        "--snapshot-root",
        default=None,
        help="脱敏集群拓扑快照目录；缺省使用环境包 config/topology/（存在时）",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    config_root = Path(args.config_root).resolve() if args.config_root else load_env_pack(repo_root).config_dir
    knowledge_root = (
        Path(args.knowledge_root).resolve()
        if args.knowledge_root
        else resolve_knowledge_root(repo_root)
    )
    output = (
        Path(args.out).resolve() if args.out
        else resolve_data_root(repo_root) / "build" / "kb-topology.json"
    )
    html_output = (
        Path(args.html_out).resolve()
        if args.html_out
        else output.with_suffix(".html")
    )
    try:
        snapshot_root = Path(
            args.snapshot_root or config_root / "topology"
        ).resolve()
        graph = build_topology(config_root, knowledge_root, snapshot_root)
        write_topology(graph, output)
        write_topology_html(graph, html_output)
    except TopologyError as exc:
        print(f"拓扑构建失败: {exc}", file=sys.stderr)
        return 1

    print(json.dumps({
        "ok": True,
        "output": str(output),
        "html_output": str(html_output),
        "stats": graph["stats"],
        "warnings": graph["warnings"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
