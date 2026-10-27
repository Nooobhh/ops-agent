"""回放报告的 Markdown 渲染(JSON 报告是真源,这里只做人读视图)。"""
from __future__ import annotations

from typing import Any, Dict, List


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _run_row(case_id: str, index: int, run: Dict[str, Any]) -> str:
    replay = run["replay"]
    usage = run.get("usage") or {}
    evidence = run["evidence"]
    task_type = f"{_fmt(run['task_type'])}/{_fmt(run['task_type_expected'])}"
    status = run["status"] + (f"({run['failure_kind']})" if run.get("failure_kind") else "")
    key = (
        f"{evidence['key_hit']}/{evidence['key_total']}" if evidence["key_total"] else "-"
    )
    return (
        f"| {case_id} | {index} | {task_type} | {status} | {run['conclusion']} | {key} "
        f"| {run['tools']['tool_calls']}/{run['tools']['model_turns']} "
        f"| {replay['exact']}/{replay['fuzzy']}/{replay['miss']} "
        f"| {run['rejections']['tool']}+{run['rejections']['decision']} "
        f"| {usage.get('prompt_tokens', 0)}/{usage.get('completion_tokens', 0)} "
        f"| {', '.join(run['hard_failures']) or '-'} |"
    )


def render_markdown(report: Dict[str, Any]) -> str:
    summary = report["summary"]
    lines: List[str] = [
        f"# Agent 回放评估 — {report['label']}",
        "",
        f"- 模式:{report['mode']}(model = 真模型 + 录制环境;recorded = A0 录制模型重放)",
        f"- 代码:{report['git'].get('branch')} @ {report['git'].get('commit')}"
        + ("(工作区有未提交改动)" if report["git"].get("dirty") else ""),
        f"- 知识库:{report['knowledge'].get('commit') or '未钉住'}",
        f"- 模型:{', '.join(report['model'].get('models') or []) or '-'}"
        f"(思考强度 {', '.join(report['model'].get('efforts') or []) or '-'})",
        f"- 生成时间:{report['created_at']}",
        "",
        "## 汇总",
        "",
        f"- 用例 {summary['cases']} 个,运行 {summary['runs']} 次(模型不可用 {summary['model_unavailable_runs']} 次)",
        f"- 硬失败:{', '.join(summary['hard_failures']) or '无'}",
        f"- 结论分布:{summary['conclusions']};与期望一致率 {_fmt(summary['conclusion_ok_rate'])};"
        f"类型命中率 {_fmt(summary['task_type_match_rate'])}",
        f"- 关键证据召回均值 {_fmt(summary['key_recall_mean'])};工具调用均值 {_fmt(summary['tool_calls_mean'])};"
        f"模型轮次中位 {_fmt(summary['model_turns_median'])};驳回均值 {_fmt(summary['rejections_mean'])}",
        f"- 回放未命中率 {_fmt(summary['miss_rate'])}",
        f"- 模型请求 {summary['model_requests']} 次;输入 {summary['prompt_tokens']} tokens"
        f"(缓存 {summary['cached_tokens']}),输出 {summary['completion_tokens']} tokens",
        "",
    ]
    comparison = report.get("comparison")
    if comparison:
        lines += [
            "## 对比基线",
            "",
            f"- 基线:{report.get('baseline_label') or '-'}",
            f"- 判定:**{comparison['verdict']}**",
            f"- 硬失败:{', '.join(comparison['hard_failures']) or '无'}",
            f"- 软退化:{comparison['soft_regressions'] or '无'}",
            f"- 提示:{comparison['info'] or '无'}",
            f"- 低保真用例(基线命中率 <50%,不参与软判定):{', '.join(comparison['low_fidelity_cases']) or '无'}",
            f"- 两侧不对齐的用例:{', '.join(comparison['unmatched_cases']) or '无'}",
            "",
            "| 指标 | 基线 | 候选 |",
            "|---|---|---|",
        ]
        for key, value in comparison["baseline"].items():
            lines.append(f"| {key} | {_fmt(value)} | {_fmt(comparison['candidate'].get(key))} |")
        lines.append("")
    lines += [
        "## 逐例",
        "",
        "列:类型(实际/期望)、终态、结论、关键证据(命中/总数)、调用/轮次、"
        "回放(精确/宽松/未命中)、驳回(工具+决策)、tokens(输入/输出)、硬失败",
        "",
        "| 用例 | 轮 | 类型 | 终态 | 结论 | 关键证据 | 调用/轮次 | 回放 | 驳回 | tokens | 硬失败 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    divergences: List[str] = []
    for case in report["cases"]:
        for index, run in enumerate(case["runs"], start=1):
            lines.append(_run_row(case["case_id"], index, run))
        for index, a0 in enumerate(case.get("a0") or [], start=1):
            if a0 and a0.get("divergence"):
                item = a0["divergence"]
                divergences.append(
                    f"- {case['case_id']} 第 {index} 轮:事件 #{item['index']} 期望 `{item['expected']}`,"
                    f"实际 `{item['actual']}`"
                )
    errors = [
        f"- {case['case_id']} 第 {index} 轮:{run['error']}"
        for case in report["cases"]
        for index, run in enumerate(case["runs"], start=1)
        if run.get("error")
    ]
    if divergences:
        lines += ["", "## A0 分叉", "", *divergences]
    if errors:
        lines += ["", "## 任务错误", "", *errors]
    return "\n".join(lines) + "\n"
