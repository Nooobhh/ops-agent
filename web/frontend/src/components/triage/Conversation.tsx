import { type ReactNode, useEffect, useMemo, useRef, useState } from "react"
import {
  AlertCircle,
  Ban,
  Bot,
  CheckCircle2,
  LoaderCircle,
  RefreshCw,
  RotateCcw,
  Send,
  UserRound,
} from "lucide-react"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { formatTime } from "@/lib/api"
import { isTerminalTask, toConversationBlocks, type ConversationBlock, type ToolBlock } from "@/lib/triageEvents"
import {
  agentDisplayLabel,
  agentDisplayPhase,
  canRetryTask,
  canSendMessage,
  shouldSubmitComposer,
  taskDisplayTitle,
} from "@/lib/triageView"
import type { ApprovalDecision, ApprovalScope, TriageEvent, TriageTask } from "@/types"
import { ApprovalCard } from "./ApprovalCard"
import { CommandReviewCard } from "./CommandReviewCard"
import { OperationReviewCard } from "./OperationReviewCard"
import { BudgetReviewCard } from "./BudgetReviewCard"
import { KbProposalCard } from "./KbProposalCard"
import { KbReviewCard } from "./KbReviewCard"
import { ReportGateCard } from "./ReportGateCard"
import { FinishSummaryCard } from "./FinishSummaryCard"
import { HarnessNotice } from "./HarnessNotice"
import { WorklogReviewCard } from "./WorklogReviewCard"
import { ToolActivity } from "./ToolActivity"
import { ToolActivityGroup } from "./ToolActivityGroup"
import { TriageComposerBar, type ComposerDraft } from "./TriageComposerBar"
import type { CommandMode, ModelOption, RuntimeControlUpdates, TriageTaskDetail, WorkflowModelDefault } from "@/types"


type ConversationProps = {
  task: TriageTask | null
  events: TriageEvent[]
  pendingMessage?: { taskId: string | null; text: string } | null
  loading: boolean
  busy: boolean
  connectionIssue: string
  headerActions?: ReactNode
  onApproval: (
    approvalId: string,
    decision: ApprovalDecision,
    feedback?: string,
    scope?: ApprovalScope,
  ) => Promise<void>
  onCancel: () => Promise<void>
  onComplete: () => Promise<void>
  onRefresh: () => void
  onRetry: () => Promise<void>
  onSend: (message: string) => Promise<void>
  createEnabled: boolean
  missingModel: boolean
  createReason: string
  models: ModelOption[]
  manualDefault: WorkflowModelDefault | null
  defaultMode: CommandMode
  initialDraft?: ComposerDraft | null
  onCreate: (request: import("@/types").CreateTriageTaskRequest) => Promise<void>
  onRuntimeControls: (taskId: string, updates: RuntimeControlUpdates) => Promise<TriageTaskDetail>
  onMissingModel: () => void
}

function PendingRun({message}: {message: string}) {
  return (
    <>
      <article className="triage-message triage-message-user" aria-label="你的消息">
        <div className="triage-message-author">
          <UserRound className="size-4" />
          <span>你</span>
        </div>
        <p>{message}</p>
      </article>
      <div aria-live="polite" className="triage-pending-run-card">
        <LoaderCircle className="size-4 shrink-0 animate-spin text-[var(--link)]" />
        <div>
          <p className="font-medium">Agent 正在处理</p>
          <p className="mt-1 text-xs text-muted-foreground">正在准备任务上下文，稍后显示执行活动。</p>
        </div>
      </div>
    </>
  )
}


export function Conversation({
  task,
  events,
  pendingMessage = null,
  loading,
  busy,
  connectionIssue,
  headerActions,
  onApproval,
  onCancel,
  onComplete,
  onRefresh,
  onRetry,
  onSend,
  createEnabled,
  missingModel,
  createReason,
  models,
  manualDefault,
  defaultMode,
  initialDraft,
  onCreate,
  onRuntimeControls,
  onMissingModel,
}: ConversationProps) {
  const endRef = useRef<HTMLDivElement>(null)
  const [focusComposerRequest, setFocusComposerRequest] = useState(0)
  const blocks = useMemo(() => toConversationBlocks(events, {
    taskStatus: task?.status,
    taskNode: task?.node,
  }), [events, task?.node, task?.status])
  const toolGroups = useMemo(() => {
    const groups = new Map<string, ToolBlock[]>()
    blocks.forEach((block: ConversationBlock) => {
      if (block.kind !== "tool") return
      const key = block.batchId || block.eventId
      groups.set(key, [...(groups.get(key) || []), block])
    })
    return groups
  }, [blocks])

  useEffect(() => {
    endRef.current?.scrollIntoView({block: "end"})
  }, [blocks.length, pendingMessage?.text, task?.task_id])

  if (!task) {
    const pendingCreate = pendingMessage?.taskId === null ? pendingMessage : null
    return (
      <section className="flex h-full min-h-[520px] flex-col bg-background">
        <div className="flex h-14 items-center justify-between border-b px-4">
          <h2 className="text-sm font-semibold">对话与活动</h2>
          {headerActions}
        </div>
        <div className="flex min-h-0 flex-1 flex-col">
          {pendingCreate ? (
            <div className="triage-conversation-scroll">
              <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 px-4 py-5 lg:px-6">
                <PendingRun message={pendingCreate.text} />
                <div ref={endRef} />
              </div>
            </div>
          ) : (
            // 文案吃掉剩余高度并居中,把输入区顶到页面底部——与有任务时的位置一致
            <div className="flex flex-1 items-center justify-center px-8 text-center">
              <div>
                <Bot className="mx-auto size-8 text-muted-foreground" aria-hidden="true" />
                <p className="mt-3 text-lg font-semibold">开始一项 Ops 工作</p>
                <p className="mt-1 text-sm text-muted-foreground">描述需求，Agent 会判断是直接回答、调查服务器还是生成任务产物。</p>
              </div>
            </div>
          )}
          <TriageComposerBar
            busy={busy}
            createEnabled={createEnabled}
            missingModel={missingModel}
            createReason={createReason}
            defaultMode={defaultMode}
            initialDraft={initialDraft}
            manualDefault={manualDefault}
            models={models}
            onCreate={onCreate}
            onMissingModel={onMissingModel}
            onRuntimeControls={onRuntimeControls}
            onSend={onSend}
            placeholder="描述任务需求"
            sendEnabled
            task={null}
          />
        </div>
      </section>
    )
  }

  const terminal = isTerminalTask(task.status)
  const pendingTextForTask = pendingMessage?.taskId === task.task_id
    ? pendingMessage.text
    : null
  const pendingForTask = pendingTextForTask !== null && !blocks.some((block) => (
    block.kind === "message" && block.role === "user" && block.text === pendingTextForTask
  ))
  const sendEnabled = canSendMessage(task)
  // A queued/running Agent is still processing even after the mutation that
  // created or resumed the task has returned. Keep the Composer locked until
  // the Agent reaches a user-action state.
  const composerBusy = busy || task.status === "queued" || task.status === "running"
    || task.status === "waiting_operation"
  const displayPhase = agentDisplayPhase(task.status)
  const retryEnabled = canRetryTask(task)
  const closed = task.status === "cancelled"
  const composerText = task.source === "auto"
    ? "自动任务为只读，不能追加人工消息"
    : closed
      ? "继续追问会在原报告上补充新发现"
      : retryEnabled
        ? "补充信息后继续任务，或直接重试"
        : task.status === "completed"
          ? "任务已完成，仍可继续对话或补充新的工作"
          : task.status === "waiting_approval"
            ? "任务正在等待审批"
            : task.status === "waiting_operation"
              ? "写操作执行中，Harness 跟踪到结束后 Agent 继续"
              : displayPhase === "can_input"
              ? "可以输入补充信息，Agent 会继续当前任务"
              : "Agent 正在处理，等待需要补充信息时可回复"

  function handleHandoffOption(option: string) {
    if (["补充告警信息", "补充任务信息", "补充信息"].includes(option)) {
      setFocusComposerRequest((value) => value + 1)
      return
    }
    void onSend(option)
  }

  return (
    <section className="flex h-full min-h-0 flex-col bg-background">
      <header className="flex min-h-14 items-center gap-3 border-b px-4 py-2">
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-sm font-semibold" title={task.initial_message}>
            {taskDisplayTitle(task.initial_message, task.display_title)}
          </h1>
          <div className="mt-0.5 flex items-center gap-2 text-xs text-muted-foreground">
            <span className={`triage-status triage-status-${task.status}`}>
              {agentDisplayLabel(task.status)}
            </span>
            <span>{task.source === "manual" ? "人工" : "自动"}</span>
            <span>更新于 {formatTime(task.updated_at)}</span>
          </div>
        </div>
        <Button
          aria-label="刷新当前任务"
          disabled={loading}
          onClick={onRefresh}
          size="icon"
          title="刷新当前任务"
          variant="ghost"
        >
          <RefreshCw className={loading ? "animate-spin" : undefined} />
        </Button>
        {task.source === "manual" && !terminal ? (
          <Button
            aria-label="取消任务"
            disabled={busy}
            onClick={() => void onCancel()}
            size="icon"
            title="取消任务"
            variant="ghost"
          >
            <Ban />
          </Button>
        ) : null}
        {task.source === "manual" && task.status === "waiting_input" ? (
          <Button
            aria-label="完成任务"
            disabled={busy}
            onClick={() => void onComplete()}
            size="sm"
            title="标记任务完成"
          >
            <CheckCircle2 data-icon="inline-start" />
            完成任务
          </Button>
        ) : null}
        {headerActions}
      </header>

      {connectionIssue ? (
        <Alert className="m-3 mb-0 rounded-md border-[#d69e2e]/50 bg-[#d69e2e]/10">
          <AlertCircle />
          <AlertDescription>{connectionIssue}，已保留当前内容并自动重试。</AlertDescription>
        </Alert>
      ) : null}
      {task.status === "waiting_input" ? (
        <div className="mx-4 mt-3 border-l-2 border-[#d69e2e] bg-[#d69e2e]/10 px-3 py-2 text-sm">
          {task.context_missing?.length
            ? "请补充任务目标；确认后 Agent 会继续。"
            : "Agent 已完成本轮回复。你可以继续对话，或将任务标记为完成。"}
        </div>
      ) : null}

      <div className="triage-conversation-scroll">
        <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 px-4 py-5 lg:px-6">
          {blocks.map((block) => {
            if (block.kind === "status") return null
            if (block.kind === "model_retry") {
              return (
                <p className="flex items-center gap-2 text-xs text-muted-foreground" key={block.eventId}>
                  <RotateCcw className="size-3.5 shrink-0" aria-hidden="true" />
                  模型请求暂时失败，{block.waitSeconds} 秒后自动重试（第 {block.attempt}/{block.maxAttempts} 次）
                </p>
              )
            }
            if (block.kind === "notice") {
              return <HarnessNotice key={block.eventId} text={block.text} variant={block.variant} />
            }
            if (block.kind === "finish") {
              return <FinishSummaryCard key={block.eventId} summary={block.summary} />
            }
            if (block.kind === "message") {
              if (!block.text) return null
              const user = block.role === "user"
              return (
                <article className={`triage-message ${user ? "triage-message-user" : "triage-message-agent"}`} key={block.eventId}>
                  <div className="triage-message-author">
                    {user ? <UserRound className="size-4" /> : <Bot className="size-4" />}
                    <span>{user ? "你" : "Agent"}</span>
                  </div>
                  <p>{block.text}</p>
                </article>
              )
            }
            if (block.kind === "tool") {
              const group = toolGroups.get(block.batchId || block.eventId) || [block]
              if (group[0].eventId !== block.eventId) return null
              if (group.length > 1) {
                return <ToolActivityGroup activities={group} key={block.eventId} taskId={task.task_id} />
              }
              return (
                <ToolActivity
                  activity={block}
                  key={block.eventId}
                  taskId={task.task_id}
                />
              )
            }
            if (block.kind === "task_type_question") {
              return (
                <article className="rounded-md border bg-muted/40 p-4" key={block.eventId}>
                  <p className="text-sm font-medium">{block.request.question || "请选择任务类型"}</p>
                  <div className="mt-3 flex flex-wrap gap-2">
                    {block.request.options.map((option) => (
                      <Button
                        disabled={busy || task.status !== "waiting_input"}
                        key={option}
                        onClick={() => void onSend(option)}
                        size="sm"
                        type="button"
                        variant="outline"
                      >
                        {option}
                      </Button>
                    ))}
                  </div>
                </article>
              )
            }
            if (block.kind === "agent_handoff") {
              return (
                <article className="rounded-md border border-[#6d8fd6]/50 bg-[#6d8fd6]/10 p-4" key={block.eventId}>
                  <div className="flex items-center gap-2 text-sm font-medium">
                    <Bot className="size-4" />
                    <span>Agent handoff</span>
                  </div>
                  <p className="mt-2 text-sm">{block.request.fromAgent} → {block.request.toAgent}</p>
                  {block.request.reason ? <p className="mt-1 whitespace-pre-wrap text-sm text-muted-foreground">{block.request.reason}</p> : null}
                  {block.request.missing.length ? <p className="mt-1 text-xs text-muted-foreground">待补信息：{block.request.missing.join("、")}</p> : null}
                  {block.decision ? (
                    <p className="mt-3 text-xs text-muted-foreground">已选择：{block.decision}</p>
                  ) : (
                    <div className="mt-3 flex flex-wrap gap-2">
                      {block.request.options.map((option) => (
                        <Button
                          disabled={busy}
                          key={option}
                          onClick={() => handleHandoffOption(option)}
                          size="sm"
                          type="button"
                          variant="outline"
                        >
                          {option}
                        </Button>
                      ))}
                    </div>
                  )}
                </article>
              )
            }
            if (block.kind === "kb_proposal_review") {
              return (
                <KbProposalCard
                  busy={busy}
                  key={block.eventId}
                  manual={task.source === "manual"}
                  onDecision={onApproval}
                  outcome={block.outcome}
                  request={block.request}
                />
              )
            }
            if (block.kind === "command_review") {
              return (
                <CommandReviewCard
                  busy={busy}
                  key={block.eventId}
                  manual={task.source === "manual"}
                  onDecision={onApproval}
                  outcome={block.outcome}
                  request={block}
                />
              )
            }
            if (block.kind === "operation_review") {
              return (
                <OperationReviewCard
                  busy={busy}
                  decidedBy={block.decidedBy}
                  execution={block.execution}
                  key={block.eventId}
                  manual={task.source === "manual"}
                  onDecision={onApproval}
                  outcome={block.outcome}
                  request={block.request}
                />
              )
            }
            if (block.kind === "kb_review") {
              return (
                <KbReviewCard
                  busy={busy}
                  key={block.eventId}
                  manual={task.source === "manual"}
                  onDecision={onApproval}
                  outcome={block.outcome}
                  request={block.request}
                />
              )
            }
            if (block.kind === "budget_review") {
              return (
                <BudgetReviewCard
                  busy={busy}
                  key={block.eventId}
                  manual={task.source === "manual"}
                  onDecision={onApproval}
                  outcome={block.outcome}
                  request={block.request}
                />
              )
            }
            if (block.kind === "worklog_review") {
              return (
                <WorklogReviewCard
                  busy={busy}
                  key={block.eventId}
                  manual={task.source === "manual"}
                  onDecision={onApproval}
                  outcome={block.outcome}
                  request={block.request}
                />
              )
            }
            if (block.kind === "report_review") {
              return (
                <ReportGateCard
                  busy={busy}
                  key={block.eventId}
                  manual={task.source === "manual"}
                  onDecision={onApproval}
                  outcome={block.outcome}
                  request={block.request}
                />
              )
            }
            if (block.kind === "approval") {
              return (
                <ApprovalCard
                  busy={busy}
                  decided={block.decided}
                  key={block.eventId}
                  manual={task.source === "manual"}
                  onDecision={(approvalId, approved) => onApproval(
                    approvalId,
                    approved ? "approve" : "reject",
                  )}
                  request={block.request}
                />
              )
            }
            if (block.kind === "error") {
              return (
                <Alert className="flex items-center gap-3 rounded-md" key={block.eventId} variant="destructive">
                  <AlertCircle className="size-4 shrink-0" />
                  <AlertDescription className="min-w-0 flex-1 whitespace-pre-wrap">{block.message || "任务执行失败"}</AlertDescription>
                  {retryEnabled ? (
                    <Button
                      aria-label="重试失败任务"
                      className="shrink-0"
                      disabled={busy}
                      onClick={() => void onRetry()}
                      size="icon"
                      title="重试失败任务"
                      variant="outline"
                    >
                      <RotateCcw className={busy ? "animate-spin" : undefined} />
                    </Button>
                  ) : null}
                </Alert>
              )
            }
            return null
          })}
          {pendingForTask ? <PendingRun message={pendingTextForTask} /> : null}
          {blocks.length === 0 && !pendingForTask ? (
            <p className="py-16 text-center text-sm text-muted-foreground">等待任务事件</p>
          ) : null}
          <div ref={endRef} />
        </div>
      </div>

        <TriageComposerBar
          busy={composerBusy}
        createEnabled={createEnabled}
        missingModel={missingModel}
        createReason={createReason}
        defaultMode={defaultMode}
        manualDefault={manualDefault}
        models={models}
        onCreate={onCreate}
        onMissingModel={onMissingModel}
        onRuntimeControls={onRuntimeControls}
        onSend={onSend}
        focusRequest={focusComposerRequest}
        placeholder={composerText}
        sendEnabled={sendEnabled}
        task={task}
      />
    </section>
  )
}
