import { type ReactNode, useEffect, useState } from "react"
import { Ban, BookOpen, CheckCircle2, ChevronDown, ClipboardList, Download, ExternalLink, FileCode2, FileText, NotebookPen, PanelRightClose, ServerCog, TerminalSquare } from "lucide-react"
import { Link } from "react-router-dom"
import { MarkdownView } from "@/components/MarkdownView"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { ScrollArea } from "@/components/ui/scroll-area"
import { normalizeKnowledgePath, routeFromPath, splitKnowledgeDocs } from "@/lib/knowledgeView"
import {
  consultedKnowledgeDocs,
  executedCommands,
  modelUsageSummary,
  toConversationBlocks,
} from "@/lib/triageEvents"
import { REASONING_LABELS } from "@/lib/modelSettingsApi"
import { FAILURE_KIND_LABELS, agentDisplayLabel, canSendMessage, taskTypeLabel, triageTargetLabel } from "@/lib/triageView"
import { listTaskArtifacts, loadTaskArtifact, taskArtifactDownloadUrl } from "@/lib/triageApi"
import type { TaskArtifact, TriageEvent, TriageTask } from "@/types"
import { TaskWorkItem } from "./TaskWorkItem"
import { ToolDetailDialog } from "./ToolDetailDialog"


function CollapsibleSection({
  title,
  icon,
  open,
  onToggle,
  children,
}: {
  title: string
  icon?: ReactNode
  open: boolean
  onToggle: () => void
  children: ReactNode
}) {
  return (
    <section className="triage-context-section">
      <button
        aria-expanded={open}
        className="triage-context-section-toggle"
        onClick={onToggle}
        type="button"
      >
        {icon}
        <span>{title}</span>
        <ChevronDown className="triage-context-chevron" aria-hidden="true" />
      </button>
      {open ? <div className="pt-2">{children}</div> : null}
    </section>
  )
}


export function TaskContext({
  task,
  events,
  busy = false,
  onGenerateKb,
  onGenerateWorklog,
  onToggleCollapse,
}: {
  task: TriageTask | null
  events: TriageEvent[]
  busy?: boolean
  onGenerateKb?: () => Promise<void>
  onGenerateWorklog?: () => Promise<void>
  onToggleCollapse?: () => void
}) {
  const [artifacts, setArtifacts] = useState<TaskArtifact[]>([])
  const [preview, setPreview] = useState<{artifact: TaskArtifact; content: string} | null>(null)
  const [openSections, setOpenSections] = useState<Record<string, boolean>>({
    task: true,
    workItem: true,
    knowledge: true,
    activities: false,
    commands: false,
    unexecuted: false,
    artifacts: true,
    report: true,
    kb: true,
    worklog: true,
    error: true,
  })

  useEffect(() => {
    if (!task) { setArtifacts([]); return }
    const controller = new AbortController()
    listTaskArtifacts(task.task_id, controller.signal)
      .then((value) => setArtifacts(value.items))
      .catch((reason: Error) => { if (reason.name !== "AbortError") setArtifacts([]) })
    return () => controller.abort()
  }, [task?.task_id, task?.updated_at])

  useEffect(() => {
    setOpenSections({
      task: true,
      workItem: true,
      knowledge: true,
      activities: false,
      commands: false,
      unexecuted: false,
      artifacts: true,
      report: true,
      kb: true,
      worklog: true,
      error: true,
    })
  }, [task?.task_id])

  if (!task) {
    return (
      <div className="triage-context-shell">
        <div className="triage-context-toolbar">
          <div className="flex min-w-0 items-center gap-2">
            <ServerCog className="size-4 text-muted-foreground" aria-hidden="true" />
            <h2>任务上下文</h2>
          </div>
          {onToggleCollapse ? (
            <Button aria-label="收起任务上下文" onClick={onToggleCollapse} size="icon" title="收起任务上下文" variant="ghost">
              <PanelRightClose />
            </Button>
          ) : null}
        </div>
        <div className="flex min-h-0 flex-1 items-center justify-center p-6 text-sm text-muted-foreground">
          选择任务后查看上下文
        </div>
      </div>
    )
  }

  const blocks = toConversationBlocks(events, {
    taskStatus: task.status,
    taskNode: task.node,
  })
  const commands = executedCommands(blocks)
  const usage = modelUsageSummary(events)
  const newRuntime = task.harness_tier === "autonomous" || task.harness_tier === "guided"
  // 旧任务需与事件流轨迹合并，保留部署前后完整的已查阅文档。
  const consultedDocs = [...new Set([
    ...consultedKnowledgeDocs(blocks),
    ...task.consulted_docs,
  ])]
  const visibleConsultedDocs = consultedDocs.slice(-5)
  const hiddenConsultedCount = consultedDocs.length - visibleConsultedDocs.length
  const split = splitKnowledgeDocs(task.matched_doc, visibleConsultedDocs)
  const docGroups = [
    { label: "路由命中且已查阅", docs: split.both },
    { label: "路由命中", docs: split.matchedOnly },
    { label: "已查阅", docs: split.consultedOnly },
  ].filter((group) => group.docs.length)
  const activities = blocks.filter((block) => block.kind === "tool")
  const unexecutedActivities = activities.filter((item) => (
    item.status === "rejected" || item.status === "skipped" || item.status === "needs_input" || item.status === "approval_pending"
  ))
  const latestPlaybook = [...activities].reverse().find((item) => (
    item.actionId.includes("playbook") || item.actionId.includes("/")
  ))
  const canTriggerArtifact = canSendMessage(task)
  const legacyWorklogEvent = [...events].reverse().find((event) => (
    event.type === "assistant_message" && event.payload.artifact === "worklog"
  ))
  const effectiveWorklogCursor = task.worklog_cursor || legacyWorklogEvent?.event_id || null
  const recordedWorklogEntries = task.worklog_entries || (legacyWorklogEvent ? 1 : 0)
  const cursorIndex = effectiveWorklogCursor
    ? events.findIndex((event) => event.event_id === effectiveWorklogCursor)
    : -1
  const hasUnloggedWork = events.slice(cursorIndex + 1).some((event) => {
    if (!["user_message", "assistant_message", "tool_call", "tool_result"].includes(event.type)) return false
    if (event.payload.artifact === "worklog") return false
    const message = String(event.payload.message || "").trim()
    return message !== "请生成或更新本次任务的 worklog。"
      && message !== "请记录本次排查 worklog。"
  })

  return (
    <div className="triage-context-shell">
      <div className="triage-context-toolbar">
        <div className="flex min-w-0 items-center gap-2">
          <ServerCog className="size-4 text-muted-foreground" aria-hidden="true" />
          <h2>任务上下文</h2>
        </div>
        {onToggleCollapse ? (
          <Button aria-label="收起任务上下文" onClick={onToggleCollapse} size="icon" title="收起任务上下文" variant="ghost">
            <PanelRightClose />
          </Button>
        ) : null}
      </div>
      <ScrollArea className="min-h-0 flex-1">
      <div className="p-5">
        <CollapsibleSection
          icon={<ServerCog aria-hidden="true" />}
          onToggle={() => setOpenSections((current) => ({...current, task: !current.task}))}
          open={openSections.task}
          title="任务信息"
        >
        <dl className="triage-context-facts mt-2">
          <div>
            <dt>状态</dt>
            <dd>
              {agentDisplayLabel(task.status)}
              {(task.status === "failed" || task.status === "kb_failed")
                && task.failure_kind
                && task.failure_kind !== "knowledge" ? (
                <span className="mt-0.5 block text-xs font-normal text-muted-foreground">
                  {FAILURE_KIND_LABELS[task.failure_kind]}
                </span>
              ) : null}
            </dd>
          </div>
          <div><dt>来源</dt><dd>{task.source === "manual" ? "人工任务" : "自动任务"}</dd></div>
          <div><dt>任务类型</dt><dd>{taskTypeLabel(task.task_type)}</dd></div>
          {task.active_agent ? <div><dt>当前 Agent</dt><dd>{task.active_agent}</dd></div> : null}
          {task.loop_phase ? <div><dt>Loop 阶段</dt><dd>{task.loop_phase}</dd></div> : null}
          <div><dt>平台</dt><dd>{triageTargetLabel(task.platform)}</dd></div>
          <div>
            <dt>范围</dt>
            <dd>
              {task.scope === "cluster" ? "集群" : "单节点"}
              {task.node ? (
                <span className="mt-0.5 block font-mono text-xs font-normal text-muted-foreground">
                  {task.scope === "cluster" ? `入口 ${task.node}` : task.node}
                </span>
              ) : (
                <span className="mt-0.5 block text-xs font-normal text-muted-foreground">
                  节点待确认
                </span>
              )}
            </dd>
          </div>
          <div>
            <dt>模型</dt>
            <dd>
              {task.model_title || task.model_name || "未记录"}
              {task.model_title && task.model_name ? (
                <span className="mt-0.5 block break-all font-mono text-xs font-normal text-muted-foreground">
                  {task.model_name}
                </span>
              ) : null}
            </dd>
          </div>
          <div>
            <dt>思考强度</dt>
            <dd>
              {task.reasoning_effort
                ? REASONING_LABELS[task.reasoning_effort]
                : "未记录"}
            </dd>
          </div>
          <div>
            <dt>运行时</dt>
            <dd>{newRuntime ? "Agent 运行时" : "旧版 Agent(只读)"}</dd>
          </div>
          {usage ? (
            <div>
              <dt>模型请求</dt>
              <dd>
                {usage.requests} 次
                <span className="mt-0.5 block text-xs font-normal text-muted-foreground">
                  缓存命中 {usage.hitRate === null ? "未知" : `${Math.round(usage.hitRate * 100)}%`}
                  {" · "}输入 {usage.promptTokens.toLocaleString()} tokens
                </span>
              </dd>
            </div>
          ) : null}
          <div><dt>{newRuntime ? "节点命令" : "诊断次数"}</dt><dd>{task.diagnostic_calls}</dd></div>
          <div><dt>工具轮次</dt><dd>{task.tool_turns}</dd></div>
        </dl>
        </CollapsibleSection>

        <CollapsibleSection
          icon={<ClipboardList aria-hidden="true" />}
          onToggle={() => setOpenSections((current) => ({...current, workItem: !current.workItem}))}
          open={openSections.workItem}
          title="工作项"
        >
          <TaskWorkItem task={task} />
        </CollapsibleSection>

        <CollapsibleSection
          icon={<BookOpen aria-hidden="true" />}
          onToggle={() => setOpenSections((current) => ({...current, knowledge: !current.knowledge}))}
          open={openSections.knowledge}
          title="知识与 Playbook"
        >
          {/* 路由命中与已查阅分区呈现:此前两者同图标同字号逐行铺开,只能靠顺序猜 */}
          {docGroups.map((group) => (
            <div className="mt-3" key={group.label}>
              <p className="text-[11px] text-muted-foreground">{group.label}</p>
              {group.docs.map((doc) => (
                <div className="mt-1 flex gap-2" key={doc}>
                  <BookOpen className="mt-0.5 size-4 shrink-0 text-[var(--link)]" aria-hidden="true" />
                  <Link
                    className="min-w-0 break-all font-mono text-xs text-[var(--link)] hover:underline"
                    to={routeFromPath(normalizeKnowledgePath(doc))}
                  >{doc}</Link>
                </div>
              ))}
            </div>
          ))}
          {hiddenConsultedCount ? (
            <p className="mt-2 text-xs text-muted-foreground">
              另有 {hiddenConsultedCount} 篇已省略
            </p>
          ) : null}
          {!docGroups.length ? (
            <p className="mt-3 text-sm text-muted-foreground">尚未命中知识文档</p>
          ) : null}
          {latestPlaybook ? (
            <p className="mt-2 break-all font-mono text-xs text-muted-foreground">
              {latestPlaybook.actionId}
            </p>
          ) : null}
        </CollapsibleSection>

        <CollapsibleSection
          icon={<ServerCog aria-hidden="true" />}
          onToggle={() => setOpenSections((current) => ({...current, activities: !current.activities}))}
          open={openSections.activities}
          title="操作记录"
        >
          {activities.length ? (
            <div className="mt-3 flex flex-col gap-2">
              {activities.slice(-5).reverse().map((activity) => (
                <div className="border-l-2 pl-3 text-xs" key={activity.eventId}>
                  <p className="break-all font-mono">{activity.label || activity.actionId || "登记动作"}</p>
                  <p className="mt-0.5 text-muted-foreground">
                    {activity.scope || "未指定范围"} · {
                      activity.status === "done"
                        ? "完成"
                        : activity.status === "partial"
                          ? "部分完成"
                        : activity.status === "failed"
                          ? "失败"
                          : activity.status === "rejected"
                            ? "未执行·已拒绝"
                          : activity.status === "skipped"
                            ? "未执行·已跳过"
                          : activity.status === "needs_input"
                            ? "需要补充"
                          : activity.status === "approval_pending"
                            ? "等待审批"
                          : activity.status === "interrupted"
                            ? "已中断"
                            : activity.status === "pending"
                              ? "Agent运行中"
                            : "执行中"
                    }
                  </p>
                </div>
              ))}
            </div>
          ) : <p className="mt-3 text-sm text-muted-foreground">还没有诊断活动</p>}
        </CollapsibleSection>

        <CollapsibleSection
          icon={<TerminalSquare aria-hidden="true" />}
          onToggle={() => setOpenSections((current) => ({...current, commands: !current.commands}))}
          open={openSections.commands}
          title={`已执行命令${commands.length ? ` (${commands.length})` : ""}`}
        >
          {commands.length ? (
            <ul className="triage-commands">
              {commands.map((item) => (
                <li key={item.key}>
                  {item.activity ? (
                    <ToolDetailDialog activity={item.activity} taskId={task.task_id}>
                      <button className="w-full text-left" type="button">
                        <span className="triage-command-scope">{item.node || "未指定节点"}</span>
                        <code>{item.command}</code>
                      </button>
                    </ToolDetailDialog>
                  ) : (
                    <>
                      <span className="triage-command-scope">{item.node || "未指定节点"}</span>
                      <code>{item.command}</code>
                    </>
                  )}
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-3 text-sm text-muted-foreground">
              本任务未在节点上执行命令
            </p>
          )}
        </CollapsibleSection>

        <CollapsibleSection
          icon={<Ban aria-hidden="true" />}
          onToggle={() => setOpenSections((current) => ({...current, unexecuted: !current.unexecuted}))}
          open={openSections.unexecuted}
          title={`未执行调用${unexecutedActivities.length ? ` (${unexecutedActivities.length})` : ""}`}
        >
          {unexecutedActivities.length ? (
            <div className="mt-3 flex flex-col gap-2">
              {unexecutedActivities.slice(-8).reverse().map((activity) => (
                <div className="border-l-2 border-muted-foreground/40 pl-3 text-xs" key={activity.eventId}>
                  <p className="break-all font-mono">{activity.label || activity.actionId || "工具调用"}</p>
                  <p className="mt-0.5 text-muted-foreground">
                    {activity.status === "skipped" ? "已跳过" : activity.status === "rejected" ? "已拒绝" : activity.status === "needs_input" ? "需要补充" : "等待审批"}
                    {activity.skipReason ? ` · ${activity.skipReason}` : ""}
                  </p>
                </div>
              ))}
            </div>
          ) : <p className="mt-3 text-sm text-muted-foreground">没有未执行调用</p>}
        </CollapsibleSection>

        <CollapsibleSection
          icon={<FileCode2 aria-hidden="true" />}
          onToggle={() => setOpenSections((current) => ({...current, artifacts: !current.artifacts}))}
          open={openSections.artifacts}
          title="任务产物"
        >
          {artifacts.length ? (
            <div className="space-y-2">
              {artifacts.map((artifact) => (
                <div className="rounded border bg-background p-2" key={artifact.filename}>
                  <p className="break-all font-mono text-xs">{artifact.filename}</p>
                  <p className="mt-0.5 text-[11px] text-muted-foreground">
                    {artifact.type.toUpperCase()} · {artifact.size.toLocaleString()} B
                  </p>
                  <div className="mt-2 flex gap-2">
                    <Button
                      onClick={() => void loadTaskArtifact(task.task_id, artifact.filename)
                        .then((content) => setPreview({artifact, content}))}
                      size="sm"
                      variant="outline"
                    >预览</Button>
                    <Button asChild size="sm" variant="ghost">
                      <a href={taskArtifactDownloadUrl(task.task_id, artifact.filename)}>
                        <Download data-icon="inline-start" />下载
                      </a>
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          ) : <p className="text-xs text-muted-foreground">尚未生成 md、sh 或 py 产物。</p>}
        </CollapsibleSection>

        <CollapsibleSection
          icon={<FileText aria-hidden="true" />}
          onToggle={() => setOpenSections((current) => ({...current, report: !current.report}))}
          open={openSections.report}
          title="执行报告"
        >
            <div>
                {task.report_path ? (
                  <>
                    <p className="mt-1 break-all font-mono text-[11px] text-muted-foreground">{task.report_path}</p>
                    <Button asChild className="mt-2" size="sm" variant="outline">
                      <Link
                        to={`/triage/${encodeURIComponent(task.task_id)}/report`}
                        rel="noopener noreferrer"
                        target="_blank"
                      >
                        <ExternalLink data-icon="inline-start" />
                        打开报告
                      </Link>
                    </Button>
                  </>
                ) : (
                  <>
                    <p className="mt-1 text-xs text-muted-foreground">首次执行服务器查询或命令后自动生成并持续更新。</p>
                  </>
                )}
              </div>
        </CollapsibleSection>
        {task.kb_doc ? (
          <CollapsibleSection
            icon={<BookOpen aria-hidden="true" />}
            onToggle={() => setOpenSections((current) => ({...current, kb: !current.kb}))}
            open={openSections.kb}
            title="知识库草稿"
          >
            <div>
                <p className="mt-1 break-all font-mono text-[11px] text-muted-foreground">{task.kb_doc}</p>
                <Button asChild className="mt-2" size="sm" variant="outline">
                  <Link
                    to={routeFromPath(normalizeKnowledgePath(task.kb_doc))}
                    rel="noopener noreferrer"
                    target="_blank"
                  >
                    <ExternalLink data-icon="inline-start" />
                    打开草稿
                  </Link>
                </Button>
              </div>
          </CollapsibleSection>
        ) : task.source === "manual" && !task.kb_declined ? (
          <CollapsibleSection
            icon={<BookOpen aria-hidden="true" />}
            onToggle={() => setOpenSections((current) => ({...current, kb: !current.kb}))}
            open={openSections.kb}
            title="知识库草稿"
          >
            <div>
                <p className="mt-1 text-xs text-muted-foreground">
                  {task.report_path
                    ? "尚未生成草稿，点击后根据报告整理草稿并等待确认。"
                    : "尚未生成草稿；执行过服务器操作并形成报告后方可生成。"}
                </p>
                <Button
                  className="mt-2"
                  disabled={busy || !onGenerateKb || !task.report_path || !canTriggerArtifact}
                  onClick={() => void onGenerateKb?.()}
                  size="sm"
                  variant="outline"
                >
                  <BookOpen data-icon="inline-start" />
                  生成草稿
                </Button>
              </div>
          </CollapsibleSection>
        ) : null}
        {task.source === "manual" && task.manual_artifacts ? (
          <CollapsibleSection
            icon={<NotebookPen aria-hidden="true" />}
            onToggle={() => setOpenSections((current) => ({...current, worklog: !current.worklog}))}
            open={openSections.worklog}
            title="工作日志"
          >
            <div>
                {recordedWorklogEntries > 0 && !hasUnloggedWork ? (
                  <p className="mt-2 flex items-center gap-1.5 text-xs text-[#247a56]">
                    <CheckCircle2 className="size-3.5" aria-hidden="true" />
                    已记录到 worklog
                  </p>
                ) : (
                  <>
                    <p className="mt-1 text-xs text-muted-foreground">由 LLM 概括尚未记录的任务内容，确认后追加写入。</p>
                    <Button
                      className="mt-2"
                      disabled={busy || !onGenerateWorklog || !canTriggerArtifact}
                      onClick={() => void onGenerateWorklog?.()}
                      size="sm"
                      variant="outline"
                    >
                      <NotebookPen data-icon="inline-start" />
                      {recordedWorklogEntries > 0 ? "更新 worklog" : "生成 worklog"}
                    </Button>
                  </>
                )}
              </div>
          </CollapsibleSection>
        ) : null}
        {task.kb_declined ? (
          <CollapsibleSection
            icon={<BookOpen aria-hidden="true" />}
            onToggle={() => setOpenSections((current) => ({...current, kb: !current.kb}))}
            open={openSections.kb}
            title="未落档知识库"
          >
            <div>
              <p className="mt-1 text-xs text-muted-foreground">
                草稿经人工确认后拒绝落档，本任务不沉淀知识库。
              </p>
            </div>
          </CollapsibleSection>
        ) : null}
        {task.error ? (
          <CollapsibleSection
            icon={<TerminalSquare aria-hidden="true" />}
            onToggle={() => setOpenSections((current) => ({...current, error: !current.error}))}
            open={openSections.error}
            title="任务错误"
          >
            <h3 className="text-sm font-medium text-destructive">任务错误</h3>
            <p className="mt-1 whitespace-pre-wrap text-xs text-muted-foreground">{task.error}</p>
          </CollapsibleSection>
        ) : null}
        <Dialog onOpenChange={(open) => { if (!open) setPreview(null) }} open={Boolean(preview)}>
          <DialogContent className="max-h-[85vh] max-w-4xl overflow-auto">
            <DialogHeader>
              <DialogTitle>{preview?.artifact.filename}</DialogTitle>
              <DialogDescription>任务产物在线预览</DialogDescription>
            </DialogHeader>
            {preview?.artifact.type === "md"
              ? <MarkdownView content={preview.content} />
              : <pre className="whitespace-pre-wrap break-words rounded bg-[var(--pre-bg)] p-4 text-xs text-[var(--pre-fg)]">{preview?.content}</pre>}
          </DialogContent>
        </Dialog>
      </div>
      </ScrollArea>
    </div>
  )
}
