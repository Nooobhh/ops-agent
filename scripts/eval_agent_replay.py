#!/usr/bin/env python
"""Agent 回放评估:录制环境结果重放历史排查任务,度量迁移前后的行为退化。

子命令(stdout 均为纯 JSON):
  build-cases  按清单从主仓历史任务冻结用例与 state fixtures(对源仓只读)
  prepare      钉知识库 commit 快照并重建当前 checkout 的 build/ 索引
  run          并行回放用例,写 runs/<label>/report.{json,md} 与 --report 指定的最新报告
  compare      两份报告对比(候选 vs 基线)
  run-case     单例回放(由 run 以子进程调用)
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


def _path(value):
    return Path(value).expanduser().resolve() if value else None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Agent replay evaluation")
    from lib.data_root import resolve_data_root

    build_dir = resolve_data_root(ROOT) / "build"
    parser.add_argument("--out", default=str(build_dir / "agent-replay"), help="用例、fixtures、快照与运行产物目录")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build-cases")
    build.add_argument("--source-repo", required=True, help="历史任务所在仓库(只读其 state/ 与 records/)")
    build.add_argument("--manifest", default=None, help="默认 <out>/manifest.yaml")

    prep = sub.add_parser("prepare")
    prep.add_argument("--kb-repo", default=None, help="默认按 config/paths.local.yaml 解析")
    prep.add_argument("--kb-commit", default=None, help="默认取 fixtures 记录的清单 knowledge_commit")

    run = sub.add_parser("run")
    run.add_argument("--cases", default="smoke", help="smoke | all | 逗号分隔用例 id")
    run.add_argument("--repeat", type=int, default=1)
    run.add_argument("--jobs", type=int, default=2)
    run.add_argument("--label", default=None)
    run.add_argument("--model-config", default=str(DEFAULT_MODEL_CONFIG))
    run.add_argument("--signature-map", default=None)
    run.add_argument("--script-run", default=None, help="A0:重放该标签运行第 1 轮录下的模型响应")
    run.add_argument("--baseline", default=None, help="基线 report.json,给出则附对比判定")
    run.add_argument("--report", default=str(build_dir / "agent-replay-eval"),
                     help="最新报告前缀,写 .json 与 .md")

    cmp_parser = sub.add_parser("compare")
    cmp_parser.add_argument("baseline")
    cmp_parser.add_argument("candidate")

    single = sub.add_parser("run-case")
    single.add_argument("--case", required=True)
    single.add_argument("--out-dir", required=True)
    single.add_argument("--fixtures", required=True)
    single.add_argument("--model-config", required=True)
    single.add_argument("--signature-map", default=None)
    single.add_argument("--script", default=None)

    args = parser.parse_args(argv)
    out = _path(args.out)
    try:
        if args.command == "build-cases":
            from lib.agent_replay.runner import build_cases

            manifest = _path(args.manifest) or out / "manifest.yaml"
            result = build_cases(manifest, _path(args.source_repo), out)
        elif args.command == "prepare":
            from lib.agent_replay.runner import prepare

            result = prepare(ROOT, out, kb_repo=_path(args.kb_repo), kb_commit=args.kb_commit)
        elif args.command == "run":
            from lib.agent_replay.runner import git_info, run_suite

            mode = "a0" if args.script_run else "model"
            label = args.label or f"{mode}-{git_info(ROOT)['commit']}-{datetime.now():%Y%m%d-%H%M%S}"
            report = run_suite(
                repo_root=ROOT, out=out, script=Path(__file__).resolve(),
                selector=args.cases, repeat=max(1, args.repeat), jobs=args.jobs,
                label=label, model_config=_path(args.model_config),
                signature_map=_path(args.signature_map), script_run=args.script_run,
                baseline=_path(args.baseline), report_prefix=_path(args.report),
            )
            result = {
                "ok": not report["summary"]["hard_failures"],
                "label": label,
                "report": str(out / "runs" / label / "report.json"),
                "summary": report["summary"],
                "comparison": report["comparison"],
            }
        elif args.command == "compare":
            from lib.agent_replay.metrics import compare, split_runs

            baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
            candidate = json.loads(Path(args.candidate).read_text(encoding="utf-8"))
            result = compare(split_runs(baseline), split_runs(candidate))
        else:
            from lib.network_env import configure_direct_network

            # 与 Web/worker 启动同口径:清代理,模型网关与内网一律直连
            configure_direct_network()
            from lib.agent_replay.cases import load_case
            from lib.agent_replay.driver import ReplayDriver
            from lib.agent_replay.recordings import SignatureMap

            script = (
                json.loads(Path(args.script).read_text(encoding="utf-8"))
                if args.script else None
            )
            driver = ReplayDriver(
                load_case(Path(args.case)),
                repo_root=ROOT,
                out_dir=Path(args.out_dir),
                fixtures_dir=Path(args.fixtures),
                model_config=Path(args.model_config),
                signature_map=SignatureMap.load(_path(args.signature_map)),
                script=script,
            )
            outcome = driver.run()
            result = {"ok": not outcome["metrics"]["hard_failures"], "case_id": outcome["case_id"],
                      "status": outcome["metrics"]["status"], "conclusion": outcome["metrics"]["conclusion"]}
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
