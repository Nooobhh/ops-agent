#!/usr/bin/env python
"""评估实跑(A3.6 起):一题 × 一份模型配置,新运行时在生产集群只读实跑。

会连真实集群并消耗模型额度,必须显式加 --live;stdout 为纯 JSON。
命令档位缺省 bypass;写操作与凭据审批一律拒并记录,报告闸放行并记录(见 lib/agent_replay/live_eval.py)。
产物:<数据根>/build/a3/runs/<label>/<case>/<模型 id>/(含独立 state、records、result.json)。
同批共用一份 state 快照:<数据根>/build/a3/runs/<label>/fixtures/(首次运行时从 --source-state 拷)。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_MODEL_CONFIG = Path("~/.ops-agent/model-replay.local.yaml").expanduser()


def main(argv=None) -> int:
    from lib.data_root import resolve_data_root

    data_root = resolve_data_root(ROOT)
    build_dir = data_root / "build"
    parser = argparse.ArgumentParser(description="A3.6 new-runtime evaluation (live, read-only)")
    parser.add_argument("--case", required=True)
    parser.add_argument("--cases-file", default=str(build_dir / "a3" / "cases.yaml"))
    parser.add_argument("--model-config", default=str(DEFAULT_MODEL_CONFIG))
    parser.add_argument("--mode", default="bypass", choices=["whitelist", "auto", "bypass"])
    parser.add_argument("--label", default=None)
    parser.add_argument("--source-state", default=str(data_root / "state"),
                        help="同批 state 快照的来源(worktree 里须指向主仓 state)")
    parser.add_argument("--live", action="store_true", help="确认在生产集群只读实跑")
    args = parser.parse_args(argv)
    if not args.live:
        print(json.dumps({"ok": False, "error": "LiveNotConfirmed",
                          "message": "会连生产集群并消耗模型额度,确认后加 --live"}, ensure_ascii=False))
        return 2
    try:
        from lib.agent_replay.live_eval import EvalDriver, eval_case, load_eval_cases, model_tag, snapshot_state
        from lib.network_env import configure_direct_network

        configure_direct_network()
        case = load_eval_cases(Path(args.cases_file))[args.case]
        model_config = Path(args.model_config).expanduser()
        label = args.label or f"{datetime.now():%Y%m%d-%H%M%S}"
        batch = build_dir / "a3" / "runs" / label
        out_dir = batch / args.case / model_tag(model_config)
        if out_dir.exists():
            raise FileExistsError(f"运行目录已存在: {out_dir}")
        fixtures = snapshot_state(Path(args.source_state), batch / "fixtures")
        result = EvalDriver(
            eval_case(case), model_config=model_config, repo_root=ROOT,
            out_dir=out_dir, fixtures_dir=fixtures, command_mode=args.mode,
        ).run()
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        return 1
    runtime = result["runtime"]
    print(json.dumps({
        "ok": True, "case": args.case, "model": result.get("model"),
        "out_dir": str(out_dir), "status": result["metrics"]["status"],
        "hard_failures": result["metrics"]["hard_failures"], "outcome": runtime["outcome"],
        "verdict": runtime["verification"]["verdict"], "grounded": runtime["verification"]["grounded"],
        "violations": len(runtime["safety"]["violations"]), "turns": runtime["tools"]["turns"],
        "usage": runtime["usage"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
