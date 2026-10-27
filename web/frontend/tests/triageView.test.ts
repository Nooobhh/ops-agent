import assert from "node:assert/strict"
import test from "node:test"

import {
  agentDisplayLabel,
  canCreateTask,
  operationOutcomeTone,
  operationPhaseLabel,
  planRemainingSeconds,
  composerSendEnabled,
  canRetryTask,
  canSendMessage,
  failureCause,
  groupForStatus,
  pollDelay,
  runtimeControlsChanged,
  setTriageTargets,
  shouldSubmitComposer,
  statusLabel,
  shouldPromptForMissingModel,
  taskDisplayTitle,
  taskDialogDefaultKey,
  TRIAGE_TARGET_LABELS,
  triageTargetLabel,
} from "../src/lib/triageView.ts"
import type { TriageHealth, TriageTask } from "../src/types.ts"


function health(
  model: TriageHealth["model"],
  worker: TriageHealth["worker"]["state"],
): TriageHealth {
  return {
    schema_version: 1,
    model,
    can_create: model === "configured" && worker === "running",
    worker: {state: worker, pid: null},
    queue: {queued: 0, running: 0, waiting_input: 0, waiting_approval: 0},
  }
}


function task(overrides: Partial<TriageTask>): TriageTask {
  return {
    schema_version: 1,
    task_id: "tr-1",
    source: "manual",
    status: "queued",
    created_at: "2026-07-26T00:00:00Z",
    updated_at: "2026-07-26T00:00:00Z",
    initial_message: "检查",
    platform: null,
    node: null,
    alert_key: null,
    occurrence_key: null,
    alert_event: null,
    model_profile_id: null,
    model_title: null,
    model_name: null,
    reasoning_effort: null,
    run_id: null,
    matched_doc: null,
    report_path: null,
    kb_doc: null,
    deep_dive: false,
    tool_turns: 0,
    diagnostic_calls: 0,
    error: null,
    lease_owner: null,
    lease_expires_at: null,
    pending_invocation_id: null,
    ...overrides,
  }
}


test("groups workflow statuses for task-list filtering", () => {
  assert.equal(groupForStatus("running"), "active")
  assert.equal(groupForStatus("waiting_input"), "waiting")
  assert.equal(groupForStatus("waiting_approval"), "waiting")
  assert.equal(groupForStatus("completed"), "finished")
  assert.equal(groupForStatus("kb_failed"), "finished")
})


test("disables create unless configured worker is available", () => {
  assert.equal(canCreateTask(health("not_configured", "not_configured")), false)
  assert.equal(canCreateTask(health("configured", "stopped")), false)
  assert.equal(canCreateTask(health("configured", "running")), true)
})


test("waiting and failed manual tasks accept a follow-up message", () => {
  assert.equal(canSendMessage(task({source: "auto", status: "waiting_input"})), false)
  assert.equal(canSendMessage(task({source: "manual", status: "running"})), false)
  assert.equal(canSendMessage(task({source: "manual", status: "waiting_input"})), true)
  assert.equal(canSendMessage(task({source: "manual", status: "failed"})), true)
  assert.equal(canSendMessage(task({source: "manual", status: "kb_failed"})), true)
  assert.equal(canSendMessage(task({source: "manual", status: "completed"})), true)
  assert.equal(canRetryTask(task({source: "manual", status: "failed"})), true)
  assert.equal(canRetryTask(task({source: "manual", status: "kb_failed"})), true)
  assert.equal(canRetryTask(task({source: "auto", status: "failed"})), false)
})


test("polling uses bounded backoff and Chinese status labels", () => {
  assert.deepEqual([0, 1, 2, 8].map(pollDelay), [1500, 3000, 6000, 12000])
  assert.equal(statusLabel("waiting_input"), "等待补充")
  assert.equal(statusLabel("kb_failed"), "知识库失败")
})


test("task chrome uses a compact alert/project title", () => {
  assert.equal(
    taskDisplayTitle("========告警触发========\n【告警项目】：Mtail监控-JobScheduler任务执行失败告警\n【告警集群】：Alpha"),
    "Mtail监控-JobScheduler任务执行失败告警",
  )
  assert.match(taskDisplayTitle("调查 hbase_regionserver 的 GC 后 Old Gen 使用率为 95.6%，继续观察"), /^调查 hbase_regionserver 的 GC 后/)
})


test("triage targets and alert header prefixes come from the env clusters", () => {
  setTriageTargets([
    { key: "alpha", display_name: "Alpha 集群", short_name: "Alpha", roles: ["managed", "knowledge"] },
    { key: "db", display_name: "数据库集群", short_name: "DB", roles: ["mysql_direct", "knowledge"] },
    { key: "cache", display_name: "缓存集群", short_name: "Cache", roles: ["knowledge"] },
  ])
  assert.deepEqual(Object.keys(TRIAGE_TARGET_LABELS), ["alpha", "db"])
  assert.equal(triageTargetLabel("db"), "数据库集群")
  assert.equal(triageTargetLabel("cache"), "cache")
  assert.equal(taskDisplayTitle("Alpha / node1\nNameNode 堆内存偏高"), "NameNode 堆内存偏高")
  assert.equal(taskDisplayTitle("CACHE / 主从切换\n复核"), "复核")
  assert.equal(taskDisplayTitle("Other / x"), "Other / x")
})


test("composer sends on Enter and keeps Shift+Enter for a newline", () => {
  assert.equal(shouldSubmitComposer("Enter", false), true)
  assert.equal(shouldSubmitComposer("Enter", true), false)
  assert.equal(shouldSubmitComposer("a", false), false)
  assert.equal(shouldSubmitComposer("Enter", false, true), false)
})


test("keeps task dialog defaults stable across equivalent settings refreshes", () => {
  assert.equal(
    taskDialogDefaultKey({model_id: "model-02", reasoning_effort: "medium"}),
    taskDialogDefaultKey({model_id: "model-02", reasoning_effort: "medium"}),
  )
  assert.notEqual(
    taskDialogDefaultKey({model_id: "model-02", reasoning_effort: "medium"}),
    taskDialogDefaultKey({model_id: "model-01", reasoning_effort: "medium"}),
  )
})


test("settings polling keeps the composer sync key stable", () => {
  const beforePoll = {model_id: "model-02", reasoning_effort: "medium" as const}
  const afterPoll = {model_id: "model-02", reasoning_effort: "medium" as const}

  assert.equal(taskDialogDefaultKey(beforePoll), taskDialogDefaultKey(afterPoll))
})


test("new task entry prompts immediately when the global model is missing", () => {
  assert.equal(
    shouldPromptForMissingModel(health("not_configured", "running"), null),
    true,
  )
  assert.equal(
    shouldPromptForMissingModel(
      health("configured", "running"),
      {model_id: "model-02", reasoning_effort: "medium"},
    ),
    false,
  )
})


test("missing-model composer keeps send clickable for the dialog", () => {
  assert.equal(
    composerSendEnabled(null, "检查日志", false, true, false, true),
    true,
  )
  assert.equal(
    composerSendEnabled(null, "检查日志", false, true, true, false),
    true,
  )
  assert.equal(
    composerSendEnabled(null, "检查日志", false, true, false, false),
    false,
  )
})


test("runtime controls skip the Select form echo and unchanged values", () => {
  const current = task({
    status: "waiting_input",
    platform: "alpha",
    node: "node1",
    model_profile_id: "model-02",
    reasoning_effort: "medium",
    command_mode: null,
  })
  // 整页打开 ?task= 时模型选项未挂载,隐藏原生 select 回流空串——复现时实际发出的 body
  assert.equal(runtimeControlsChanged(current, {model_id: "", reasoning_effort: "medium"}), false)
  assert.equal(runtimeControlsChanged(current, {model_id: "", reasoning_effort: "high"}), false)
  assert.equal(runtimeControlsChanged(current, {platform: "alpha"}), false)
  assert.equal(runtimeControlsChanged(current, {model_id: "model-02", reasoning_effort: "medium"}), false)
  assert.equal(runtimeControlsChanged(task({platform: null}), {platform: null}), false)
  assert.equal(runtimeControlsChanged(current, {}), false)

  assert.equal(runtimeControlsChanged(current, {model_id: "model-01", reasoning_effort: "medium"}), true)
  assert.equal(runtimeControlsChanged(current, {reasoning_effort: "high"}), true)
  assert.equal(runtimeControlsChanged(current, {platform: null}), true)
  assert.equal(runtimeControlsChanged(current, {node: "node3"}), true)
  // command_mode 为 null 表示跟随全局,显式选定档位也算改动
  assert.equal(runtimeControlsChanged(current, {command_mode: "whitelist"}), true)
})


test("failure cause prefers the exhaustion kind over the stored status", () => {
  assert.equal(failureCause(task({status: "kb_failed", failure_kind: "turn_cap"})), "工具轮次耗尽")
  assert.equal(
    failureCause(task({status: "failed", failure_kind: "model_unavailable", error: "x"})),
    "模型服务连续不可用",
  )
  assert.equal(
    failureCause(task({status: "failed", error: "SSH 连接失败: 198.51.100.101\nTraceback"})),
    "SSH 连接失败: 198.51.100.101",
  )
  assert.equal(failureCause(task({status: "kb_failed", failure_kind: "knowledge", error: "x"})), null)
  assert.equal(failureCause(task({status: "completed", error: "old"})), null)
})


test("write operation tracking reads as Agent running and outcomes get tones", () => {
  assert.equal(agentDisplayLabel("waiting_operation"), "Agent运行中")
  assert.equal(statusLabel("waiting_operation"), "写操作执行中")
  assert.equal(groupForStatus("waiting_operation"), "active")
  assert.equal(operationOutcomeTone("succeeded"), "ok")
  assert.equal(operationOutcomeTone("failed"), "bad")
  assert.equal(operationOutcomeTone("unknown"), "warn")
  assert.equal(operationOutcomeTone("not_executed"), "muted")
  const base = {phase: "submitted", requestId: 3301, requestStatus: "IN_PROGRESS", progressPct: 40, tasks: [],
    outcome: null, reason: "", oplog: null, oplogError: null, replanned: false, tokenDigest: null} as const
  assert.equal(operationPhaseLabel({...base}), "已下发,跟踪中 · 请求 3301 · IN_PROGRESS 40%")
  assert.equal(operationPhaseLabel({...base, phase: "finished", outcome: "partial"}), "部分成功")
  assert.equal(operationPhaseLabel({...base, phase: "finished", outcome: "not_executed", replanned: true}),
    "计划已失效,已重新预检")
  const now = Date.parse("2026-10-22T10:00:00Z")
  assert.equal(planRemainingSeconds("2026-10-22T10:04:30+00:00", now), 270)
  assert.equal(planRemainingSeconds("2026-10-22T09:59:00+00:00", now), null)
  assert.equal(planRemainingSeconds("bad", now), null)
})
