import type {
  ApprovalDecision,
  ApprovalScope,
  ApprovalRequest,
  FinishEvidence,
  FinishHypothesis,
  FinishStatus,
  FinishSummary,
  BudgetReviewRequest,
  KbProposalRequest,
  KbReviewOutcome,
  KbReviewRequest,
  OperationExecution,
  OperationOutcome,
  OperationReviewRequest,
  ReportGateRequest,
  TriageEvent,
  TriageTaskStatus,
  WorklogReviewRequest,
  CommandReviewTargets,
  CommandReviewVerdict,
  TaskTypeQuestionRequest,
  ToolResultEnvelope,
} from "../types.ts"


export type ToolBlock = {
  kind: "tool"
  eventId: string
  invocationId: string
  batchId?: string
  batchIndex?: number | null
  toolName: string
  actionId: string
  label: string
  scope: string | null
  node: string | null
  status: "running" | "done" | "partial" | "failed" | "pending" | "interrupted" | "rejected" | "skipped" | "needs_input" | "approval_pending"
  stdout: string
  stderr: string
  commands: string[]
  documentKind: "report" | "kb" | null
  knowledgeDoc: string | null
  durationSeconds: number | null
  outcome?: string
  executed?: boolean
  executionState?: string
  commandStatus?: string
  skipReason?: string | null
  timing?: ToolResultEnvelope["timing"]
  targetKind?: string
  connectionStatus?: string | null
  evidenceEligible?: boolean
  errorDisposition?: string | null
  argumentsText: string
  resultText: string
}

export type ExecutedCommand = {
  key: string
  actionId: string
  node: string | null
  command: string
  activity?: ToolBlock
}

export type ConversationBlock =
  | {
      kind: "message"
      eventId: string
      role: "user" | "assistant"
      text: string
    }
  | ToolBlock
  | {
      kind: "approval"
      eventId: string
      request: ApprovalRequest
      decided: boolean | null
    }
  | {
      kind: "command_review"
      eventId: string
      approvalId: string
      command: string
      signature: string
      node: string | null
      mode: string
      reason: string
      review: CommandReviewVerdict | null
      targets: CommandReviewTargets
      outcome: KbReviewOutcome | null
    }
  | {
      kind: "kb_review"
      eventId: string
      request: KbReviewRequest
      outcome: KbReviewOutcome | null
    }
  | {
      kind: "kb_proposal_review"
      eventId: string
      request: KbProposalRequest
      outcome: KbReviewOutcome | null
    }
  | {
      kind: "worklog_review"
      eventId: string
      request: WorklogReviewRequest
      outcome: KbReviewOutcome | null
    }
  | {
      kind: "task_type_question"
      eventId: string
      request: TaskTypeQuestionRequest
    }
  | {
      kind: "agent_handoff"
      eventId: string
      request: AgentHandoffRequest
      decision: string | null
    }
  | {
      kind: "budget_review"
      eventId: string
      request: BudgetReviewRequest
      outcome: KbReviewOutcome | null
    }
  | {
      // 插件写操作:同一张卡随事件更新(计划 → 裁决 → 执行 → 跟踪 → 结论)
      kind: "operation_review"
      eventId: string
      request: OperationReviewRequest
      outcome: KbReviewOutcome | null
      decidedBy: string | null
      execution: OperationExecution | null
    }
  | {
      kind: "report_review"
      eventId: string
      request: ReportGateRequest
      outcome: KbReviewOutcome | null
    }
  | {
      kind: "status"
      eventId: string
      status: string
    }
  | {
      kind: "error"
      eventId: string
      message: string
    }
  | {
      kind: "model_retry"
      eventId: string
      attempt: number
      maxAttempts: number
      waitSeconds: number
    }
  | {
      // 新运行时由 Harness 追加的说明:开场材料、预算提醒、收口回合、未收口、档位变更
      kind: "notice"
      eventId: string
      variant: NoticeVariant
      text: string
    }
  | {
      kind: "finish"
      eventId: string
      summary: FinishSummary
    }


export type NoticeVariant = "opening" | "reminder" | "closing" | "unanswered" | "tier" | "notice"

export type ModelUsageSummary = {
  requests: number
  promptTokens: number
  cachedTokens: number
  completionTokens: number
  hitRate: number | null
}


export type AgentHandoffRequest = {
  handoffId: string
  fromAgent: string
  toAgent: string
  reason: string
  missing: string[]
  options: string[]
}


const TERMINAL = new Set<TriageTaskStatus>([
  "completed",
  "failed",
  "kb_failed",
  "cancelled",
])
const KNOWLEDGE_TOOLS = new Set([
  "lookup_alert_route",
  "search_kb_candidates",
  "read_kb_document",
  "create_kb_draft",
  "kb_search",
  "kb_grep",
  "kb_read",
])
// 读取知识文档全文的工具(结构档 / 新运行时)
const KNOWLEDGE_READ_TOOLS = new Set(["read_kb_document", "kb_read"])
const SENSITIVE_DETAIL_KEY = /(?:api[_-]?key|password|passwd|secret|token|authorization|credential|private[_-]?key|ssh[_-]?key|key[_-]?file|webhook[_-]?key|totp)/i
const DETAIL_STRING_LIMIT = 4000
const DETAIL_JSON_LIMIT = 12000


type ToolProjectionContext = {
  taskStatus?: TriageTaskStatus
  taskNode?: string | null
}


export function assertSchemaV1(value: {schema_version?: number}): void {
  if (value.schema_version !== 1) {
    throw new Error(`不支持的数据版本: ${String(value.schema_version)}`)
  }
}


export function isTerminalTask(status: TriageTaskStatus): boolean {
  return TERMINAL.has(status)
}


export function mergeTaskEvents(
  existing: TriageEvent[],
  incoming: TriageEvent[],
): TriageEvent[] {
  const seen = new Set(existing.map((event) => event.event_id))
  const merged = [...existing]
  for (const event of incoming) {
    assertSchemaV1(event)
    if (!seen.has(event.event_id)) {
      seen.add(event.event_id)
      merged.push(event)
    }
  }
  return merged
}


export function toConversationBlocks(
  events: TriageEvent[],
  context: ToolProjectionContext = {},
): ConversationBlock[] {
  const results = new Map<string, TriageEvent>()
  const calls = new Set<string>()
  const legacyBatchByCallId = new Map<string, string>()
  for (const event of events) {
    if (event.type === "assistant_message") {
      const message = recordValue(event.payload.message)
      const toolCalls = message.tool_calls
      if (Array.isArray(toolCalls)) {
        for (const item of toolCalls) {
          const value = recordValue(item)
          const id = stringValue(value.id)
          if (id) legacyBatchByCallId.set(id, `legacy-${event.event_id}`)
        }
      }
    }
    const invocation = stringValue(event.payload.tool_call_id)
    if (event.type === "tool_call" && invocation) calls.add(invocation)
    if (event.type === "tool_result" && invocation) results.set(invocation, event)
  }

  const decided = new Map<string, KbReviewOutcome>()
  const decidedBy = new Map<string, string>()
  for (const event of events) {
    if (event.type !== "approval_decided") continue
    const approvalId = stringValue(event.payload.approval_id)
    if (approvalId) decided.set(approvalId, toOutcome(event.payload))
    const by = recordValue(event.payload.decided_by)
    if (approvalId && (stringValue(by.name) || stringValue(by.id))) {
      decidedBy.set(approvalId, stringValue(by.name) || stringValue(by.id))
    }
  }
  const executions = operationExecutions(events)

  const handoffDecisions = new Map<string, string>()
  for (const event of events) {
    if (![
      "agent_handoff_decided",
      "agent_handoff_accepted",
      "agent_handoff_completed",
    ].includes(event.type)) continue
    const handoffId = stringValue(event.payload.handoff_id)
    if (!handoffId) continue
    handoffDecisions.set(
      handoffId,
      stringValue(event.payload.decision)
        || event.type.replace("agent_handoff_", ""),
    )
  }

  const blocks: ConversationBlock[] = []
  // harness_notice 的含义取决于紧挨着它的标记事件(开场 / 预算提醒 / 收口回合)
  let noticeVariant: NoticeVariant | null = null
  for (const event of events) {
    assertSchemaV1(event)
    if (event.type === "agent_opening") {
      noticeVariant = "opening"
    } else if (event.type === "budget_reminder") {
      noticeVariant = "reminder"
    } else if (event.type === "closing_round") {
      noticeVariant = "closing"
    } else if (event.type === "harness_notice") {
      blocks.push({
        kind: "notice",
        eventId: event.event_id,
        variant: noticeVariant ?? "notice",
        text: boundedText(event.payload.message, 20000),
      })
      noticeVariant = null
    } else if (event.type === "closing_round_unanswered") {
      blocks.push({
        kind: "notice",
        eventId: event.event_id,
        variant: "unanswered",
        text: `收口回合仍未收口(${boundedText(event.payload.reason, 500)}),已按已采记录生成未收口报告。`,
      })
    } else if (event.type === "harness_tier_changed") {
      blocks.push({
        kind: "notice",
        eventId: event.event_id,
        variant: "tier",
        text: `Harness 档位 ${tierLabel(event.payload.from)} → ${tierLabel(event.payload.to)},下一轮生效`,
      })
    } else if (event.type === "user_message") {
      const text = boundedText(event.payload.message)
      if (text === "请生成或更新本次任务的 worklog。" || text === "请记录本次排查 worklog。") {
        continue
      }
      blocks.push({
        kind: "message",
        eventId: event.event_id,
        role: "user",
        text,
      })
    } else if (event.type === "assistant_message") {
      const message = recordValue(event.payload.message)
      blocks.push({
        kind: "message",
        eventId: event.event_id,
        role: "assistant",
        text: visibleAssistantText(message.content),
      })
    } else if (event.type === "tool_call") {
      const invocation = stringValue(event.payload.tool_call_id)
      if (invocation) {
        const result = results.get(invocation)
        const summary = event.payload.name === "finish" ? finishSummary(result) : null
        blocks.push(summary
          ? {kind: "finish", eventId: event.event_id, summary}
          : toolBlock(event, result, context, legacyBatchByCallId.get(invocation)))
      }
    } else if (event.type === "tool_result") {
      const invocation = stringValue(event.payload.tool_call_id)
      if (invocation && !calls.has(invocation)) {
        blocks.push(toolBlock(
          event,
          event,
          context,
          legacyBatchByCallId.get(invocation),
        ))
      }
    } else if (
      event.type === "task_type_clarification_requested"
      || event.type === "task_context_clarification_requested"
    ) {
      blocks.push({
        kind: "task_type_question",
        eventId: event.event_id,
        request: {
          question: boundedText(event.payload.question, 2000),
          options: stringArray(event.payload.options).slice(0, 4),
          taskType: stringValue(event.payload.task_type),
        },
      })
    } else if (event.type === "agent_handoff_requested") {
      const handoffId = stringValue(event.payload.handoff_id)
      if (handoffId) {
        blocks.push({
          kind: "agent_handoff",
          eventId: event.event_id,
          request: {
            handoffId,
            fromAgent: stringValue(event.payload.from_agent),
            toAgent: stringValue(event.payload.to_agent),
            reason: boundedText(event.payload.reason, 4000),
            missing: stringArray(event.payload.missing),
            options: stringArray(event.payload.options).slice(0, 4),
          },
          decision: handoffDecisions.get(handoffId) ?? null,
        })
      }
    } else if (event.type === "approval_requested") {
      const approvalId = stringValue(event.payload.approval_id)
      if (event.payload.kind === "command") {
        const reviewValue = recordValue(event.payload.review)
        const targetsValue = recordValue(event.payload.targets)
        const mysql = Array.isArray(targetsValue.mysql)
          ? targetsValue.mysql.slice(0, 100).map((item) => {
            const value = recordValue(item)
            return {
              database: nullableString(value.database),
              tables: stringArray(value.tables),
            }
          })
          : []
        blocks.push({
          kind: "command_review",
          eventId: event.event_id,
          approvalId,
          command: boundedText(event.payload.command, 20000),
          signature: boundedText(event.payload.signature, 1000),
          node: nullableString(event.payload.node),
          mode: stringValue(event.payload.mode),
          reason: boundedText(event.payload.reason, 4000),
          review: event.payload.review == null ? null : {
            safe: reviewValue.safe === true,
            reason: boundedText(reviewValue.reason, 2000),
            reviewed: reviewValue.reviewed === true,
          },
          targets: {
            paths: stringArray(targetsValue.paths),
            mysql,
            mysql_parsed: targetsValue.mysql_parsed !== false,
          },
          outcome: decided.get(approvalId) ?? null,
        })
      } else if (event.payload.kind === "operation") {
        blocks.push({
          kind: "operation_review",
          eventId: event.event_id,
          request: operationRequest(approvalId, event.payload),
          outcome: decided.get(approvalId) ?? null,
          decidedBy: decidedBy.get(approvalId) ?? null,
          execution: executions.get(approvalId) ?? null,
        })
      } else if (event.payload.kind === "kb_draft") {
        blocks.push({
          kind: "kb_review",
          eventId: event.event_id,
          request: {
            approvalId,
            doc: stringValue(event.payload.doc),
            mode: stringValue(event.payload.mode),
            markdown: boundedText(event.payload.markdown, 40000),
            diff: boundedText(event.payload.diff, 40000),
            evidenceIssues: stringArray(event.payload.evidence_issues).slice(0, 12),
          },
          outcome: decided.get(approvalId) ?? null,
        })
      } else if (event.payload.kind === "kb_proposal") {
        blocks.push({
          kind: "kb_proposal_review",
          eventId: event.event_id,
          request: {
            approvalId,
            doc: stringValue(event.payload.doc),
            mode: stringValue(event.payload.mode),
            title: boundedText(event.payload.title, 400),
            summary: boundedText(event.payload.summary, 2000),
            symptoms: stringArray(event.payload.symptoms),
          },
          outcome: decided.get(approvalId) ?? null,
        })
      } else if (event.payload.kind === "budget") {
        blocks.push({
          kind: "budget_review",
          eventId: event.event_id,
          request: {
            approvalId,
            reason: boundedText(event.payload.reason, 500),
            toolTurns: numberValue(event.payload.tool_turns) ?? 0,
            turnBudget: numberValue(event.payload.turn_budget) ?? 0,
            diagnosticCalls: numberValue(event.payload.diagnostic_calls) ?? 0,
            diagnosticBudget: numberValue(event.payload.diagnostic_budget) ?? 0,
            grants: numberValue(event.payload.grants) ?? 0,
            grantTurns: numberValue(event.payload.grant_turns) ?? 0,
            hasReport: event.payload.has_report === true,
          },
          outcome: decided.get(approvalId) ?? null,
        })
      } else if (event.payload.kind === "worklog") {
        blocks.push({
          kind: "worklog_review",
          eventId: event.event_id,
          request: {
            approvalId,
            preview: boundedText(event.payload.preview, 8000),
          },
          outcome: decided.get(approvalId) ?? null,
        })
      } else if (event.payload.kind === "report_gate") {
        const proposal = recordValue(event.payload.proposal)
        // 新运行时的提案是 {finish, verification};旧任务是 finalize_report 的参数
        const finish = recordValue(proposal.finish)
        const verification = recordValue(proposal.verification)
        blocks.push({
          kind: "report_review",
          eventId: event.event_id,
          request: {
            approvalId,
            reason: boundedText(event.payload.reason, 4000),
            verdict: stringValue(proposal.verdict),
            severity: stringValue(proposal.severity || finish.severity),
            rootCause: boundedText(proposal.root_cause, 8000),
            evidence: stringArray(proposal.evidence),
            remediation: stringArray(proposal.remediation ?? finish.remediation),
            ...(Object.keys(finish).length ? {
              answer: boundedText(finish.answer, 8000),
              hypotheses: hypothesisList(verification.hypotheses),
            } : {}),
          },
          outcome: decided.get(approvalId) ?? null,
        })
      } else {
        blocks.push({
          kind: "approval",
          eventId: event.event_id,
          request: {
            approvalId,
            actionId: stringValue(event.payload.action_id),
            command: boundedText(event.payload.command, 2000),
            impact: boundedText(event.payload.impact, 2000),
            rollback: boundedText(event.payload.rollback, 2000),
          },
          decided: decided.has(approvalId)
            ? decided.get(approvalId)!.decision === "approve"
            : null,
        })
      }
    } else if (event.type === "status") {
      blocks.push({
        kind: "status",
        eventId: event.event_id,
        status: stringValue(event.payload.status),
      })
    } else if (event.type === "model_retry") {
      blocks.push({
        kind: "model_retry",
        eventId: event.event_id,
        attempt: numberValue(event.payload.attempt) ?? 0,
        maxAttempts: numberValue(event.payload.max_attempts) ?? 0,
        waitSeconds: numberValue(event.payload.wait_seconds) ?? 0,
      })
    } else if (event.type === "error") {
      blocks.push({
        kind: "error",
        eventId: event.event_id,
        message: boundedText(event.payload.message, 4000),
      })
    }
  }
  return blocks
}

const OPERATION_OUTCOMES = new Set<OperationOutcome>([
  "succeeded", "partial", "failed", "unverified", "unknown", "noop", "not_executed",
])


function operationRequest(approvalId: string, payload: Record<string, unknown>): OperationReviewRequest {
  const plan = recordValue(payload.plan)
  const request = recordValue(plan.request)
  const impact = payload.impact == null ? null : recordValue(payload.impact)
  const current = Array.isArray(plan.current) ? plan.current.slice(0, 200) : []
  const changes = Array.isArray(plan.pending_config_changes) ? plan.pending_config_changes.slice(0, 50) : []
  return {
    approvalId,
    title: boundedText(payload.title, 200),
    plugin: stringValue(payload.plugin),
    cluster: stringValue(payload.cluster),
    planId: stringValue(payload.plan_id),
    planHash: stringValue(payload.plan_hash),
    reason: boundedText(payload.reason, 2000),
    createdAt: stringValue(payload.created_at),
    expiresAt: stringValue(payload.expires_at),
    expectedMaxDurationS: numberValue(payload.expected_max_duration_s) ?? 0,
    summary: stringArray(plan.summary).slice(0, 20).map((item) => boundedText(item, 500)),
    current: current.map((item) => {
      const value = recordValue(item)
      return {
        host: stringValue(value.host),
        component: stringValue(value.component),
        state: stringValue(value.state),
        desiredState: stringValue(value.desired_state),
        maintenance: stringValue(value.maintenance),
      }
    }),
    pendingChanges: changes.map((item) => Object.values(recordValue(item))
      .filter((value) => typeof value === "string" || typeof value === "number")
      .map((value) => boundedText(value, 200))
      .join(" · ")),
    warnings: [...stringArray(plan.warnings), ...stringArray(payload.plan_warnings)]
      .slice(0, 20).map((item) => boundedText(item, 500)),
    request: {
      method: stringValue(request.method),
      path: boundedText(request.path, 500),
      body: boundedDetailJson(recordValue(request.body)),
    },
    impact: impact && {
      ok: impact.ok === true,
      downstream: stringArray(impact.downstream).slice(0, 50),
      upstream: stringArray(impact.upstream).slice(0, 50),
      error: boundedText(impact.error, 300),
    },
    replannedFrom: nullableString(payload.replanned_from),
    replanReason: boundedText(payload.replan_reason, 500),
  }
}


/** operation_* 事件按审批 ID 归并成执行进展;只取展示所需字段(事件里本来就没有令牌与密钥)。 */
export function operationExecutions(events: TriageEvent[]): Map<string, OperationExecution> {
  const views = new Map<string, OperationExecution>()
  for (const event of events) {
    if (!event.type.startsWith("operation_")) continue
    const approvalId = stringValue(event.payload.approval_id)
    if (!approvalId) continue
    const view: OperationExecution = views.get(approvalId) ?? {
      phase: "executing", requestId: null, requestStatus: null, progressPct: null, tasks: [], outcome: null,
      reason: "", oplog: null, oplogError: null, replanned: false, tokenDigest: null,
    }
    const payload = event.payload
    if (event.type === "operation_executing") {
      view.phase = "executing"
    } else if (event.type === "operation_executed") {
      view.tokenDigest = nullableString(payload.token_digest) ?? view.tokenDigest
      if (payload.maybe_delivered === true && payload.request_id == null) view.phase = "reconciling"
    } else if (event.type === "operation_submitted") {
      view.phase = "submitted"
      view.requestId = numberValue(payload.request_id)
      view.tokenDigest = nullableString(payload.token_digest) ?? view.tokenDigest
    } else if (event.type === "operation_progress") {
      view.requestStatus = nullableString(payload.request_status)
      view.progressPct = numberValue(payload.progress_pct)
      if (Array.isArray(payload.tasks)) {
        view.tasks = payload.tasks.slice(0, 200).map((item) => {
          const value = recordValue(item)
          return {
            host: stringValue(value.host),
            component: stringValue(value.component),
            status: stringValue(value.status),
            exitCode: numberValue(value.exit_code),
            durationS: numberValue(value.duration_s),
          }
        })
      }
    } else if (event.type === "operation_finished") {
      const outcome = stringValue(payload.outcome) as OperationOutcome
      view.phase = "finished"
      view.outcome = OPERATION_OUTCOMES.has(outcome) ? outcome : "unknown"
      view.reason = boundedText(payload.reason, 2000)
      view.oplog = nullableString(payload.oplog)
      view.oplogError = nullableString(payload.oplog_error)
      view.replanned = payload.replanned === true
    }
    views.set(approvalId, view)
  }
  return views
}


/** Providers may put private chain-of-thought in content despite the API contract.
 * Keep the event for model replay, but never render reasoning in the conversation. */
function visibleAssistantText(value: unknown): string {
  const text = boundedText(value)
  if (!text) return ""
  const withoutReasoning = text
    .replace(/<thinking\b[^>]*>[\s\S]*?<\/thinking>/gi, "")
    .replace(/<analysis\b[^>]*>[\s\S]*?<\/analysis>/gi, "")
    .replace(/<reasoning\b[^>]*>[\s\S]*?<\/reasoning>/gi, "")
    // Some compatible gateways emit an opening tag without a closing tag.
    // In that case the whole remainder is private rather than safe final text.
    .replace(/<(?:thinking|analysis|reasoning)\b[^>]*>[\s\S]*$/gi, "")
    .trim()
  return withoutReasoning
}


function toolBlock(
  call: TriageEvent,
  resultEvent: TriageEvent | undefined,
  context: ToolProjectionContext,
  legacyBatchId?: string,
): ToolBlock {
  const callPayload = call.payload
  const resultPayload = resultEvent?.payload ?? {}
  const argumentsValue = recordValue(callPayload.arguments)
  const rawResult = recordValue(resultPayload.result)
  const canonical = recordValue(rawResult.tool_result) as Partial<ToolResultEnvelope>
  const canonicalData = recordValue(canonical.data)
  const canonicalTarget = recordValue(canonical.target)
  const canonicalExecution = recordValue(canonicalData.execution)
  // Canonical data is preferred, but the flat projection remains available
  // for old task records and old action consumers.
  const resultValue: Record<string, unknown> = {
    ...canonicalData,
    ...rawResult,
    ...(Object.keys(canonicalExecution).length > 0 ? canonicalExecution : {}),
  }
  if (resultValue.node === undefined && canonicalTarget.node !== undefined) {
    resultValue.node = canonicalTarget.node
  }
  if (resultValue.platform === undefined && canonicalTarget.platform !== undefined) {
    resultValue.platform = canonicalTarget.platform
  }
  const invocationId = (
    stringValue(callPayload.tool_call_id)
    || stringValue(resultPayload.tool_call_id)
  )
  const batchId = stringValue(callPayload.batch_id)
    || stringValue(resultPayload.batch_id)
    || legacyBatchId
  const batchIndex = numberValue(callPayload.batch_index) ?? numberValue(resultPayload.batch_index)
  const toolName = (
    stringValue(callPayload.name)
    || stringValue(resultPayload.name)
  )
  const actionId = (
    stringValue(argumentsValue.diagnostic_id)
    || stringValue(argumentsValue.action_id)
    || stringValue(argumentsValue.playbook)
    || stringValue(resultValue.diagnostic_id)
    || toolName
  )
  const exitCode = numberValue(resultValue.exit_code)
  const outcome = stringValue(canonical.outcome) || nullishOutcome(resultValue)
  const executionState = stringValue(canonical.execution_state)
    || (Object.keys(canonicalExecution).length > 0 ? "executed" : "unknown")
  const commandStatus = stringValue(canonical.command_status)
    || (exitCode === null ? "not_applicable" : exitCode === 0 ? "zero" : "nonzero")
  const skipReason = stringValue(recordValue(canonical.skip).reason)
    || stringValue(resultValue.reason)
    || null
  const rejected = outcome === "denied" || outcome === "rejected"
  const skipped = outcome === "skipped" || resultValue.skipped === true
  const interrupted = resultValue.interrupted === true
    || (!resultEvent && context.taskStatus !== undefined
      && isTerminalTask(context.taskStatus))
  const waitingForInput = !resultEvent && context.taskStatus === "waiting_input"
  const waitingForApproval = !resultEvent && context.taskStatus === "waiting_approval"
  const failed = outcome === "failed"
    || resultValue.failed === true
    || (Boolean(stringValue(resultValue.error)) && executionState !== "executed")
  const pending = outcome === "pending_approval"
  const needsInput = outcome === "needs_input"
  const partial = outcome === "completed_partial"
  const metricsDurationMs = numberValue(recordValue(canonical.metrics).duration_ms)
  const timingValue = recordValue(canonical.timing)
  const timing = Object.keys(timingValue).length
    ? timingValue as ToolResultEnvelope["timing"]
    : undefined
  const status: ToolBlock["status"] = interrupted
    ? "interrupted"
    : waitingForInput
      ? "needs_input"
    : waitingForApproval
      ? "approval_pending"
    : needsInput
      ? "needs_input"
      : pending
      ? "pending"
    : skipped
      ? "skipped"
    : rejected
      ? "rejected"
    : partial
      ? "partial"
    : failed
      ? "failed"
    : resultEvent
      ? "done"
      : "running"
  const plannedCommands = toolName === "run_command"
    ? plannedToolCommands(argumentsValue)
    : []
  const actualCommands = toolCommands(resultValue)
  const label = toolName === "run_command"
    ? actualCommands[0] || plannedCommands[0] || "命令"
    : actionId
  const scope = toolScope(
    toolName,
    argumentsValue,
    resultValue,
    context.taskNode ?? null,
  )
  const block: ToolBlock = {
    kind: "tool",
    eventId: call.event_id,
    invocationId,
    toolName,
    actionId,
    label,
    scope,
    node: scope,
    status,
    stdout: boundedText(resultValue.stdout, 8000),
    stderr: boundedText(resultValue.stderr, 4000),
    commands: actualCommands,
    documentKind: documentKind(toolName, resultValue),
    knowledgeDoc: KNOWLEDGE_READ_TOOLS.has(toolName) && status === "done"
      ? nullableString(resultValue.doc)
      : null,
    durationSeconds: (
      numberValue(resultValue.duration_seconds)
      ?? numberValue(resultValue.duration)
      ?? (metricsDurationMs === null ? null : metricsDurationMs / 1000)
    ),
    argumentsText: boundedDetailJson(argumentsValue),
    resultText: boundedDetailJson(resultValue),
  }
  if (toolName === "shell") applyShell(block, argumentsValue, resultValue, Boolean(resultEvent))
  const plugin = pluginToolParts(toolName)
  if (plugin) applyPlugin(block, plugin, resultValue)
  if (batchId) block.batchId = batchId
  if (batchIndex !== null) block.batchIndex = batchIndex
  if (Object.keys(canonicalExecution).length > 0 || canonical.execution_state) {
    block.executed = executionState === "executed"
    block.executionState = executionState
  }
  if (canonical.command_status || exitCode !== null || Object.keys(canonicalExecution).length > 0) {
    block.commandStatus = commandStatus
  }
  if (skipReason) block.skipReason = skipReason
  if (timing) block.timing = timing
  if (Object.keys(recordValue(rawResult.tool_result)).length > 0) {
    const canonicalError = recordValue(canonical.error)
    block.outcome = outcome
    block.targetKind = stringValue(canonicalTarget.kind)
    block.connectionStatus = stringValue(recordValue(canonical.connection).status) || null
    block.evidenceEligible = recordValue(canonical.evidence).eligible === true
    block.errorDisposition = stringValue(canonicalError.disposition) || null
  }
  return block
}


/** 插件(MCP)工具:模型可见名 mcp__<插件>__<工具>,展示为「插件 · 工具」。 */
export function pluginToolParts(toolName: string): { plugin: string; tool: string } | null {
  const match = /^mcp__([a-z][a-z0-9-]*)__(.+)$/.exec(toolName)
  return match ? { plugin: match[1], tool: match[2] } : null
}


function applyPlugin(
  block: ToolBlock,
  plugin: { plugin: string; tool: string },
  resultValue: Record<string, unknown>,
) {
  block.label = `${plugin.plugin} · ${plugin.tool}`
  block.scope = `插件 ${plugin.plugin}`
  block.node = block.scope
  const error = recordValue(resultValue.error)
  if (resultValue.ok === false && Object.keys(error).length > 0) {
    block.stderr = boundedText(`${stringValue(error.code)}: ${stringValue(error.message)}`, 4000)
  } else if (resultValue.data !== undefined) {
    block.stdout = boundedText(JSON.stringify(resultValue.data, null, 2), 8000)
  }
}


/** 新运行时的裸 shell:一条命令可下发多个节点,输出按节点分段展示。 */
function applyShell(
  block: ToolBlock,
  argumentsValue: Record<string, unknown>,
  resultValue: Record<string, unknown>,
  hasResult: boolean,
): void {
  const command = stringValue(argumentsValue.command) || stringValue(resultValue.command)
  const nodes = stringArray(argumentsValue.nodes)
  const results = Array.isArray(resultValue.results) ? resultValue.results.map(recordValue) : []
  const firstLine = command.split("\n")[0].trim()
  block.label = firstLine.length <= 200 ? firstLine || "shell" : `${firstLine.slice(0, 200)}…`
  block.commands = command
    ? [redactSensitiveText(command.length <= 2000 ? command : `${command.slice(0, 2000)}…命令已截断`)]
    : []
  if (nodes.length) block.scope = block.node = nodes.join(", ")
  if (!results.length) return
  block.stdout = boundedText(results.map((item) => {
    const node = stringValue(item.node)
    if (stringValue(item.error)) return `## ${node} · 执行失败\n${stringValue(item.error)}`
    const header = `## ${node} · 退出 ${numberValue(item.exit_code) ?? "?"} · ${stringValue(item.ref)}`
    const stderr = stringValue(item.stderr)
    return [header, stringValue(item.stdout).trimEnd(), stderr ? `[stderr]\n${stderr.trimEnd()}` : ""]
      .filter(Boolean).join("\n")
  }).join("\n\n"), 12000)
  block.stderr = ""
  const failures = results.filter((item) => stringValue(item.error)).length
  if (hasResult && failures === results.length) block.status = "failed"
  else if (hasResult && failures > 0) block.status = "partial"
  const codes = results.map((item) => numberValue(item.exit_code)).filter((code): code is number => code !== null)
  if (codes.length) block.commandStatus = codes.some((code) => code !== 0) ? "nonzero" : "zero"
}


/** 新运行时 finish 的核验结果;没有结果或执行失败时返回 null,照常按工具调用展示。 */
function finishSummary(resultEvent: TriageEvent | undefined): FinishSummary | null {
  const result = recordValue(resultEvent?.payload.result)
  const verdict = stringValue(result.verdict)
  if (!verdict) return null
  return {
    verdict,
    grounded: result.grounded === true,
    hypotheses: hypothesisList(result.hypotheses),
    facts: evidenceList(result.facts, "fact"),
    report: nullableString(result.report),
  }
}


function hypothesisList(value: unknown): FinishHypothesis[] {
  if (!Array.isArray(value)) return []
  return value.slice(0, 20).map((item) => {
    const record = recordValue(item)
    return {
      statement: boundedText(record.statement, 2000),
      status: finishStatus(record.status) ?? "insufficient",
      declared: finishStatus(record.declared),
      reasons: stringArray(record.reasons ?? record.downgraded),
      evidence: evidenceList(record.evidence),
      related: evidenceList(record.related, "related"),
    }
  })
}


function evidenceList(value: unknown, role = ""): FinishEvidence[] {
  if (!Array.isArray(value)) return []
  return value.slice(0, 30).map((item) => {
    const record = recordValue(item)
    return {
      role: stringValue(record.role) || role,
      ref: stringValue(record.ref),
      found: record.found === true,
      text: boundedText(record.text, 2000),
      reason: boundedText(record.reason, 500),
    }
  })
}


function finishStatus(value: unknown): FinishStatus | null {
  return value === "supported" || value === "insufficient" || value === "refuted" ? value : null
}


const TIER_LABELS: Record<string, string> = {
  autonomous: "自主档",
  guided: "引导档",
  structured: "结构档",
}


export function tierLabel(value: unknown): string {
  return TIER_LABELS[stringValue(value)] ?? "未声明"
}


/** 新运行时每次模型请求的 usage 汇总;缓存命中率 = 缓存命中输入 / 全部输入。 */
export function modelUsageSummary(events: TriageEvent[]): ModelUsageSummary | null {
  const usage = events.filter((event) => event.type === "model_usage")
  if (!usage.length) return null
  const sum = (key: string) => usage.reduce((total, event) => total + (numberValue(event.payload[key]) ?? 0), 0)
  const promptTokens = sum("prompt_tokens")
  const cachedTokens = sum("cached_tokens")
  return {
    requests: usage.length,
    promptTokens,
    cachedTokens,
    completionTokens: sum("completion_tokens"),
    hitRate: promptTokens > 0 ? cachedTokens / promptTokens : null,
  }
}


function nullishOutcome(resultValue: Record<string, unknown>): string {
  return stringValue(resultValue.outcome) || "completed"
}


function toOutcome(payload: Record<string, unknown>): KbReviewOutcome {
  const raw = stringValue(payload.decision)
  const decision: ApprovalDecision = (
    raw === "approve" || raw === "revise" || raw === "reject"
      ? raw
      : payload.approved === true ? "approve" : "reject"
  )
  const rawScope = stringValue(payload.scope)
  const scope: ApprovalScope | undefined = (
    rawScope === "once" || rawScope === "task" || rawScope === "always"
      ? rawScope
      : undefined
  )
  const result = {decision, feedback: boundedText(payload.feedback, 2000)}
  return scope ? {...result, scope} : result
}


/**
 * 产出了归档文档的工具调用:详情按渲染后的 Markdown 展示而非执行记录。
 * 以结果里真实写出的路径为准,避免驳回/要求修改的调用也去拉文档。
 */
function documentKind(
  toolName: string,
  resultValue: Record<string, unknown>,
): "report" | "kb" | null {
  if (toolName === "finalize_report" && stringValue(resultValue.report)) {
    return "report"
  }
  if (toolName === "create_kb_draft" && stringValue(resultValue.kb_doc)) {
    return "kb"
  }
  return null
}


/** 本次工具在节点上真正执行的命令:诊断动作一条,playbook 每个 step 一条。 */
function toolCommands(resultValue: Record<string, unknown>): string[] {
  const commands: string[] = []
  const single = stringValue(resultValue.command)
  if (single) commands.push(single)
  const steps = resultValue.steps
  if (Array.isArray(steps)) {
    for (const step of steps.slice(0, 100)) {
      const cmd = stringValue(recordValue(step).cmd)
      if (cmd) commands.push(cmd)
    }
  }
  return commands.map((item) => redactSensitiveText(
    item.length <= 2000 ? item : `${item.slice(0, 2000)}…命令已截断`,
  ))
}


function plannedToolCommands(argumentsValue: Record<string, unknown>): string[] {
  const pipeline = argumentsValue.pipeline
  if (!Array.isArray(pipeline)) return []
  const segments = pipeline
    .filter((segment): segment is unknown[] => Array.isArray(segment))
    .map((segment) => segment.map((token) => String(token)).join(" "))
    .filter(Boolean)
  if (!segments.length) return []
  const command = segments.join(" | ")
  return [redactSensitiveText(
    command.length <= 2000 ? command : `${command.slice(0, 2000)}…命令已截断`,
  )]
}


/** 按对话顺序汇总本次排查在服务器上执行过的全部命令。 */
export function executedCommands(
  blocks: ConversationBlock[],
): ExecutedCommand[] {
  const results: ExecutedCommand[] = []
  for (const block of blocks) {
    if (block.kind !== "tool" || block.executed === false) continue
    block.commands.forEach((command, index) => {
      results.push({
        key: `${block.eventId}-${index}`,
        actionId: block.actionId,
        node: block.node,
        command,
        activity: block,
      })
    })
  }
  return results
}


/** 按对话顺序汇总本次排查实际查阅过的知识文档。 */
export function consultedKnowledgeDocs(blocks: ConversationBlock[]): string[] {
  const seen = new Set<string>()
  const docs: string[] = []
  for (const block of blocks) {
    if (block.kind !== "tool" || !block.knowledgeDoc) continue
    if (seen.has(block.knowledgeDoc)) continue
    seen.add(block.knowledgeDoc)
    docs.push(block.knowledgeDoc)
  }
  return docs
}


function toolScope(
  toolName: string,
  argumentsValue: Record<string, unknown>,
  resultValue: Record<string, unknown>,
  taskNode: string | null,
): string | null {
  if (KNOWLEDGE_TOOLS.has(toolName)) return "知识库"
  const resolvedNode = (
    nullableString(resultValue.node)
    || nullableString(argumentsValue.node)
    || taskNode
  )
  if (
    toolName === "run_registered_playbook"
    || toolName === "run_diagnostic"
    || toolName === "set_task_context"
  ) {
    return resolvedNode
  }
  if (toolName === "finalize_report") return "排查报告"
  return resolvedNode
}


function boundedDetailJson(value: Record<string, unknown>): string {
  const serialized = JSON.stringify(sanitizeDetail(value), null, 2)
  return serialized.length <= DETAIL_JSON_LIMIT
    ? serialized
    : `${serialized.slice(0, DETAIL_JSON_LIMIT)}\n…详情已截断`
}


function sanitizeDetail(value: unknown, depth = 0): unknown {
  if (depth >= 6) return "[内容层级过深，已截断]"
  if (typeof value === "string") {
    const bounded = value.length <= DETAIL_STRING_LIMIT
      ? value
      : `${value.slice(0, DETAIL_STRING_LIMIT)}…内容已截断`
    return redactSensitiveText(bounded)
  }
  if (
    value === null
    || typeof value === "number"
    || typeof value === "boolean"
  ) {
    return value
  }
  if (Array.isArray(value)) {
    const items = value.slice(0, 50).map((item) => (
      sanitizeDetail(item, depth + 1)
    ))
    if (value.length > 50) items.push(`…其余 ${value.length - 50} 项已截断`)
    return items
  }
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
    const result: Record<string, unknown> = {}
    for (const [key, item] of entries.slice(0, 100)) {
      result[key] = SENSITIVE_DETAIL_KEY.test(key)
        ? "[已隐藏]"
        : sanitizeDetail(item, depth + 1)
    }
    if (entries.length > 100) {
      result["…"] = `其余 ${entries.length - 100} 个字段已截断`
    }
    return result
  }
  return String(value)
}


function redactSensitiveText(value: string): string {
  return value
    .replace(
      /\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+/gi,
      "$1 [已隐藏]",
    )
    .replace(/\bsk-[A-Za-z0-9_-]{8,}\b/g, "[已隐藏]")
    .replace(
      /\b(api[_-]?key|password|passwd|secret|token|authorization|credential|webhook[_-]?key)\s*([:=])\s*([^\s,;]+)/gi,
      "$1$2[已隐藏]",
    )
}


function recordValue(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}


function stringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value
    .filter((item): item is string => typeof item === "string")
    .map((item) => item.slice(0, 4000))
}


function stringValue(value: unknown): string {
  return typeof value === "string" ? value : ""
}


function nullableString(value: unknown): string | null {
  const result = stringValue(value)
  return result || null
}


function numberValue(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null
}


function boundedText(value: unknown, limit = 8000): string {
  const text = stringValue(value)
  return text.length <= limit ? text : `${text.slice(0, limit)}\n…输出已截断`
}
