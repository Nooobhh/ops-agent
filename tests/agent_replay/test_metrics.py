import copy

from lib.agent_replay.metrics import (
    case_metrics,
    compare,
    conclusion_of,
    skeleton,
    skeleton_divergence,
    summarize,
)
from lib.agent_replay.report import render_markdown

CASE = {
    "case_id": "c1",
    "expect": {"task_type": "investigation", "conclusion": ["conclusive"]},
    "labels": {"key_evidence": ["k1", "k2"]},
}


def ev(event_type, **payload):
    return {"type": event_type, "payload": payload}


def metrics(task=None, events=(), invocations=None, session=None, lookups=(), driver_error=None):
    return case_metrics(
        CASE,
        task={"status": "completed", "task_type": "investigation", "tool_turns": 4, **(task or {})},
        events=list(events),
        invocations=invocations or {},
        session=session,
        lookups=list(lookups),
        usage={"requests": 2, "prompt_tokens": 10},
        kb_attempts=0,
        driver_error=driver_error,
        wall_seconds=1.0,
    )


def test_conclusion_prefers_verdict_then_hypothesis_then_answer():
    assert conclusion_of({}, [], {"verdict": "insufficient"}) == "inconclusive"
    assert conclusion_of({"current_hypothesis": {"status": "supported"}}, [], None) == "conclusive"
    answered = [ev("agent_decision", action="needs_input", answer="结论")]
    assert conclusion_of({}, answered, None) == "answered"
    assert conclusion_of({}, [ev("agent_decision", action="needs_input", answer="")], None) == "none"


def test_case_metrics_counts_replay_evidence_and_rejections():
    result = metrics(
        events=[
            ev("tool_call", name="run_diagnostic"),
            ev("tool_result", name="run_diagnostic", result={"ok": True}),
            ev("tool_call", name="run_diagnostic"),
            ev("tool_result", name="run_diagnostic", result={"error": "未登记", "failed": True}),
            ev("tool_call", name="read_kb_document"),
            ev("tool_result", name="read_kb_document",
               result={"tool_result": {"ok": False, "outcome": "skipped"}}),
            ev("harness_feedback"),
            ev("approval_requested", kind="budget"),
        ],
        session={"verdict": "conclusive", "evidence": ["a", "b"]},
        lookups=[
            {"tool": "run_diagnostic", "match": "exact", "recording": "k1"},
            {"tool": "run_command", "match": "miss", "recording": None},
            {"tool": "read_resource_snapshot", "match": "fallback", "recording": None},
        ],
    )

    assert result["conclusion"] == "conclusive" and result["conclusion_ok"] is True
    assert result["evidence"]["key_recall"] == 0.5
    assert result["evidence"]["cited"] == 2
    assert result["tools"]["env_calls"] == 2 and result["tools"]["tool_calls"] == 3
    assert result["replay"]["hit_rate"] == 0.5
    assert result["rejections"] == {"tool": 1, "decision": 1, "chain_fail": False, "model_retries": 0}
    assert result["gates"] == {"budget": 1}
    assert result["hard_failures"] == []


def test_hard_failures_separate_harness_bugs_from_model_and_budget():
    crashed = metrics(task={"status": "failed", "error": "KeyError: 'x'"})
    assert crashed["hard_failures"] == ["harness_error"]
    exhausted = metrics(task={"status": "kb_failed", "failure_kind": "turn_cap"})
    assert exhausted["hard_failures"] == []
    model_down = metrics(task={"status": "failed", "error": "模型请求失败: HTTP 429"})
    assert model_down["hard_failures"] == [] and model_down["model_unavailable"] is True
    live = metrics(task={"status": "failed", "error": "回放模式禁止访问真实集群与数据库"})
    assert live["hard_failures"] == ["live_access"]
    unclosed = metrics(invocations={"c1": {"status": "pending"}})
    assert unclosed["hard_failures"] == ["unclosed_invocations"]
    assert metrics(driver_error="max_steps")["hard_failures"] == ["driver_error"]


def test_skeleton_ignores_ids_and_reports_first_divergence():
    events = [
        ev("status", status="running"),
        ev("tool_call", name="run_command", tool_call_id="call_random"),
        ev("tool_result", name="run_command", result={"ok": True}),
        ev("history_compacted"),
    ]
    items = skeleton(events)
    assert items == ["status:running", "call:run_command", "result:run_command:ok"]
    changed = [*items[:2], "result:run_command:fail"]
    assert skeleton_divergence(items, changed) == {
        "index": 2, "expected": "result:run_command:ok", "actual": "result:run_command:fail",
    }
    assert skeleton_divergence(items, items[:2])["actual"] is None
    assert skeleton_divergence(items, items) is None


def run(case_id, *, conclusion="conclusive", calls=10, turns=5, rejections=0, hit=1.0, key=1.0, hard=()):
    return {
        "case_id": case_id, "conclusion": conclusion, "conclusion_ok": True, "model_unavailable": False,
        "task_type": "qa", "task_type_expected": "qa",
        "evidence": {"key_recall": key},
        "tools": {"tool_calls": calls, "model_turns": turns},
        "rejections": {"tool": rejections, "decision": 0},
        "replay": {"exact": int(hit * 10), "fuzzy": 0, "miss": 10 - int(hit * 10), "hit_rate": hit},
        "usage": {"prompt_tokens": 1000, "completion_tokens": 10, "cached_tokens": 0, "requests": 5},
        "hard_failures": list(hard),
    }


BASELINE = {
    case_id: [run(case_id), run(case_id, calls=12)]
    for case_id in ("a", "b", "c")
}


def test_compare_passes_within_baseline_envelope():
    candidate = {case_id: [run(case_id, calls=11)] for case_id in BASELINE}
    result = compare(BASELINE, candidate)
    assert result["verdict"] == "pass"
    assert result["baseline"]["tool_calls_per_round"] == 33.0


def test_compare_flags_hard_and_soft_regressions():
    candidate = copy.deepcopy({case_id: [run(case_id)] for case_id in BASELINE})
    candidate["a"] = [run("a", conclusion="none")]
    candidate["b"] = [run("b", conclusion="inconclusive", calls=40)]
    result = compare(BASELINE, candidate)
    assert result["verdict"] == "regressed"
    rules = {item["rule"] for item in result["soft_regressions"]}
    assert rules == {"conclusion", "tool_calls"}
    assert result["conclusion_regressed_cases"] == ["a", "b"]

    candidate["c"] = [run("c", hard=["harness_error"])]
    assert compare(BASELINE, candidate)["verdict"] == "fail"


def test_low_fidelity_cases_do_not_gate_soft_rules():
    baseline = {**BASELINE, "low": [run("low", hit=0.2), run("low", hit=0.3)]}
    candidate = {case_id: [run(case_id)] for case_id in BASELINE}
    candidate["low"] = [run("low", conclusion="none", calls=99, hit=0.2)]
    result = compare(baseline, candidate)
    assert result["low_fidelity_cases"] == ["low"]
    assert result["verdict"] == "pass"


def test_summary_and_markdown_render():
    report = {
        "label": "t", "mode": "model",
        "git": {"branch": "b", "commit": "c", "dirty": False},
        "knowledge": {"commit": "k"}, "model": {"models": ["m"], "efforts": ["high"]},
        "created_at": "now",
        "cases": [{
            "case_id": "a",
            "runs": [{
                **run("a"), "status": "completed", "failure_kind": None, "error": None,
                "evidence": {"key_recall": 1.0, "key_hit": 1, "key_total": 1},
                "replay": {"exact": 1, "fuzzy": 0, "miss": 0, "hit_rate": 1.0},
            }],
            "a0": [None],
        }],
    }
    report["summary"] = summarize({"a": report["cases"][0]["runs"]})
    report["comparison"] = compare(BASELINE, {case_id: [run(case_id)] for case_id in BASELINE})
    text = render_markdown(report)
    assert "# Agent 回放评估 — t" in text
    assert "| a | 1 | qa/qa | completed | conclusive | 1/1 |" in text
    assert "判定:**pass**" in text
