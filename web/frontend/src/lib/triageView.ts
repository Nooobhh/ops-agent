import type {
  EnvCluster,
  OperationExecution,
  OperationOutcome,
  RuntimeControlUpdates,
  TriageHealth,
  TriageTask,
  TriageTaskStatus,
  TriageTarget,
  TaskType,
  WorkflowModelDefault,
} from "../types.ts"


export type TaskGroup = "active" | "waiting" | "finished"
export type AgentDisplayPhase = "active" | "can_input" | "approval" | "finished"


const STATUS_LABELS: Record<TriageTaskStatus, string> = {
  queued: "排队中",
  running: "运行中",
  waiting_input: "等待补充",
  waiting_approval: "等待审批",
  waiting_operation: "写操作执行中",
  completed: "已完成",
  failed: "失败",
  kb_failed: "知识库失败",
  cancelled: "已取消",
}


export function groupForStatus(status: TriageTaskStatus): TaskGroup {
  if (status === "waiting_input" || status === "waiting_approval") {
    return "waiting"
  }
  if (
    status === "completed"
    || status === "failed"
    || status === "kb_failed"
    || status === "cancelled"
  ) {
    return "finished"
  }
  return "active"
}


export function canCreateTask(health: TriageHealth | null): boolean {
  return Boolean(
    health?.can_create
    && health.model === "configured"
    && health.worker.state === "running",
  )
}


export function canSendMessage(task: TriageTask | null): boolean {
  return Boolean(
    task?.source === "manual"
    && (
      task.status === "waiting_input"
      || task.status === "failed"
      || task.status === "kb_failed"
      || task.status === "completed"
    ),
  )
}


export function canRetryTask(task: TriageTask | null): boolean {
  return Boolean(
    task?.source === "manual"
    && (task.status === "failed" || task.status === "kb_failed"),
  )
}


export function shouldSubmitComposer(
  key: string,
  shiftKey: boolean,
  isComposing = false,
): boolean {
  return key === "Enter" && !shiftKey && !isComposing
}


export function pollDelay(failureCount: number): number {
  return Math.min(12_000, 1500 * (2 ** Math.max(0, failureCount)))
}


export function statusLabel(status: TriageTaskStatus): string {
  return STATUS_LABELS[status]
}


export const FAILURE_KIND_LABELS: Record<
  Exclude<NonNullable<TriageTask["failure_kind"]>, "knowledge">,
  string
> = {
  rejections: "连续驳回耗尽",
  turn_cap: "工具轮次耗尽",
  time_limit: "运行时限耗尽",
  diagnostic_cap: "诊断次数耗尽",
  tool_cap: "工具调用额度耗尽",
  model_unavailable: "模型服务连续不可用",
  closing_round_unanswered: "收口回合未收口",
  legacy_runtime_retired: "旧版 Agent 已下线",
}


/**
 * Why a failed task stopped. Exhaustion also lands in kb_failed once a report
 * exists, so failure_kind wins over the status; other failures show the
 * first line of the stored error.
 */
export function failureCause(task: TriageTask): string | null {
  if (task.status !== "failed" && task.status !== "kb_failed") return null
  if (task.failure_kind && task.failure_kind !== "knowledge") {
    return FAILURE_KIND_LABELS[task.failure_kind]
  }
  if (task.failure_kind === "knowledge") return null
  return (task.error || "").split("\n", 1)[0].trim() || null
}


/** User-facing task state intentionally collapses queue and execution into one phase.
 * 写操作跟踪期间(waiting_operation)也归「Agent运行中」:Harness 在跑,用户不能输入。 */
export function agentDisplayPhase(status: TriageTaskStatus): AgentDisplayPhase {
  if (status === "queued" || status === "running" || status === "waiting_operation") return "active"
  if (status === "waiting_input") return "can_input"
  if (status === "waiting_approval") return "approval"
  return "finished"
}


export function agentDisplayLabel(status: TriageTaskStatus): string {
  const phase = agentDisplayPhase(status)
  if (phase === "active") return "Agent运行中"
  if (phase === "can_input") return "可以输入"
  if (phase === "approval") return "等待审批"
  return statusLabel(status)
}


export const TASK_TYPE_LABELS: Record<TaskType, string> = {
  qa: "问答",
  investigation: "调查排查",
  alert: "精准告警",
  artifact: "任务产物",
  default: "通用任务",
}


export function taskTypeLabel(taskType: TaskType | null | undefined): string {
  return taskType ? TASK_TYPE_LABELS[taskType] : "待分类"
}

// 可排查集群(托管或直连 MySQL)的显示名,AppShell 启动时按 /api/env 设置,顺序即环境包声明顺序
export const TRIAGE_TARGET_LABELS: Record<TriageTarget, string> = {}
let platformPrefixes = new Set<string>()

export function setTriageTargets(clusters: EnvCluster[]): void {
  for (const key of Object.keys(TRIAGE_TARGET_LABELS)) delete TRIAGE_TARGET_LABELS[key]
  platformPrefixes = new Set()
  for (const cluster of clusters) {
    platformPrefixes.add(cluster.key.toLowerCase())
    if (cluster.short_name) platformPrefixes.add(cluster.short_name.toLowerCase())
    if (cluster.roles.includes("managed") || cluster.roles.includes("mysql_direct")) {
      TRIAGE_TARGET_LABELS[cluster.key] = cluster.display_name
    }
  }
}

/** 告警正文的「<集群> / …」标题行不是任务描述。 */
function isPlatformHeader(line: string): boolean {
  const head = line.match(/^([^\s/]+)\s*\/\s*/)?.[1]?.toLowerCase()
  return Boolean(head && platformPrefixes.has(head))
}

export function triageTargetLabel(target: TriageTarget | null | undefined): string {
  return target ? TRIAGE_TARGET_LABELS[target] || target : "平台待确认"
}

/** Compact task label for list/header chrome; the full request remains in title. */
export function taskDisplayTitle(message: string, persistedTitle?: string | null): string {
  if (persistedTitle?.trim()) return persistedTitle.trim()
  const alertProject = message.match(/【告警项目】\s*[：:]\s*([^\n]+)/)?.[1]?.trim()
  if (alertProject) return alertProject
  const firstLine = message
    .split(/\r?\n/)
    .map((line) => line.trim())
    .find((line) => line && !/^=+$/.test(line) && !isPlatformHeader(line))
    || "未命名任务"
  const clause = firstLine.split(/[，。；:：!?！？]/)[0].trim() || firstLine
  if (clause.length <= 34) return clause
  return `${clause.slice(0, 32)}…`
}


export function taskDialogDefaultKey(
  selection: WorkflowModelDefault | null,
): string {
  return selection
    ? `${selection.model_id}\u0000${selection.reasoning_effort}`
    : ""
}


export function shouldPromptForMissingModel(
  health: TriageHealth | null,
  selection: WorkflowModelDefault | null,
): boolean {
  return Boolean(
    health
    && (health.model === "not_configured" || selection === null),
  )
}


export function composerSendEnabled(
  task: TriageTask | null,
  message: string,
  busy: boolean,
  sendEnabled: boolean,
  createEnabled: boolean,
  missingModel: boolean,
): boolean {
  return Boolean(
    !busy
    && message.trim()
    && sendEnabled
    && (task !== null || createEnabled || missingModel),
  )
}


// 运行时控制项改动是否值得发 PATCH:任一字段为空串,或全部字段都与任务现值相同,都不发。
// 空串来自 Radix Select 的表单回流:Select 在 <form> 内会渲染隐藏原生 <select>,受控 value
// 一变就写进去并派发 change;选项还没挂载时(整页打开 ?task=,任务详情先于模型列表到达)
// 原生 select 读回空串,经 onValueChange 冒充一次用户选择。
export function runtimeControlsChanged(
  task: Pick<TriageTask, "platform" | "node" | "model_profile_id" | "reasoning_effort" | "command_mode">,
  updates: RuntimeControlUpdates,
): boolean {
  const current: Record<keyof RuntimeControlUpdates, string | null> = {
    platform: task.platform,
    node: task.node,
    model_id: task.model_profile_id,
    reasoning_effort: task.reasoning_effort,
    command_mode: task.command_mode,
  }
  const fields = (Object.keys(updates) as Array<keyof RuntimeControlUpdates>)
    .filter((key) => updates[key] !== undefined)
  return fields.every((key) => updates[key] !== "")
    && fields.some((key) => updates[key] !== current[key])
}


// 档位在代码里叫 whitelist,给人看的名字是 default。审核卡片与 chip 下拉共用
// 这一份映射,避免两处各写各的文案。
export const COMMAND_MODE_LABELS: Record<string, string> = {
  whitelist: "default",
  auto: "auto",
  bypass: "bypass",
}


export function commandModeLabel(mode: string | null | undefined): string {
  if (!mode) return "未知"
  return COMMAND_MODE_LABELS[mode] || mode
}


const OPERATION_OUTCOME_LABELS: Record<OperationOutcome, string> = {
  succeeded: "成功",
  partial: "部分成功",
  failed: "失败",
  unverified: "已完成未复核",
  unknown: "结果未知",
  noop: "无需执行",
  not_executed: "未执行",
}


export function operationOutcomeLabel(outcome: OperationOutcome): string {
  return OPERATION_OUTCOME_LABELS[outcome] ?? outcome
}


/** 结论色调:成功绿、需关注黄、失败红、没发生写动作灰。 */
export function operationOutcomeTone(outcome: OperationOutcome): "ok" | "warn" | "bad" | "muted" {
  if (outcome === "succeeded") return "ok"
  if (outcome === "failed") return "bad"
  if (outcome === "noop" || outcome === "not_executed") return "muted"
  return "warn"
}


export function operationPhaseLabel(execution: OperationExecution): string {
  if (execution.phase === "finished" && execution.outcome) {
    return execution.replanned ? "计划已失效,已重新预检" : operationOutcomeLabel(execution.outcome)
  }
  if (execution.phase === "reconciling") return "结果待对账:请求可能已送达,Harness 正在核对"
  if (execution.phase === "submitted") {
    const status = execution.requestStatus ? ` · ${execution.requestStatus}` : ""
    const pct = execution.progressPct !== null ? ` ${Math.round(execution.progressPct)}%` : ""
    return `已下发,跟踪中 · 请求 ${execution.requestId ?? "?"}${status}${pct}`
  }
  return "Harness 正在签发令牌并下发"
}


/** 计划剩余有效期;过期或无法解析返回 null。 */
export function planRemainingSeconds(expiresAt: string, now: number): number | null {
  const deadline = Date.parse(expiresAt)
  if (Number.isNaN(deadline)) return null
  const remaining = Math.floor((deadline - now) / 1000)
  return remaining > 0 ? remaining : null
}
