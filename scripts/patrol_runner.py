"""巡检单次执行入口:对节点列表跑同一 playbook,汇总 snapshot.json + summary.md。

v0.1 只做单次执行(手动或 cron 调),不做异常转告警(v0.2)。
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from lib.data_root import ambient_data_root, local_config_path, records_root_for
from lib.envpack import load_env_pack
from lib.playbook import patrol_node_healthy
from lib.runner import run_playbook, PlaybookTimeout, _RUN_ID_RE


@dataclass
class PatrolResult:
    snapshot_path: Path
    summary_path: Path


def resolve_run_id(run_id: str, playbook: Path, *, platform_path: Path,
                   now: datetime | None = None) -> str:
    if run_id != "auto":
        return run_id
    now = now or datetime.now()
    stem = Path(playbook).stem
    if stem.startswith("patrol_"):
        stem = stem[len("patrol_"):]
    platform = _platform_from_platform_path(platform_path)
    return f"patrol-{now.strftime('%Y%m%d-%H%M')}-{platform}-{stem}"


def run_patrol_once(*, pool, nodes: List[str], playbook: Path, records_root: Path,
                    run_id: str, platform: str) -> PatrolResult:
    d = Path(records_root) / run_id
    d.mkdir(parents=True, exist_ok=True)
    per_node: Dict[str, Any] = {}

    for node in nodes:
        try:
            r = run_playbook(pool, node, playbook, run_id=run_id)
            per_node[node] = {
                "verdict": r.verdict, "severity": r.severity,
                "conclusion_hint": r.conclusion_hint,
                "steps": [{"name": s.name, "pass": s.pass_,
                           "actual": s.actual, "expect": s.expect}
                          for s in r.steps],
                "metrics": r.metrics,
            }
        except Exception as e:
            per_node[node] = {"error": f"{type(e).__name__}: {e}"}

    snap = {
        "run_id": run_id,
        "platform": platform,
        "playbook": str(playbook),
        "nodes": per_node,
    }
    snap_path = d / "snapshot.json"
    snap_path.write_text(
        json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    summary_path = d / "summary.md"
    lines = [f"# 巡检报告 · {run_id}\n", f"- playbook: `{playbook}`",
             f"- 节点数: {len(nodes)}\n"]
    for node, r in per_node.items():
        if "error" in r:
            lines.append(f"- **{node}** ⚠ {r['error']}")
        else:
            mark = "✓" if patrol_node_healthy(r) else "✗"
            lines.append(f"- {mark} **{node}** verdict={r['verdict']} severity={r['severity']}")
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return PatrolResult(snapshot_path=snap_path, summary_path=summary_path)


def _platform_from_platform_path(path: Path) -> str:
    stem = Path(path).stem
    if stem.startswith("platform-"):
        return stem[len("platform-"):]
    return stem


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--playbook", required=True, help="patrol playbook 路径")
    ap.add_argument("--platform", help="平台主机清单;缺省取环境包 config/platform-hd.yaml")
    ap.add_argument("--local-dev", default=str(local_config_path(REPO_ROOT, "local-dev.yaml")))
    ap.add_argument("--run-id", required=True, help="run_id,如 patrol-YYYYMMDD-HHMM")
    ap.add_argument("--nodes", help="逗号分隔节点名;不填则用 platform 里全部")
    a = ap.parse_args()
    a.platform = a.platform or str(load_env_pack(REPO_ROOT).config_file("platform-hd.yaml"))
    if a.playbook.startswith("playbooks/"):  # 知识库登记的引用写法,解析到环境包
        a.playbook = str(load_env_pack(REPO_ROOT).playbook_path(a.playbook))

    platform = _platform_from_platform_path(Path(a.platform))
    a.run_id = resolve_run_id(a.run_id, Path(a.playbook), platform_path=Path(a.platform))
    if not _RUN_ID_RE.fullmatch(a.run_id):
        raise SystemExit(f"run_id 只允许 [A-Za-z0-9_.-],得到 {a.run_id!r}")

    from lib.cmd_filter import CmdFilter
    from lib.config import load_local_dev, load_platform
    from lib.credentials import FileCredentials
    from lib.ssh_pool import SSHPool

    plat = load_platform(Path(a.platform))
    dev = load_local_dev(Path(a.local_dev))
    creds = FileCredentials(dev.credentials_file.expanduser(), merge_home=True) if dev \
        else FileCredentials(local_config_path(REPO_ROOT, "credentials.json"), merge_home=True)
    pool = SSHPool(plat, creds, CmdFilter.default(),
                   jump=dev.jump if dev else None)

    nodes = a.nodes.split(",") if a.nodes else list(plat.nodes.keys())
    result = run_patrol_once(
        pool=pool, nodes=nodes, playbook=Path(a.playbook),
        records_root=records_root_for(ambient_data_root()) / "patrol", run_id=a.run_id,
        platform=platform,
    )
    try:
        from lib.worklog import append_from_patrol
        append_from_patrol(result.snapshot_path)
    except Exception as e:
        print(f"WARN worklog auto-emit 失败: {e}", file=sys.stderr)
    print(f"snapshot: {result.snapshot_path}")
    print(f"summary:  {result.summary_path}")
    pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
