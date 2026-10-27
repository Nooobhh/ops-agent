"""回放指标:单例指标、A0 事件骨架、候选 vs 基线判定。"""
from __future__ import annotations

import statistics
from collections import Counter
from typing import Any, Dict, List, Optional

from .recordings import ENV_TOOLS

TERMINAL = {"completed", "failed", "kb_failed", "cancelled"}
# 已知的耗尽/外部失败;其余 failed 视为 harness 异常
KNOWN_FAILURE_KINDS = {
    "turn_cap", "time_limit", "rejections", "diagnostic_cap", "tool_cap",
    "model_unavailable", "knowledge",
}
CONCLUSION_RANK = {"conclusive": 2, "answered": 2, "inconclusive": 1, "none": 0}
LOW_FIDELITY_HIT_RATE = 0.5
LIVE_ACCESS_MARK = "回放模式禁止访问"
SCRIPT_EXHAUSTED_MARK = "录制的模型响应已用完"


def _result_failed(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    canonical = result.get("tool_result") if isinstance(result.get("tool_result"), dict) else result
    if canonical.get("outcome") == "skipped":
        return False
    return bool(result.get("failed")) or canonical.get("ok") is False


def conclusion_of(
    task: Dict[str, Any],
    events: List[Dict[str, Any]],
    session: Optional[Dict[str, Any]],
) -> str:
    verdict = (session or {}).get("verdict")
    if verdict == "conclusive":
        return "conclusive"
    if verdict in {"insufficient", "inconclusive"}:
        return "inconclusive"
    status = (task.get("current_hypothesis") or {}).get("status")
    if status in {"supported", "refuted"}:
        return "conclusive"
    if status == "inconclusive":
        return "inconclusive"
    for event in reversed(events):
        if event.get("type") == "agent_decision":
            payload = event.get("payload") or {}
            if payload.get("action") in {"finish", "needs_input", "answer"} and str(payload.get("answer") or "").strip():
                return "answered"
            break
    return "none"


def skeleton(events: List[Dict[str, Any]]) -> List[str]:
    """A0 逐条比对的事件骨架:只留与时间/随机 ID 无关的结构信息。"""
    items: List[str] = []
    for event in events:
        kind = event.get("type")
        payload = event.get("payload") or {}
        if kind == "tool_call":
            items.append(f"call:{payload.get('name')}")
        elif kind == "tool_result":
            state = "fail" if _result_failed(payload.get("result")) else "ok"
            items.append(f"result:{payload.get('name')}:{state}")
        elif kind == "status":
            items.append(f"status:{payload.get('status')}")
        elif kind == "approval_requested":
            items.append(f"approval:{payload.get('kind')}")
        elif kind == "agent_decision":
            items.append(f"decision:{payload.get('action')}")
        elif kind == "agent_selected":
            items.append(f"agent:{payload.get('agent_id')}")
        elif kind in {"hypothesis_proposed", "hypothesis_reviewed", "agent_handoff_requested",
                      "harness_feedback", "tool_rejection_chain", "agent_playbook_result"}:
            items.append(kind)
    return items


def skeleton_divergence(expected: List[str], actual: List[str]) -> Optional[Dict[str, Any]]:
    for index, (left, right) in enumerate(zip(expected, actual)):
        if left != right:
            return {"index": index, "expected": left, "actual": right}
    if len(expected) != len(actual):
        index = min(len(expected), len(actual))
        return {
            "index": index,
            "expected": expected[index] if index < len(expected) else None,
            "actual": actual[index] if index < len(actual) else None,
        }
    return None


def case_metrics(
    case: Dict[str, Any],
    *,
    task: Dict[str, Any],
    events: List[Dict[str, Any]],
    invocations: Dict[str, Any],
    session: Optional[Dict[str, Any]],
    lookups: List[Dict[str, Any]],
    usage: Dict[str, Any],
    kb_attempts: int,
    driver_error: Optional[str],
    wall_seconds: float,
) -> Dict[str, Any]:
    calls = [event.get("payload") or {} for event in events if event.get("type") == "tool_call"]
    by_tool = Counter(str(call.get("name")) for call in calls)
    results = [event.get("payload") or {} for event in events if event.get("type") == "tool_result"]
    approvals = Counter(
        str((event.get("payload") or {}).get("kind"))
        for event in events if event.get("type") == "approval_requested"
    )
    event_counts = Counter(str(event.get("type")) for event in events)
    matches = Counter(
        item["match"] for item in lookups if item.get("tool") != "read_resource_snapshot"
    )
    served = matches["exact"] + matches["fuzzy"]
    replayable = served + matches["miss"]
    served_ids = {item.get("recording") for item in lookups if item.get("recording")}
    key_ids = list((case.get("labels") or {}).get("key_evidence") or [])
    key_hit = sum(1 for call_id in key_ids if call_id in served_ids)

    status = str(task.get("status") or "")
    error = str(task.get("error") or "")
    failure_kind = task.get("failure_kind")
    pending = sum(
        1 for item in invocations.values()
        if isinstance(item, dict) and item.get("status") == "pending"
    )
    hard: List[str] = []
    if LIVE_ACCESS_MARK in error:
        hard.append("live_access")
    model_error = error.startswith("模型请求失败") or failure_kind == "model_unavailable"
    if (
        status == "failed"
        and failure_kind not in KNOWN_FAILURE_KINDS
        and not model_error
        and SCRIPT_EXHAUSTED_MARK not in error
        and "live_access" not in hard
    ):
        hard.append("harness_error")
    if status in TERMINAL and pending:
        hard.append("unclosed_invocations")
    if driver_error:
        hard.append("driver_error")

    expected = case.get("expect") or {}
    conclusion = conclusion_of(task, events, session)
    expected_conclusions = expected.get("conclusion") or []
    evidence_cited = len((session or {}).get("evidence") or [])
    for event in reversed(events):
        if event.get("type") == "agent_decision":
            evidence_cited = max(evidence_cited, len((event.get("payload") or {}).get("evidence_refs") or []))
            break
    return {
        "case_id": case["case_id"],
        "status": status,
        "failure_kind": failure_kind,
        "error": error[:300] or None,
        "model_unavailable": model_error,
        "task_type": task.get("task_type"),
        "task_type_expected": expected.get("task_type"),
        "agent_path": [
            str((event.get("payload") or {}).get("agent_id"))
            for event in events if event.get("type") == "agent_selected"
        ] + [
            str((event.get("payload") or {}).get("to_agent"))
            for event in events if event.get("type") == "agent_handoff_requested"
        ],
        "conclusion": conclusion,
        "conclusion_ok": (conclusion in expected_conclusions) if expected_conclusions else None,
        "evidence": {
            "cited": evidence_cited,
            "gate_rejections": approvals["report_gate"] + sum(
                1 for item in results
                if item.get("name") in {"finalize_report", "amend_report"} and _result_failed(item.get("result"))
            ),
            "key_total": len(key_ids),
            "key_hit": key_hit,
            "key_recall": round(key_hit / len(key_ids), 3) if key_ids else None,
        },
        "tools": {
            "model_turns": int(task.get("tool_turns") or 0),
            "tool_calls": len(calls),
            "env_calls": sum(count for name, count in by_tool.items() if name in ENV_TOOLS),
            "by_tool": dict(sorted(by_tool.items())),
        },
        "replay": {
            "exact": matches["exact"],
            "fuzzy": matches["fuzzy"],
            "miss": matches["miss"],
            "local": matches["local"],
            "hit_rate": round(served / replayable, 3) if replayable else None,
        },
        "rejections": {
            "tool": sum(1 for item in results if _result_failed(item.get("result"))),
            "decision": event_counts["harness_feedback"],
            "chain_fail": event_counts["tool_rejection_chain"] > 0,
            "model_retries": event_counts["model_retry"],
        },
        "gates": dict(sorted(approvals.items())),
        "unclosed_invocations": pending,
        "kb_write_attempts": kb_attempts,
        "usage": usage,
        "wall_seconds": round(wall_seconds, 1),
        "hard_failures": hard,
    }


def _mean(values: List[float]) -> Optional[float]:
    return round(statistics.fmean(values), 3) if values else None


def summarize(runs_by_case: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    runs = [run for items in runs_by_case.values() for run in items]
    counted = [run for run in runs if not run.get("model_unavailable")]
    matches = Counter()
    for run in counted:
        for key in ("exact", "fuzzy", "miss"):
            matches[key] += run["replay"][key]
    replayable = matches["exact"] + matches["fuzzy"] + matches["miss"]
    return {
        "cases": len(runs_by_case),
        "runs": len(runs),
        "model_unavailable_runs": len(runs) - len(counted),
        "hard_failures": sorted({
            f"{run['case_id']}:{code}" for run in runs for code in run["hard_failures"]
        }),
        "conclusions": dict(Counter(run["conclusion"] for run in counted)),
        "conclusion_ok_rate": _mean([
            1.0 if run["conclusion_ok"] else 0.0 for run in counted if run["conclusion_ok"] is not None
        ]),
        "task_type_match_rate": _mean([
            1.0 if run["task_type"] == run["task_type_expected"] else 0.0
            for run in counted if run["task_type_expected"]
        ]),
        "key_recall_mean": _mean([
            run["evidence"]["key_recall"] for run in counted if run["evidence"]["key_recall"] is not None
        ]),
        "tool_calls_mean": _mean([run["tools"]["tool_calls"] for run in counted]),
        "model_turns_median": statistics.median([run["tools"]["model_turns"] for run in counted]) if counted else None,
        "rejections_mean": _mean([run["rejections"]["tool"] + run["rejections"]["decision"] for run in counted]),
        "miss_rate": round(matches["miss"] / replayable, 3) if replayable else None,
        "prompt_tokens": sum(int(run["usage"].get("prompt_tokens") or 0) for run in runs),
        "completion_tokens": sum(int(run["usage"].get("completion_tokens") or 0) for run in runs),
        "cached_tokens": sum(int(run["usage"].get("cached_tokens") or 0) for run in runs),
        "model_requests": sum(int(run["usage"].get("requests") or 0) for run in runs),
    }


def _per_run(runs: List[Dict[str, Any]], getter) -> List[float]:
    return [float(getter(run)) for run in runs if not run.get("model_unavailable")]


def compare(baseline: Dict[str, List[Dict[str, Any]]], candidate: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """候选 vs 基线:硬失败一票否决,软指标以基线各轮包络为界。"""
    hard = sorted({
        f"{run['case_id']}:{code}"
        for runs in candidate.values() for run in runs for code in run["hard_failures"]
    })
    low_fidelity = sorted(
        case_id for case_id, runs in baseline.items()
        if (_mean([run["replay"]["hit_rate"] for run in runs if run["replay"]["hit_rate"] is not None]) or 0.0)
        < LOW_FIDELITY_HIT_RATE
    )
    shared = sorted(set(baseline) & set(candidate))
    gated = [case_id for case_id in shared if case_id not in low_fidelity]
    conclusion_regressed: List[str] = []
    for case_id in gated:
        base = _per_run(baseline[case_id], lambda run: CONCLUSION_RANK.get(run["conclusion"], 0))
        cand = _per_run(candidate[case_id], lambda run: CONCLUSION_RANK.get(run["conclusion"], 0))
        if base and cand and max(cand) < min(base):
            conclusion_regressed.append(case_id)

    def mean_over(runs_by_case, cases, getter) -> Optional[float]:
        return _mean([value for case_id in cases for value in _per_run(runs_by_case[case_id], getter)])

    def total_per_round(runs_by_case, cases, getter) -> Optional[float]:
        # 各轮总量的均值:基线 2 轮、候选 1 轮也能直接比
        rounds: Dict[int, float] = {}
        for case_id in cases:
            for index, run in enumerate(runs_by_case[case_id]):
                if not run.get("model_unavailable"):
                    rounds[index] = rounds.get(index, 0.0) + float(getter(run))
        return _mean(list(rounds.values()))

    def key_recall(run):
        return run["evidence"]["key_recall"]

    base_key = _mean([
        key_recall(run) for case_id in gated for run in baseline[case_id]
        if key_recall(run) is not None and not run.get("model_unavailable")
    ])
    cand_key = _mean([
        key_recall(run) for case_id in gated for run in candidate[case_id]
        if key_recall(run) is not None and not run.get("model_unavailable")
    ])
    base_calls = total_per_round(baseline, gated, lambda run: run["tools"]["tool_calls"])
    cand_calls = total_per_round(candidate, gated, lambda run: run["tools"]["tool_calls"])
    base_turns = mean_over(baseline, gated, lambda run: run["tools"]["model_turns"])
    cand_turns = mean_over(candidate, gated, lambda run: run["tools"]["model_turns"])
    rejection = lambda run: run["rejections"]["tool"] + run["rejections"]["decision"]  # noqa: E731
    base_rej = total_per_round(baseline, gated, rejection)
    cand_rej = total_per_round(candidate, gated, rejection)
    base_summary = summarize({case_id: baseline[case_id] for case_id in shared})
    cand_summary = summarize({case_id: candidate[case_id] for case_id in shared})
    base_tokens = total_per_round(baseline, shared, lambda run: run["usage"].get("prompt_tokens") or 0)
    cand_tokens = total_per_round(candidate, shared, lambda run: run["usage"].get("prompt_tokens") or 0)

    soft: List[Dict[str, Any]] = []
    if len(conclusion_regressed) >= 2:
        soft.append({"rule": "conclusion", "cases": conclusion_regressed})
    if base_key is not None and cand_key is not None and cand_key < base_key - 0.15:
        soft.append({"rule": "key_recall", "baseline": base_key, "candidate": cand_key})
    if base_calls and cand_calls and cand_calls > base_calls * 1.3:
        soft.append({"rule": "tool_calls", "baseline": base_calls, "candidate": cand_calls})
    if base_turns and cand_turns and cand_turns > base_turns * 1.3:
        soft.append({"rule": "model_turns", "baseline": base_turns, "candidate": cand_turns})
    if base_rej is not None and cand_rej is not None and cand_rej > base_rej * 1.5 and cand_rej - base_rej >= 3:
        soft.append({"rule": "rejections", "baseline": base_rej, "candidate": cand_rej})
    base_miss, cand_miss = base_summary["miss_rate"], cand_summary["miss_rate"]
    if base_miss is not None and cand_miss is not None and cand_miss - base_miss > 0.10:
        soft.append({"rule": "miss_rate", "baseline": base_miss, "candidate": cand_miss})
    info: List[Dict[str, Any]] = []
    if base_tokens and cand_tokens and cand_tokens > base_tokens * 1.3:
        info.append({"rule": "prompt_tokens", "baseline": base_tokens, "candidate": cand_tokens})
    missing = sorted(set(baseline) ^ set(candidate))
    return {
        "verdict": "fail" if hard else ("regressed" if soft else "pass"),
        "hard_failures": hard,
        "soft_regressions": soft,
        "info": info,
        "low_fidelity_cases": low_fidelity,
        "conclusion_regressed_cases": conclusion_regressed,
        "unmatched_cases": missing,
        "baseline": {
            "key_recall_mean": base_key, "tool_calls_per_round": base_calls,
            "model_turns_mean": base_turns, "rejections_per_round": base_rej,
            "miss_rate": base_miss, "prompt_tokens_per_round": base_tokens,
        },
        "candidate": {
            "key_recall_mean": cand_key, "tool_calls_per_round": cand_calls,
            "model_turns_mean": cand_turns, "rejections_per_round": cand_rej,
            "miss_rate": cand_miss, "prompt_tokens_per_round": cand_tokens,
        },
    }


def split_runs(report: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    return {item["case_id"]: list(item.get("runs") or []) for item in report.get("cases") or []}
