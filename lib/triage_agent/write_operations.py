"""人工批准的写操作:插件两阶段写(计划 → 审批 → Harness 签令牌执行 → 跟踪)与 SSH 写命令的落档。

插件写的状态全在事件流,worker 重启后据此重建,不重复下发:

    approval_requested(kind=operation) → approval_decided(decided_by)
    → operation_executing(写前日志)→ operation_executed(插件返回)
    → operation_submitted(拿到请求号)→ operation_progress*(轮询)→ operation_finished

令牌只在网关 ``execute_plugin_operation`` 内存在;本模块只见令牌摘要。跟踪期间任务处于
``waiting_operation``,由 worker 空闲时 ``poll_due`` 轮询,终态后转 queued 交回运行时收尾。
执行动作(oplog、records、报告、给模型的结果)只追加,已发出的消息不改。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from lib.envpack import env_timezone
from lib.kb.frontmatter import SERVICES
from lib.sanitize import redact_secrets

from .approvals import new_approval_id, request_approval
from .errors import ActionDenied, ApprovalPending
from .models import TaskStatus, TriageTask

KIND = "operation"
POLL_FAST_SECONDS = 10
POLL_SLOW_SECONDS = 30
FAST_WINDOW_SECONDS = 120
DEFAULT_MAX_DURATION_SECONDS = 1200
DEADLINE_MARGIN_SECONDS = 300
# 批准时计划剩余有效期不足即重新预检(签令牌、调用、插件复查都要时间)
EXPIRY_MARGIN_SECONDS = 20
RECONCILE_GRACE_SECONDS = 60
MAX_EXECUTE_ATTEMPTS = 3
MAX_REPLANS = 2
RECONCILE_LIMIT = 50
PLAN_TEXT_LIMIT = 40000
LOG_TAIL_CHARS = 4000
FAILED_TASK_STATES = {"FAILED", "ABORTED", "TIMEDOUT", "SKIPPED_FAILED"}
# 有写动作发生(或可能发生)的结论:都要 oplog
WRITE_OUTCOMES = {"succeeded", "partial", "failed", "unverified", "unknown"}
OUTCOME_LABELS = {
    "succeeded": "成功", "partial": "部分成功", "failed": "失败", "unverified": "已完成未复核",
    "unknown": "结果未知", "noop": "无需执行", "not_executed": "未执行",
}
_ACTION_LABELS = {"start": "启动", "stop": "停止", "restart": "重启"}
_NOT_EXECUTED_HINTS = {
    "PLAN_STALE": "批准后目标状态已变化,插件拒绝执行;如仍需操作请重新提出计划",
    "APPROVAL_INVALID": "插件拒绝了审批令牌(多半是中台与插件的审批密钥不一致,请在插件页核对)",
    "SERVICE_OPERATION_CONFLICT": "目标上有进行中的操作,插件拒绝执行",
    "PLUGIN_UNAVAILABLE": "插件不可用,请求没有发出",
    "PLUGIN_AUTH_FAILED": "插件认证失败,请求没有执行",
    "PLUGIN_DISABLED": "插件或计划工具已在插件页停用,未执行",
    "APPROVAL_SECRET_MISSING": "插件未配置审批密钥,未执行",
}


@dataclass
class Operation:
    """事件流重建出的一次写操作(以最近一次 approval_requested 为准)。"""
    request: Dict[str, Any]
    decision: Optional[Dict[str, Any]] = None
    executing: List[Dict[str, Any]] = field(default_factory=list)
    executed: List[Dict[str, Any]] = field(default_factory=list)
    submitted: Optional[Dict[str, Any]] = None
    progress: List[Dict[str, Any]] = field(default_factory=list)
    finished: Optional[Dict[str, Any]] = None
    replans: int = 0
    call_settled: bool = False

    @property
    def approval_id(self) -> str:
        return str(self.request.get("approval_id") or "")

    @property
    def final_progress(self) -> Optional[Dict[str, Any]]:
        last = self.progress[-1] if self.progress else None
        return last if last and (last.get("final") or last.get("timed_out")) else None

    @property
    def approved_steps(self) -> List[str]:
        return [str(item["ref"]) for item in self.executed if item.get("approved") and item.get("ref")]


def load_operation(store, task_id: str) -> Optional[Operation]:
    op: Optional[Operation] = None
    replans = 0
    settled = set()
    for event in store.events(task_id):
        payload = event.payload
        if event.type == "tool_result":
            settled.add(str(payload.get("tool_call_id") or ""))
            continue
        if event.type == "approval_requested":
            if payload.get("kind") == KIND:
                replans = replans + 1 if payload.get("replanned_from") else 0
                op = Operation(request=payload, replans=replans)
            continue
        if op is None or payload.get("approval_id") != op.approval_id:
            continue
        if event.type == "approval_decided":
            op.decision = {**payload, "decided_at": _local_iso(event.ts)}
        elif event.type == "operation_executing":
            op.executing.append(payload)
        elif event.type == "operation_executed":
            op.executed.append(payload)
        elif event.type == "operation_submitted":
            op.submitted = payload
        elif event.type == "operation_progress":
            op.progress.append(payload)
        elif event.type == "operation_finished":
            op.finished = payload
    if op is not None:
        op.call_settled = str(op.request.get("tool_call_id") or "") in settled
    return op


def classify(progress: Dict[str, Any]) -> Tuple[str, str]:
    """跟踪终态 → (outcome, 一句话结论)。字段为插件写协议的跟踪结果:status/final/tasks/verification。"""
    request_id = progress.get("request_id")
    if progress.get("timed_out"):
        return "unknown", f"超过预期最长时长仍未结束,Harness 停止跟踪;请求 {request_id} 可能仍在执行,请到后台操作列表核对"
    data = _data(progress.get("result"))
    status = str(data.get("status") or "")
    tasks = [item for item in data.get("tasks") or [] if isinstance(item, dict)]
    failed = [item for item in tasks if str(item.get("status") or "") in FAILED_TASK_STATES]
    verification = data.get("verification") if isinstance(data.get("verification"), dict) else None
    if status == "COMPLETED":
        if verification is None:
            return "unverified", "请求已完成,插件未给出复核结果(可能重启过),请用只读工具确认目标状态"
        if verification.get("succeeded"):
            return "succeeded", "请求已完成,复核目标状态一致"
        mismatched = verification.get("mismatched") or []
        return "partial", f"请求已完成,但 {len(mismatched)} 个实例复核状态不符"
    done = [item for item in tasks if str(item.get("status") or "") == "COMPLETED"]
    if failed and done:
        return "partial", f"请求结束为 {status},{len(failed)} 个任务失败、{len(done)} 个成功"
    return "failed", f"请求结束为 {status or '失败'}" + (f",{len(failed)} 个任务失败" if failed else "")


class WriteOperations:
    def __init__(
        self,
        store,
        invocations,
        actions,
        *,
        knowledge_sink=None,
        notice: Optional[Callable[[TriageTask, str], None]] = None,
        sync_report: Optional[Callable[[TriageTask], TriageTask]] = None,
        steps: Optional[Callable[[TriageTask], List[Dict[str, Any]]]] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.store = store
        self.invocations = invocations
        self.actions = actions
        self.knowledge_sink = knowledge_sink
        self._notice = notice or (lambda task, message: store.append_event(
            task.task_id, "harness_notice", {"message": message}))
        self._sync_report = sync_report or (lambda task: task)
        self._steps = steps or (lambda task: list(actions.session_data(task.run_id).get("steps") or []))
        self.clock = clock

    # ---- 提出:模型调用计划工具 ----

    def propose(self, task: TriageTask, call, spec: Dict[str, Any], plan: Dict[str, Any], ref: Optional[str]
                ) -> Dict[str, Any]:
        """计划工具已调用并落档。失败或无需执行时把结果回模型;否则挂审批(抛 ApprovalPending)。"""
        data = plan.get("data") if isinstance(plan.get("data"), dict) else {}
        if not plan.get("ok") or not data.get("plan_id") or not data.get("plan_hash"):
            return {"ref": ref, **plan}
        if data.get("noop"):
            return {"ref": ref, **plan, "approval": "not_required",
                    "note": "目标已是期望状态,无需执行,未发起审批"}
        self._request(task, call.call_id, call.name, spec, call.arguments, plan, ref)
        raise ApprovalPending()

    def _request(self, task: TriageTask, call_id: str, tool_name: str, spec: Dict[str, Any],
                 arguments: Dict[str, Any], plan: Dict[str, Any], ref: Optional[str], *,
                 replanned_from: Optional[str] = None, replan_reason: str = "") -> TriageTask:
        data = plan["data"]
        now = self.clock()
        expires_in = _number(data.get("expires_in_s"), 300)
        payload = {
            "approval_id": new_approval_id("wo"),
            "kind": KIND,
            "tool_call_id": call_id,
            "tool": tool_name,
            "plugin": spec["plugin"],
            "plan_tool": spec["tool"],
            "content_hash": spec["hash"],
            "write": dict(spec.get("write") or {}),
            "cluster": spec.get("cluster") or task.platform,
            "title": operation_title(arguments),
            "arguments": dict(arguments),
            "reason": str(arguments.get("reason") or ""),
            "plan": _bounded_json(data, PLAN_TEXT_LIMIT),
            "plan_warnings": [str(item) for item in plan.get("warnings") or []][:20],
            "plan_id": str(data["plan_id"]),
            "plan_hash": str(data["plan_hash"]),
            "plan_ref": ref,
            "created_at": _iso(now),
            "expires_at": _iso(now + expires_in),
            "expected_max_duration_s": int(_number(data.get("expected_max_duration_s"),
                                                   DEFAULT_MAX_DURATION_SECONDS)),
            "impact": self._impact(task, spec, arguments),
        }
        if replanned_from:
            payload.update({"replanned_from": replanned_from, "replan_reason": replan_reason})
        return request_approval(self.store, task.task_id, payload)

    def _impact(self, task: TriageTask, spec: Dict[str, Any], arguments: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """停止 / 重启服务时查下游(环境包声明的 impact 工具);查不到只标注,不挡审批。"""
        write = spec.get("write") or {}
        service = arguments.get("service")
        if not write.get("impact") or not service or arguments.get("action") not in {"stop", "restart"}:
            return None
        try:
            result = self.actions.run_plugin_write_tool(
                task.run_id, plugin_id=spec["plugin"], role="impact", arguments={"service": service},
            )
        except ActionDenied as exc:
            return {"ok": False, "error": str(exc)[:300]}
        focus = _data(result).get("focus") if isinstance(_data(result).get("focus"), dict) else {}
        if not result.get("ok"):
            return {"ok": False, "error": str((result.get("error") or {}).get("message") or "查询失败")[:300]}
        return {"ok": True, "service": str(focus.get("service") or service),
                "downstream": [str(item) for item in focus.get("downstream") or []][:50],
                "upstream": [str(item) for item in focus.get("upstream") or []][:50]}

    # ---- 兑现:每次认领后最先执行 ----

    def consume(self, task: TriageTask) -> Optional[TriageTask]:
        """返回任务表示本轮到此为止(挂起审批 / 进入跟踪);None 表示交回 Agent 循环。"""
        op = load_operation(self.store, task.task_id)
        if op is None or op.finished is not None:
            return None
        if op.submitted is not None:
            if op.final_progress is not None:
                return self._finalize(task, op)
            return self._wait(task, POLL_FAST_SECONDS)
        if op.executing:
            return self._recover(task, op)
        if op.decision is None:
            return None
        verdict = str(op.decision.get("decision") or ("approve" if op.decision.get("approved") else "reject"))
        if verdict != "approve":
            feedback = str(op.decision.get("feedback") or "").strip()
            label = "打回" if verdict == "revise" else "拒绝"
            reason = f"人工{label}" + (f": {feedback}" if feedback else "")
            return self._finish(task, op, "not_executed", reason, model_result={
                "approved": False, "decision": verdict, "feedback": feedback,
                "error": f"写操作被{reason}" + (";请按意见调整后重新提出" if verdict == "revise" else ""),
            })
        if not _approver(op):
            return self._finish(task, op, "not_executed", "批准记录缺少审批人,未执行")
        if _remaining(op, self.clock()) < EXPIRY_MARGIN_SECONDS:
            return self._replan(task, op, "批准时计划已过期")
        return self._attempt(task, op)

    def _attempt(self, task: TriageTask, op: Operation) -> Optional[TriageTask]:
        attempt = len(op.executing) + 1
        approver = _approver(op)
        self.store.append_event(task.task_id, "operation_executing", {
            "approval_id": op.approval_id, "attempt": attempt, "plan_id": op.request["plan_id"],
            "approver": approver,
        })
        before = len(self._steps(task))
        try:
            result = self.actions.execute_plugin_operation(
                task.run_id, plugin_id=op.request["plugin"], plan_tool=op.request["plan_tool"],
                content_hash=op.request["content_hash"], plan_id=op.request["plan_id"],
                plan_hash=op.request["plan_hash"], approver=approver,
            )
        except ActionDenied as exc:
            code, message = _split_code(str(exc))
            result = {"ok": False, "data": None, "error": {"code": code, "message": message},
                      "maybe_delivered": False}
        after = len(self._steps(task))
        data = _data(result)
        error = result.get("error") or {}
        executed = {
            "approval_id": op.approval_id, "attempt": attempt, "ok": bool(result.get("ok")),
            "status": str(data.get("status") or ""), "request_id": data.get("request_id"),
            "error_code": str(error.get("code") or ""), "error": str(error.get("message") or "")[:500],
            "token_digest": result.get("token_digest"), "maybe_delivered": bool(result.get("maybe_delivered")),
            "ref": f"step:{after}" if after > before else None,
            "approved": bool(result.get("ok") and data.get("request_id")) or bool(result.get("maybe_delivered")),
        }
        self.store.append_event(task.task_id, "operation_executed", executed)
        op.executing.append({"attempt": attempt})
        op.executed.append(executed)
        return self._after_execute(task, op, executed)

    def _after_execute(self, task: TriageTask, op: Operation, executed: Dict[str, Any]) -> Optional[TriageTask]:
        if executed.get("request_id"):
            return self._submit(task, op, executed["request_id"], executed.get("token_digest"))
        if executed.get("ok"):
            return self._finish(task, op, "noop", "执行时插件判定目标已是期望状态,未下发请求")
        code = executed.get("error_code") or ""
        if code == "PLAN_EXPIRED":
            return self._replan(task, op, "插件侧计划已失效(过期或插件重启)")
        if executed.get("maybe_delivered"):
            # 请求可能已到插件:不判失败,稍后按计划标记对账
            return self._wait(task, POLL_FAST_SECONDS)
        hint = _NOT_EXECUTED_HINTS.get(code, f"插件拒绝执行({code or '未知错误'})")
        return self._finish(task, op, "not_executed", f"{hint}:{executed.get('error') or ''}".rstrip(":"))

    def _recover(self, task: TriageTask, op: Operation) -> Optional[TriageTask]:
        """调用 execute 前后中断、或请求可能已送达:先对账,再决定重发或结案,不重复下发。"""
        last = op.executed[-1] if len(op.executed) == len(op.executing) and op.executed else None
        if last is not None and not last.get("maybe_delivered"):
            return self._after_execute(task, op, last)
        now = self.clock()
        request_id, error = self._reconcile(task, op)
        if request_id is not None:
            digest = next((item.get("token_digest") for item in reversed(op.executed) if item.get("token_digest")),
                          None)
            return self._submit(task, op, request_id, digest, recovered=True)
        if error is not None:
            if now < _parse(op.request["expires_at"]) + RECONCILE_GRACE_SECONDS:
                return self._wait(task, POLL_FAST_SECONDS)
            return self._finish(task, op, "unknown", f"对账失败({error}),无法确认是否已下发;请到后台操作列表核对计划 "
                                                      f"{op.request['plan_id']}")
        if _remaining(op, now) >= EXPIRY_MARGIN_SECONDS and len(op.executing) < MAX_EXECUTE_ATTEMPTS:
            # 后台没有本计划的请求且计划仍有效:插件按 plan_id 幂等并复核状态指纹,重发不会重复下发
            return self._attempt(task, op)
        if now < _parse(op.request["expires_at"]) + RECONCILE_GRACE_SECONDS:
            return self._wait(task, POLL_FAST_SECONDS)
        return self._finish(task, op, "not_executed",
                            f"后台操作列表没有计划 {op.request['plan_id']} 的请求且计划已过期,判定未下发")

    def _reconcile(self, task: TriageTask, op: Operation) -> Tuple[Optional[Any], Optional[str]]:
        try:
            result = self.actions.run_plugin_write_tool(
                task.run_id, plugin_id=op.request["plugin"], role="reconcile",
                arguments={"limit": RECONCILE_LIMIT},
            )
        except ActionDenied as exc:
            return None, str(exc)[:200]
        if not result.get("ok"):
            return None, str((result.get("error") or {}).get("message") or "对账查询失败")[:200]
        plan_id = str(op.request["plan_id"])
        for item in _data(result).get("operations") or []:
            if isinstance(item, dict) and plan_id in str(item.get("context") or ""):
                return item.get("request_id"), None
        return None, None

    def _submit(self, task: TriageTask, op: Operation, request_id: Any, token_digest: Optional[str], *,
                recovered: bool = False) -> TriageTask:
        now = self.clock()
        deadline = now + int(op.request.get("expected_max_duration_s") or DEFAULT_MAX_DURATION_SECONDS) \
            + DEADLINE_MARGIN_SECONDS
        self.store.append_event(task.task_id, "operation_submitted", {
            "approval_id": op.approval_id, "request_id": request_id, "token_digest": token_digest,
            "submitted_at": _iso(now), "deadline": _iso(deadline), "recovered": recovered,
        })
        approver = _approver(op)
        self._deliver(task, op, {
            "approved": True, "approver": approver, "plan_id": op.request["plan_id"], "request_id": request_id,
            "status": "SUBMITTED",
            "note": "已由 Harness 下发并开始跟踪;结束后以通知告知结果,期间无需轮询",
        }, f"【写操作】{op.request.get('title')} 已由 Harness 下发(请求 {request_id}),正在跟踪,结束后通知结果")
        return self._wait(task, POLL_FAST_SECONDS)

    def _replan(self, task: TriageTask, op: Operation, reason: str) -> Optional[TriageTask]:
        """计划失效:用原参数重新预检,出一张新卡片再批;不执行没给人看过的计划。"""
        if op.replans >= MAX_REPLANS:
            return self._finish(task, op, "not_executed", f"{reason};计划已连续失效 {op.replans + 1} 次,"
                                                          "请确认插件状态后重新提出")
        self.store.append_event(task.task_id, "operation_finished", {
            "approval_id": op.approval_id, "outcome": "not_executed", "reason": f"{reason},已重新预检",
            "replanned": True,
        })
        spec = {"plugin": op.request["plugin"], "tool": op.request["plan_tool"], "hash": op.request["content_hash"],
                "write": op.request.get("write") or {}, "cluster": op.request.get("cluster")}
        before = len(self._steps(task))
        try:
            plan = self.actions.run_plugin_tool(
                task.run_id, plugin_id=spec["plugin"], tool=spec["tool"], arguments=op.request["arguments"],
                content_hash=spec["hash"],
            )
        except ActionDenied as exc:
            return self._finish(task, op, "not_executed", f"{reason};重新预检被拒:{exc}", record_event=False)
        after = len(self._steps(task))
        ref = f"step:{after}" if after > before else None
        data = _data(plan)
        if not plan.get("ok") or not data.get("plan_id") or not data.get("plan_hash"):
            error = plan.get("error") or {}
            return self._finish(task, op, "not_executed",
                                f"{reason};重新预检失败({error.get('code') or ''} {error.get('message') or ''})",
                                record_event=False, model_result={"ref": ref, **plan})
        if data.get("noop"):
            return self._finish(task, op, "noop", f"{reason};重新预检显示目标已是期望状态,无需执行",
                                record_event=False, model_result={"ref": ref, **plan, "approval": "not_required"})
        return self._request(task, op.request["tool_call_id"], op.request["tool"], spec, op.request["arguments"],
                             plan, ref, replanned_from=op.approval_id, replan_reason=reason)

    def _wait(self, task: TriageTask, delay: float) -> TriageTask:
        return self.store.transition(
            task.task_id, TaskStatus.RUNNING, TaskStatus.WAITING_OPERATION,
            lease_owner=None, lease_expires_at=None, operation_poll_at=_iso(self.clock() + delay),
        )

    # ---- 跟踪:worker 空闲时调用 ----

    def poll_due(self) -> int:
        count = 0
        for task_id in self.store.due_operations():
            try:
                self.poll(task_id)
            except Exception:  # noqa: BLE001 — 单个任务跟踪出错不挡其他任务,稍后重试
                try:
                    self.store.update(task_id, expected=TaskStatus.WAITING_OPERATION,
                                      operation_poll_at=_iso(self.clock() + POLL_SLOW_SECONDS))
                except Exception:  # noqa: BLE001
                    pass
            count += 1
        return count

    def poll(self, task_id: str) -> None:
        task = self.store.get(task_id)
        if task.status is not TaskStatus.WAITING_OPERATION:
            return
        op = load_operation(self.store, task_id)
        if op is None or op.submitted is None or op.finished is not None or op.final_progress is not None:
            # 对账、收尾都交运行时
            self._requeue(task)
            return
        now = self.clock()
        request_id = op.submitted.get("request_id")
        try:
            result = self.actions.run_plugin_write_tool(
                task.run_id, plugin_id=op.request["plugin"], role="track", arguments={"request_id": request_id},
            )
        except ActionDenied as exc:
            code, message = _split_code(str(exc))
            result = {"ok": False, "data": None, "error": {"code": code, "message": message}}
        data = _data(result)
        final = bool(result.get("ok") and data.get("final"))
        timed_out = not final and now >= _parse(op.submitted["deadline"])
        tasks = [
            {key: item.get(key) for key in ("host", "component", "command", "status", "exit_code", "duration_s")}
            for item in (data.get("tasks") or [])[:200] if isinstance(item, dict)
        ]
        progress = {
            "approval_id": op.approval_id, "request_id": request_id,
            "request_status": data.get("status"), "progress_pct": data.get("progress_pct"), "tasks": tasks,
            "final": final, "timed_out": timed_out, "polled_at": _iso(now),
            "error": None if result.get("ok") else str((result.get("error") or {}).get("message") or "")[:300],
        }
        if final or timed_out:
            last = next((item for item in reversed(op.progress) if item.get("result")), None)
            progress["result"] = _bounded_result(result) if result.get("ok") else (last or {}).get("result")
        previous = op.progress[-1] if op.progress else None
        if final or timed_out or previous is None or _signature(previous) != _signature(progress):
            self.store.append_event(task_id, "operation_progress", progress)
        if final or timed_out:
            self._requeue(task)
            return
        elapsed = now - _parse(op.submitted["submitted_at"])
        delay = POLL_FAST_SECONDS if elapsed < FAST_WINDOW_SECONDS else POLL_SLOW_SECONDS
        self.store.update(task_id, expected=TaskStatus.WAITING_OPERATION, operation_poll_at=_iso(now + delay))

    def _requeue(self, task: TriageTask) -> None:
        self.store.transition(task.task_id, TaskStatus.WAITING_OPERATION, TaskStatus.QUEUED, operation_poll_at=None)

    # ---- 收尾 ----

    def _finalize(self, task: TriageTask, op: Operation) -> None:
        final = dict(op.final_progress or {})
        outcome, summary = classify({**final, "request_id": op.submitted.get("request_id")})
        steps = [ref for ref in [op.request.get("plan_ref"), *op.approved_steps] if ref]
        if final.get("result"):
            before = len(self._steps(task))
            self.actions.record_plugin_result(
                task.run_id, plugin_id=op.request["plugin"], tool=str(op.request["write"].get("track") or ""),
                arguments={"request_id": op.submitted.get("request_id")}, result=final["result"],
            )
            after = len(self._steps(task))
            if after > before:
                steps.append(f"step:{after}")
        return self._finish(task, op, outcome, summary, final=final, steps=steps)

    def _finish(self, task: TriageTask, op: Operation, outcome: str, summary: str, *,
                final: Optional[Dict[str, Any]] = None, steps: Optional[List[str]] = None,
                model_result: Optional[Dict[str, Any]] = None, record_event: bool = True) -> None:
        submitted = op.submitted or {}
        # 插件拒绝执行时 execute 步骤不算 approved,但它是「为什么没执行」的证据,一并列出供引用
        last_ref = next((item.get("ref") for item in reversed(op.executed) if item.get("ref")), None)
        steps = steps if steps is not None else list(dict.fromkeys(
            ref for ref in [op.request.get("plan_ref"), *op.approved_steps, last_ref] if ref))
        entry = {
            "id": op.approval_id, "kind": "plugin", "title": op.request.get("title"),
            "plugin": op.request.get("plugin"), "platform": op.request.get("cluster") or task.platform,
            "arguments": op.request.get("arguments"), "reason": op.request.get("reason"),
            "plan_id": op.request.get("plan_id"), "plan_hash": op.request.get("plan_hash"),
            "approver": _approver(op) or None, "decided_at": (op.decision or {}).get("decided_at"),
            "decision": (op.decision or {}).get("decision"),
            "token_digest": submitted.get("token_digest") or next(
                (item.get("token_digest") for item in reversed(op.executed) if item.get("token_digest")), None),
            "request_id": submitted.get("request_id"), "outcome": outcome, "summary": summary, "steps": steps,
        }
        if final:
            data = _data(final.get("result"))
            entry["request_status"] = data.get("status") or final.get("request_status")
            if isinstance(data.get("verification"), dict):
                entry["verification"] = data["verification"]
        needs_oplog = outcome in WRITE_OUTCOMES or bool(op.approved_steps)
        if needs_oplog:
            oplog, error = self._write_oplog(task, entry, final)
            entry["oplog"] = oplog
            if error:
                entry["oplog_error"] = error
        if task.run_id and op.decision is not None:
            self.actions.record_write_operation(task.run_id, entry)
        if record_event:
            self.store.append_event(task.task_id, "operation_finished", {
                "approval_id": op.approval_id, "outcome": outcome, "reason": summary,
                "request_id": entry["request_id"], "oplog": entry.get("oplog"), "oplog_error": entry.get("oplog_error"),
                "steps": steps,
            })
        self._sync_report(self.store.get(task.task_id))
        label = OUTCOME_LABELS.get(outcome, outcome)
        result = model_result or {
            "approved": outcome != "not_executed", "outcome": outcome, "summary": summary,
            "plan_id": op.request.get("plan_id"), "refs": steps,
            **({"error": f"写操作{label}:{summary}"} if outcome == "not_executed" else {}),
        }
        self._deliver(task, op, result, _notice_text(op, outcome, summary, entry, final))
        return None

    def _write_oplog(self, task: TriageTask, entry: Dict[str, Any],
                     final: Optional[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
        if self.knowledge_sink is None or not task.run_id:
            return None, "知识库写入未配置"
        try:
            return self.knowledge_sink.write_oplog(task.run_id, **plugin_oplog(entry, final, task)), None
        except Exception as exc:  # noqa: BLE001 — oplog 失败不影响操作记录,finish 前重试
            return None, f"{type(exc).__name__}: {exc}"[:300]

    def _deliver(self, task: TriageTask, op: Operation, result: Dict[str, Any], text: str) -> None:
        """原调用还挂着就补它的 tool_result;已闭合(提交时或被中断接管)就追加通知。"""
        call_id = str(op.request.get("tool_call_id") or "")
        if not op.call_settled and call_id:
            self.invocations.settle(task.task_id, call_id, str(op.request.get("tool") or ""), result)
            op.call_settled = True
        else:
            self._notice(task, text)

    # ---- SSH 写命令:人工批准执行后落 oplog 与写操作记录 ----

    def record_shell_write(self, task: TriageTask, *, call_id: str, command: str,
                           results: List[Dict[str, Any]]) -> None:
        request, decision = _command_approval(self.store, task.task_id, call_id)
        nodes = [str(item.get("node")) for item in results]
        ok = [item for item in results if not item.get("error") and item.get("exit_code") == 0]
        outcome = "succeeded" if len(ok) == len(results) else "failed" if not ok else "partial"
        summary = "; ".join(
            f"{item.get('node')}: " + (f"失败({item['error'][:80]})" if item.get("error")
                                       else f"exit={item.get('exit_code')}")
            for item in results
        )
        entry = {
            "id": str((request or {}).get("approval_id") or call_id), "kind": "shell",
            "title": f"节点命令 {_clip(command, 60)}", "platform": task.platform, "command": command,
            "nodes": nodes, "approver": ((decision or {}).get("decided_by") or {}).get("name"),
            "decided_at": (decision or {}).get("decided_at"), "outcome": outcome, "summary": summary,
            "steps": [str(item["ref"]) for item in results if item.get("ref")],
        }
        oplog, error = None, "知识库写入未配置"
        if self.knowledge_sink is not None and task.run_id:
            try:
                oplog, error = self.knowledge_sink.write_oplog(task.run_id, **shell_oplog(entry, task)), None
            except Exception as exc:  # noqa: BLE001
                oplog, error = None, f"{type(exc).__name__}: {exc}"[:300]
        entry["oplog"] = oplog
        if error:
            entry["oplog_error"] = error
        self.actions.record_write_operation(task.run_id, entry)

    def retry_oplogs(self, task: TriageTask) -> None:
        """收口前补写之前失败的 oplog(仍失败则由 finish 的 oplog 闸挡住并回模型)。"""
        if self.knowledge_sink is None or not task.run_id:
            return
        session = self.actions.session_data(task.run_id)
        for entry in session.get("write_operations") or []:
            if entry.get("oplog") or not entry.get("oplog_error"):
                continue
            fields = shell_oplog(entry, task) if entry.get("kind") == "shell" else plugin_oplog(entry, None, task)
            try:
                oplog = self.knowledge_sink.write_oplog(task.run_id, **fields)
            except Exception as exc:  # noqa: BLE001
                entry = {**entry, "oplog_error": f"{type(exc).__name__}: {exc}"[:300]}
            else:
                entry = {key: value for key, value in entry.items() if key != "oplog_error"}
                entry["oplog"] = oplog
            self.actions.record_write_operation(task.run_id, entry)


# ---- 展示与 oplog 内容(只用结构化事实,不经模型) ----

def operation_title(arguments: Dict[str, Any]) -> str:
    action = str(arguments.get("action") or "")
    component, host, service = (str(arguments.get(key) or "") for key in ("component", "host", "service"))
    target = f"{component}@{host}" if component and host else component or service or host
    return _clip(f"{_ACTION_LABELS.get(action, action or '写操作')} {target}".strip(), 120)


def plugin_oplog(entry: Dict[str, Any], final: Optional[Dict[str, Any]], task: TriageTask) -> Dict[str, Any]:
    arguments = entry.get("arguments") or {}
    action = str(arguments.get("action") or "")
    outcome = str(entry.get("outcome") or "")
    label = OUTCOME_LABELS.get(outcome, outcome)
    steps = [
        f"- 动作:{entry.get('title')}(插件 `{entry.get('plugin')}`,参数 `{json.dumps(arguments, ensure_ascii=False, sort_keys=True)}`)",
        f"- 计划:`{entry.get('plan_id')}`,hash `{str(entry.get('plan_hash') or '')[:16]}`",
        f"- 理由(Agent 提出):{entry.get('reason') or '未说明'}",
        f"- 审批:{entry.get('approver') or 'n/a'} 于 {entry.get('decided_at') or 'n/a'} 批准"
        + (f",令牌摘要 `{entry['token_digest']}`" if entry.get("token_digest") else ""),
        f"- 下发:由 Harness 签发审批令牌后调用插件执行" + (f",请求 `{entry['request_id']}`" if entry.get("request_id")
                                                         else "(未取得请求号)"),
    ]
    checks = [f"- 结论:{label}。{entry.get('summary') or ''}"]
    if entry.get("request_status"):
        checks.append(f"- 请求状态:{entry['request_status']}")
    verification = entry.get("verification")
    if isinstance(verification, dict):
        mismatched = verification.get("mismatched") or []
        checks.append(f"- 插件复核:{'一致' if verification.get('succeeded') else f'{len(mismatched)} 个实例不符'}")
        for item in mismatched[:20]:
            if isinstance(item, dict):
                checks.append(f"  - {item.get('component')}@{item.get('host')}: {item.get('state')}")
    for item in _failed_tasks(final)[:20]:
        checks.append(f"- 失败任务:{item}")
    rollback = {
        "stop": "如需恢复,对同一目标重新提出「启动」计划并经人工审批后执行。",
        "start": "如需撤回,对同一目标提出「停止」计划并经人工审批后执行。",
    }.get(action, "重启没有通用回滚;如出现异常,按排查报告定位后另行提出计划并审批。")
    return _oplog_fields(entry, task, steps, checks, rollback)


def shell_oplog(entry: Dict[str, Any], task: TriageTask) -> Dict[str, Any]:
    steps = [
        f"- 命令:`{_clip(str(entry.get('command') or ''), 500)}`",
        "- 节点:" + ", ".join(f"`{node}`" for node in entry.get("nodes") or []),
        f"- 审批:{entry.get('approver') or 'n/a'} 于 {entry.get('decided_at') or 'n/a'} 批准,由 Harness 按批准时的签名执行",
    ]
    checks = [f"- 结论:{OUTCOME_LABELS.get(str(entry.get('outcome')), entry.get('outcome'))}。{entry.get('summary') or ''}"]
    rollback = "命令由人工批准后执行,无通用自动回滚;如需恢复,按排查结论另行提出并审批。"
    return _oplog_fields(entry, task, steps, checks, rollback)


def _oplog_fields(entry: Dict[str, Any], task: TriageTask, steps: List[str], checks: List[str],
                  rollback: str) -> Dict[str, Any]:
    day = datetime.now(env_timezone()).strftime("%Y-%m-%d")
    slug = re.sub(r"[^a-z0-9]+", "-", f"{entry.get('kind')}-{entry.get('title')}".lower()).strip("-")[:60].strip("-")
    ident = re.sub(r"[^a-z0-9]+", "-", str(entry.get("id") or "").lower()).strip("-")
    label = OUTCOME_LABELS.get(str(entry.get("outcome")), str(entry.get("outcome")))
    summary = _clip(f"{entry.get('approver') or '审批人'} 批准、Harness 执行的写操作:{entry.get('title')};结论:{label}。", 480)
    material = [f"- 排查记录:`records/troubleshoot/{task.run_id}/report.md`(完整输出与步骤)"]
    if entry.get("steps"):
        material.append("- 相关步骤:" + ", ".join(str(ref) for ref in entry["steps"]))
    return {
        "platform": str(entry.get("platform") or task.platform or ""),
        "service": _oplog_service(entry, task),
        "problem": f"{day}-{slug or 'write'}-{ident or 'op'}",
        "title": _clip(f"{entry.get('title')} 操作记录", 150),
        "summary": summary,
        "sections": {
            "操作步骤": "\n".join(steps),
            "验证过程": "\n".join(checks),
            "回滚": rollback,
            "补充材料": "\n".join(material),
        },
    }


def _oplog_service(entry: Dict[str, Any], task: TriageTask) -> str:
    for value in ((entry.get("arguments") or {}).get("service"), task.service):
        name = str(value or "").strip().lower()
        if name in SERVICES:
            return name
    return "system"


def _notice_text(op: Operation, outcome: str, summary: str, entry: Dict[str, Any],
                 final: Optional[Dict[str, Any]]) -> str:
    lines = [f"【写操作结果】{op.request.get('title')}:{OUTCOME_LABELS.get(outcome, outcome)}。{summary}"]
    if entry.get("request_id"):
        lines.append(f"- 请求 {entry['request_id']},状态 {entry.get('request_status') or 'n/a'}")
    failed = _failed_tasks(final)
    if failed:
        lines.append("- 失败任务:" + ";".join(failed[:10]))
    tail = _log_tail(final)
    if tail:
        lines.append(f"- 日志末尾(已脱敏):\n{tail}")
    if entry.get("steps"):
        lines.append("- 记录:" + ", ".join(entry["steps"]) + "(完整内容可 read_task_evidence 读取,可在 finish 引用)")
    if entry.get("oplog"):
        lines.append(f"- oplog:{entry['oplog']}")
    elif entry.get("oplog_error"):
        lines.append(f"- oplog 未生成({entry['oplog_error']}),收口前会再试")
    if outcome in WRITE_OUTCOMES:
        lines.append("请用只读工具确认目标现状后再收口;除非结论需要,不要重复提出同一操作。")
    return "\n".join(lines)


def _failed_tasks(final: Optional[Dict[str, Any]]) -> List[str]:
    data = _data((final or {}).get("result"))
    return [
        f"{item.get('component')}@{item.get('host')} {item.get('status')}"
        + (f"(exit {item.get('exit_code')})" if item.get("exit_code") not in (None, 0) else "")
        for item in data.get("tasks") or []
        if isinstance(item, dict) and str(item.get("status") or "") in FAILED_TASK_STATES
    ]


def _log_tail(final: Optional[Dict[str, Any]]) -> str:
    data = _data((final or {}).get("result"))
    parts = []
    for item in (data.get("logs") or [])[:3]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("stderr_tail") or item.get("stdout_tail") or "").strip()
        if text:
            parts.append(f"[{item.get('component')}@{item.get('host')}]\n{text[-LOG_TAIL_CHARS // 3:]}")
    return redact_secrets("\n".join(parts))[-LOG_TAIL_CHARS:]


def _command_approval(store, task_id: str, call_id: str):
    request = decision = None
    for event in store.events(task_id):
        payload = event.payload
        if event.type == "approval_requested" and payload.get("kind") == "command" \
                and str(payload.get("tool_call_id") or "") == call_id:
            request, decision = payload, None
        elif event.type == "approval_decided" and request is not None \
                and payload.get("approval_id") == request.get("approval_id"):
            decision = {**payload, "decided_at": _local_iso(event.ts)}
    return request, decision


def _local_iso(value: str) -> str:
    """事件时间(UTC)换成环境包时区,与报告其余时间一致。"""
    try:
        return datetime.fromisoformat(str(value)).astimezone(env_timezone()).isoformat(timespec="seconds")
    except ValueError:
        return str(value)


def _approver(op: Operation) -> str:
    decided_by = (op.decision or {}).get("decided_by") or {}
    return str(decided_by.get("name") or decided_by.get("id") or "") if isinstance(decided_by, dict) else ""


def _signature(progress: Dict[str, Any]) -> Tuple:
    return (progress.get("request_status"), progress.get("progress_pct"), progress.get("error"),
            json.dumps(progress.get("tasks"), sort_keys=True, default=str))


def _data(result: Any) -> Dict[str, Any]:
    if isinstance(result, dict) and isinstance(result.get("data"), dict):
        return result["data"]
    return {}


def _bounded_result(result: Dict[str, Any]) -> Dict[str, Any]:
    return {key: result.get(key) for key in ("plugin", "tool", "trust", "ok", "warnings", "error", "meta")} | {
        "data": _bounded_json(_data(result), PLAN_TEXT_LIMIT)}


def _bounded_json(value: Dict[str, Any], limit: int) -> Dict[str, Any]:
    """事件里只放有界的结构化内容:超长时逐级收紧字符串与列表长度,保留键结构。"""
    value = json.loads(json.dumps(value, ensure_ascii=False, default=str))
    for chars, items in ((2000, 200), (500, 50), (120, 20)):
        if len(json.dumps(value, ensure_ascii=False)) <= limit:
            break
        value = _shrink(value, chars, items)
    return value


def _shrink(value: Any, chars: int, items: int) -> Any:
    if isinstance(value, str):
        return value if len(value) <= chars else value[:chars] + "…[已截断]"
    if isinstance(value, list):
        kept = [_shrink(item, chars, items) for item in value[:items]]
        return kept + ([f"…[其余 {len(value) - items} 项已省略]"] if len(value) > items else [])
    if isinstance(value, dict):
        return {key: _shrink(item, chars, items) for key, item in value.items()}
    return value


def _split_code(message: str) -> Tuple[str, str]:
    head, sep, rest = message.partition(":")
    if sep and re.fullmatch(r"[A-Z][A-Z0-9_]+", head.strip()):
        return head.strip(), rest.strip()
    return "", message


def _remaining(op: Operation, now: float) -> float:
    return _parse(op.request.get("expires_at")) - now


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text)).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _parse(value: Any) -> float:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


__all__ = [
    "KIND", "OUTCOME_LABELS", "Operation", "WriteOperations", "classify", "load_operation", "operation_title",
    "plugin_oplog", "shell_oplog",
]
