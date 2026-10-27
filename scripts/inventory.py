"""资源清单调试:show 概览 / resolve 别名归一 / export 导出 inventory/v1。stdout 纯 JSON,不写文件。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _common import REPO_ROOT, emit_err, emit_ok
from lib.envpack import EnvPackError
from lib.inventory import load_inventory


def run(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    show = sub.add_parser("show", help="集群与资源计数;--cluster 列出该集群主机")
    show.add_argument("--cluster", help="集群旧短键")
    resolve = sub.add_parser("resolve", help="节点键/IP/别名/FQDN 归一到主机资源 ID")
    resolve.add_argument("alias")
    resolve.add_argument("--cluster", help="限定集群旧短键;不给则要求跨集群唯一命中")
    sub.add_parser("export", help="完整导出 inventory/v1")
    a = ap.parse_args(argv)

    try:
        inventory = load_inventory(REPO_ROOT)
    except (EnvPackError, OSError, ValueError) as exc:
        return emit_err("config_error", str(exc))

    if a.command == "export":
        return emit_ok(inventory=inventory.to_dict())
    if a.command == "resolve":
        try:
            host = inventory.resolve_host(a.alias, a.cluster)
        except OSError as exc:
            return emit_err("source_error", str(exc))
        if host is None:
            return emit_err("not_found", f"未能唯一归一: {a.alias}")
        key, node = inventory.legacy(host.ref)
        return emit_ok(id=host.id, platform=key, node=node)
    if a.cluster:
        try:
            hosts = inventory.hosts(a.cluster)
        except (OSError, ValueError) as exc:
            return emit_err("bad_cluster", str(exc))
        return emit_ok(cluster=inventory.require_cluster(a.cluster).id, hosts=[
            {"id": host.id, "address": host.attrs.get("address"),
             "aliases": list(host.attrs.get("aliases") or ())}
            for host in hosts
        ])
    clusters = []
    for cluster in inventory.clusters():
        rows = [item for item in inventory.resources() if item.id.startswith(cluster.id + "/")]
        clusters.append({
            "id": cluster.id,
            "legacy_key": cluster.attrs.get("legacy_key"),
            "display_name": cluster.attrs.get("display_name"),
            "roles": list(cluster.attrs.get("roles") or ()),
            "counts": {kind: sum(1 for item in rows if item.kind == kind)
                       for kind in ("host", "service", "component", "instance")},
            "source_error": inventory.source_errors.get(cluster.id),
        })
    return emit_ok(env=inventory.env.id, clusters=clusters, warnings=list(inventory.warnings))


if __name__ == "__main__":
    sys.exit(run())
