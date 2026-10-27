"""回放评估的编排:冻结用例、钉知识库、并行跑用例子进程、汇总报告。"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from lib.data_root import resolve_data_root
from lib.envpack import env_timezone
from lib.kb.bases import resolve_knowledge_root
from lib.triage_agent.model_registry import ModelRegistry

from .cases import build_case, freeze_state_fixtures, load_manifest, select_cases
from .metrics import compare, split_runs, summarize
from .report import render_markdown

REPORT_SCHEMA_VERSION = 1
CASE_TIMEOUT_SECONDS = 2 * 3600
GRAPH_FIXTURE_GLOB = "graphs/*/graph-latest.json"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


def git_info(repo_root: Path) -> Dict[str, Any]:
    try:
        return {
            "commit": _git(repo_root, "rev-parse", "--short=12", "HEAD"),
            "branch": _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(_git(repo_root, "status", "--porcelain", "--untracked-files=no")),
        }
    except (OSError, subprocess.CalledProcessError):
        return {"commit": None, "branch": None, "dirty": None}


def build_cases(manifest_path: Path, source_repo: Path, out: Path) -> Dict[str, Any]:
    """按清单从主仓历史任务冻结用例;对源仓 state/records 只读。"""
    manifest = load_manifest(manifest_path)
    cases_dir = out / "cases"
    if cases_dir.exists():
        shutil.rmtree(cases_dir)
    cases_dir.mkdir(parents=True)
    built = []
    for entry in manifest.get("cases") or []:
        case = build_case(entry, source_repo)
        (cases_dir / f"{case['case_id']}.json").write_text(
            json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        built.append({
            "case_id": case["case_id"],
            "recordings": len(case["recordings"]),
            "key_evidence": len(case["labels"]["key_evidence"]),
            "smoke": case["smoke"],
        })
    fixtures = out / "fixtures"
    if fixtures.exists():
        shutil.rmtree(fixtures)
    copied = freeze_state_fixtures(resolve_data_root(Path(source_repo)) / "state", fixtures)
    meta = {
        "source_repo": str(Path(source_repo).resolve()),
        "built_at": datetime.now(env_timezone()).isoformat(),
        "knowledge_commit": manifest.get("knowledge_commit"),
        "state_files": copied,
    }
    (fixtures / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "cases": built, "fixtures": copied, "knowledge_commit": meta["knowledge_commit"]}


def prepare(repo_root: Path, out: Path, *, kb_repo: Optional[Path] = None, kb_commit: Optional[str] = None) -> Dict[str, Any]:
    """钉知识库快照并重建当前 checkout 的 build/ 索引;对知识库仓只读。"""
    meta = json.loads((out / "fixtures" / "meta.json").read_text(encoding="utf-8"))
    kb_repo = Path(kb_repo) if kb_repo else resolve_knowledge_root(repo_root)
    commit = _git(kb_repo, "rev-parse", kb_commit or meta.get("knowledge_commit") or "HEAD")
    kb_root = out / "kb" / commit[:12]
    if not kb_root.is_dir():
        archive = subprocess.run(
            ["git", "-C", str(kb_repo), "archive", "--format=tar", commit],
            check=True, capture_output=True,
        ).stdout
        kb_root.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(kb_root, filter="data")
    # 检索索引构建时把 <数据根>/state/graphs 的图谱写进去(ContextPack 的分组组装与
    # read_task_dependencies 都读索引),所以冻结图谱必须在 build_wiki 之前装好
    graph_status = {}
    fixture_state = out / "fixtures" / "state"
    for source in sorted(fixture_state.glob(GRAPH_FIXTURE_GLOB)):
        relative = source.relative_to(fixture_state).as_posix()
        target = resolve_data_root(repo_root) / "state" / relative
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            graph_status[relative] = "installed"
        else:
            same = target.read_bytes() == source.read_bytes()
            graph_status[relative] = "present" if same else "mismatch"
    env = {**os.environ, "OPS_AGENT_KNOWLEDGE_ROOT": str(kb_root.resolve())}
    rebuild = subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "build_wiki.py"), "--repo-root", str(repo_root)],
        env=env, capture_output=True, text=True,
    )
    return {
        "ok": rebuild.returncode == 0,
        "knowledge_commit": commit[:12],
        "knowledge_root": str(kb_root),
        "build_wiki": {"returncode": rebuild.returncode, "stderr": rebuild.stderr[-1000:]},
        "graph_fixtures": graph_status,
    }


def _model_meta(model_config: Path) -> Dict[str, Any]:
    config = ModelRegistry(model_config).load()
    models = {item.id: item.model for item in config.models}
    used = [policy.model_id for policy in config.agent_policies.values()]
    efforts = sorted({policy.reasoning_effort for policy in config.agent_policies.values()})
    if config.defaults is not None:
        used.append(config.defaults.manual.model_id)
        efforts = sorted({*efforts, config.defaults.manual.reasoning_effort})
    return {
        "config": str(model_config),
        "models": sorted({models.get(item, item) for item in used}),
        "efforts": efforts,
    }


def _run_case_process(
    script: Path,
    case_path: Path,
    out_dir: Path,
    *,
    fixtures: Path,
    model_config: Path,
    signature_map: Optional[Path],
    script_path: Optional[Path],
    env: Dict[str, str],
) -> Dict[str, Any]:
    command = [
        sys.executable, str(script), "run-case",
        "--case", str(case_path), "--out-dir", str(out_dir),
        "--fixtures", str(fixtures), "--model-config", str(model_config),
    ]
    if signature_map:
        command += ["--signature-map", str(signature_map)]
    if script_path:
        command += ["--script", str(script_path)]
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "run-case.log").open("w", encoding="utf-8") as log:
        try:
            subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=CASE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            pass
    result_path = out_dir / "result.json"
    if result_path.is_file():
        return json.loads(result_path.read_text(encoding="utf-8"))
    return {"case_id": case_path.stem, "metrics": None, "a0": None, "process_error": True}


def _failed_metrics(case_id: str) -> Dict[str, Any]:
    # 子进程没写出 result.json(崩溃/超时):按硬失败计入,不让整轮静默少一例
    return {
        "case_id": case_id, "status": "", "failure_kind": None, "error": "回放子进程未产出结果",
        "model_unavailable": False, "task_type": None, "task_type_expected": None, "agent_path": [],
        "conclusion": "none", "conclusion_ok": None,
        "evidence": {"cited": 0, "gate_rejections": 0, "key_total": 0, "key_hit": 0, "key_recall": None},
        "tools": {"model_turns": 0, "tool_calls": 0, "env_calls": 0, "by_tool": {}},
        "replay": {"exact": 0, "fuzzy": 0, "miss": 0, "local": 0, "hit_rate": None},
        "rejections": {"tool": 0, "decision": 0, "chain_fail": False, "model_retries": 0},
        "gates": {}, "unclosed_invocations": 0, "kb_write_attempts": 0, "usage": {},
        "wall_seconds": 0.0, "hard_failures": ["process_error"],
    }


def run_suite(
    *,
    repo_root: Path,
    out: Path,
    script: Path,
    selector: str,
    repeat: int,
    jobs: int,
    label: str,
    model_config: Path,
    signature_map: Optional[Path] = None,
    script_run: Optional[str] = None,
    baseline: Optional[Path] = None,
    report_prefix: Optional[Path] = None,
) -> Dict[str, Any]:
    cases = select_cases(out / "cases", selector)
    run_root = out / "runs" / label
    if run_root.exists():
        raise FileExistsError(f"运行标签已存在: {run_root}")
    meta = json.loads((out / "fixtures" / "meta.json").read_text(encoding="utf-8"))
    commit = str(meta.get("knowledge_commit") or "")
    kb_dirs = sorted((out / "kb").glob(f"{commit[:12]}*")) if commit else []
    if not kb_dirs:
        raise FileNotFoundError("知识库快照未准备:先跑 prepare")
    kb_root = kb_dirs[0].resolve()
    env = {**os.environ, "OPS_AGENT_KNOWLEDGE_ROOT": str(kb_root)}
    mode = "recorded" if script_run else "model"
    plan = []
    for case in cases:
        case_path = out / "cases" / f"{case['case_id']}.json"
        for index in range(1, repeat + 1):
            run_dir = run_root / case["case_id"] / f"r{index}"
            script_path = None
            if script_run:
                source = out / "runs" / script_run / case["case_id"] / "r1"
                responses = json.loads((source / "model-responses.json").read_text(encoding="utf-8"))
                responses["skeleton"] = json.loads((source / "result.json").read_text(encoding="utf-8"))["skeleton"]
                run_dir.mkdir(parents=True, exist_ok=True)
                script_path = run_dir / "script.json"
                script_path.write_text(json.dumps(responses, ensure_ascii=False), encoding="utf-8")
            plan.append((case, index, case_path, run_dir, script_path))
    started = datetime.now(env_timezone())
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futures = [
            pool.submit(
                _run_case_process, script, case_path, run_dir,
                fixtures=out / "fixtures", model_config=model_config,
                signature_map=signature_map, script_path=script_path, env=env,
            )
            for _case, _index, case_path, run_dir, script_path in plan
        ]
        results = [future.result() for future in futures]
    by_case: Dict[str, Dict[str, Any]] = {}
    for (case, _index, *_rest), result in zip(plan, results):
        item = by_case.setdefault(case["case_id"], {
            "case_id": case["case_id"], "smoke": case.get("smoke"),
            "expect": case.get("expect"), "runs": [], "a0": [],
        })
        item["runs"].append(result.get("metrics") or _failed_metrics(case["case_id"]))
        item["a0"].append(result.get("a0"))
    runs_by_case = {case_id: item["runs"] for case_id, item in by_case.items()}
    report: Dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "label": label,
        "mode": mode,
        "script_run": script_run,
        "selector": selector,
        "repeat": repeat,
        "created_at": started.isoformat(),
        "finished_at": datetime.now(env_timezone()).isoformat(),
        "git": git_info(repo_root),
        "knowledge": {"commit": kb_root.name, "root": str(kb_root)},
        "model": _model_meta(model_config),
        "cases": list(by_case.values()),
        "summary": summarize(runs_by_case),
        "comparison": None,
    }
    if baseline is not None:
        base = json.loads(Path(baseline).read_text(encoding="utf-8"))
        report["baseline_label"] = base.get("label")
        report["comparison"] = compare(split_runs(base), runs_by_case)
        if base.get("knowledge", {}).get("commit") != report["knowledge"]["commit"]:
            report["comparison"]["info"].append({"rule": "knowledge_commit_mismatch"})
        if base.get("model", {}).get("models") != report["model"]["models"]:
            report["comparison"]["info"].append({"rule": "model_mismatch"})
    run_root.mkdir(parents=True, exist_ok=True)
    write_report(report, run_root / "report")
    if report_prefix is not None:
        write_report(report, report_prefix)
    return report


def write_report(report: Dict[str, Any], prefix: Path) -> None:
    prefix.parent.mkdir(parents=True, exist_ok=True)
    Path(f"{prefix}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(f"{prefix}.md").write_text(render_markdown(report), encoding="utf-8")

