import { useCallback, useEffect, useMemo, useState } from "react"
import { ArrowLeft, Check, FileText, History, Link2, LoaderCircle, MessageSquareText, Pencil, RefreshCw, Unlink } from "lucide-react"
import { Link, useNavigate, useParams } from "react-router-dom"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { MarkdownView } from "@/components/MarkdownView"
import { PageHeader } from "@/components/PageHeader"
import { WorkItemFormDialog, WorkItemStatusBadge, formPatch } from "@/components/workitems/WorkItemBits"
import { formatTime } from "@/lib/api"
import { listTriageTasks } from "@/lib/triageApi"
import { linkWorkItem, loadWorkItem, unlinkWorkItem, updateWorkItem } from "@/lib/workItemsApi"
import type { LinkedTask, RunSummary, WorkItemDetailResponse, WorkItemLinkKind, WorkItemStatus } from "@/lib/workItemsTypes"
import { PRIORITY_LABELS, SOURCE_LABELS, STATUS_LABELS, STATUS_ORDER, dateRangeLabel, historyLabel, isOverdue } from "@/lib/workItemsView"
import type { TriageTask } from "@/types"

const OUTCOME_LABELS: Record<string, string> = {
  succeeded: "成功", failed: "失败", partial: "部分成功", noop: "无需执行", not_executed: "未执行",
}
const DECISION_LABELS: Record<string, string> = { approve: "批准", revise: "要求修改", reject: "拒绝" }

function RunFacts({ run }: { run: RunSummary }) {
  if (run.missing) return <p className="mt-1 text-xs text-muted-foreground">排查记录 {run.run_id} 不存在或已移走</p>
  return <div className="mt-2 space-y-1.5 text-xs">
    <p className="text-muted-foreground">
      {run.report_ref ? <Link className="text-[var(--link)] hover:underline" to={`/reports/troubleshoot/${encodeURIComponent(run.run_id)}`}><FileText className="mr-1 inline size-3.5" />排查报告</Link> : "尚无报告"}
      <span className="ml-2 font-mono">{run.run_id}</span>
      {run.verdict ? <span className="ml-2">结论 {run.verdict}</span> : null}
    </p>
    {run.artifacts?.length ? <p className="break-all text-muted-foreground">产物：{run.artifacts.join("、")}</p> : null}
    {run.write_operations?.length ? <ul className="space-y-0.5">{run.write_operations.map((operation) => <li key={operation.id}>
      写操作：{operation.title || operation.id} · {OUTCOME_LABELS[operation.outcome || ""] || operation.outcome || "进行中"}{operation.approver ? ` · ${operation.approver} 批准` : ""}{operation.oplog ? " · 已留 oplog" : ""}
    </li>)}</ul> : null}
  </div>
}

function TaskLink({ task }: { task: LinkedTask }) {
  if (task.missing) return <p className="text-sm text-muted-foreground">任务 {task.task_id} 不存在或已清理</p>
  const approvals = task.approvals || []
  return <div>
    <Link className="text-sm font-medium hover:underline" to={`/triage?task=${encodeURIComponent(task.task_id)}`}>{task.title}</Link>
    <p className="mt-0.5 text-xs text-muted-foreground">{task.status} · {formatTime(task.created_at)}{task.created_by ? ` · ${task.created_by}` : ""}</p>
    {task.run_id ? <RunFacts run={task} /> : null}
    {approvals.length ? <p className="mt-1.5 text-xs text-muted-foreground">审批：{approvals.map((item) => `${item.kind || "审批"}${item.decision ? ` ${DECISION_LABELS[item.decision] || item.decision}` : " 待处理"}`).join("；")}</p> : null}
  </div>
}

function TaskPickerDialog({ open, onOpenChange, onPick }: { open: boolean; onOpenChange: (open: boolean) => void; onPick: (kind: WorkItemLinkKind, ref: string) => Promise<void> }) {
  const [tasks, setTasks] = useState<TriageTask[]>([])
  const [runId, setRunId] = useState("")
  const [busy, setBusy] = useState("")
  const [error, setError] = useState("")

  useEffect(() => {
    if (!open) return
    setRunId("")
    setError("")
    const controller = new AbortController()
    listTriageTasks(controller.signal).then((result) => setTasks(result.items.filter((task) => task.source === "manual" || task.run_id)))
      .catch((reason) => { if ((reason as Error).name !== "AbortError") setError((reason as Error).message) })
    return () => controller.abort()
  }, [open])

  async function pick(kind: WorkItemLinkKind, ref: string, key: string) {
    setBusy(key)
    setError("")
    try {
      await onPick(kind, ref)
      onOpenChange(false)
    } catch (reason) {
      setError((reason as Error).message)
    } finally {
      setBusy("")
    }
  }

  return <Dialog onOpenChange={onOpenChange} open={open}>
    <DialogContent className="flex max-h-[85vh] max-w-[min(680px,calc(100vw-2rem))] flex-col gap-0 overflow-hidden p-0">
      <DialogHeader className="shrink-0 border-b px-6 pb-4 pt-6">
        <DialogTitle>挂载排查</DialogTitle>
        <DialogDescription>选一个 Agent-Ops 任务；命令行建的排查记录（ts-/bf-/al-）填 run ID。任务的报告、产物、审批与写操作随之可见。</DialogDescription>
      </DialogHeader>
      <form className="flex shrink-0 gap-2 border-b px-6 py-3" onSubmit={(event) => { event.preventDefault(); if (runId.trim()) void pick("troubleshoot", runId.trim(), "run") }}>
        <div className="flex-1"><Label className="sr-only" htmlFor="link-run-id">排查记录 run ID</Label><Input id="link-run-id" onChange={(event) => setRunId(event.target.value)} placeholder="排查记录 run ID，如 ts-20261022-1000-abcd" value={runId} /></div>
        <Button disabled={!runId.trim() || Boolean(busy)} type="submit" variant="outline">{busy === "run" ? <LoaderCircle className="animate-spin" data-icon="inline-start" /> : <Link2 data-icon="inline-start" />}挂排查记录</Button>
      </form>
      {error ? <p className="px-6 pt-3 text-sm text-destructive" role="alert">{error}</p> : null}
      <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2">
        {tasks.map((task) => <button className="flex w-full items-start gap-2 rounded-md px-3 py-2 text-left hover:bg-muted/50 disabled:opacity-60" disabled={Boolean(busy)} key={task.task_id} onClick={() => void pick("task", task.task_id, task.task_id)} type="button">
          <span className="min-w-0 flex-1"><span className="block text-sm">{task.display_title || task.initial_message.slice(0, 80)}</span><span className="block text-xs text-muted-foreground">{formatTime(task.created_at)} · {task.status}</span></span>
          {busy === task.task_id ? <LoaderCircle className="mt-0.5 size-4 animate-spin" /> : null}
        </button>)}
      </div>
    </DialogContent>
  </Dialog>
}

export default function WorkItemDetail() {
  const { itemId = "" } = useParams()
  const navigate = useNavigate()
  const [data, setData] = useState<WorkItemDetailResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const [notice, setNotice] = useState("")
  const [note, setNote] = useState("")
  const [mutating, setMutating] = useState("")
  const [editOpen, setEditOpen] = useState(false)
  const [linkOpen, setLinkOpen] = useState(false)

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true)
    try {
      setData(await loadWorkItem(itemId, signal))
      setError("")
    } catch (reason) {
      if ((reason as Error).name !== "AbortError") setError((reason as Error).message)
    } finally {
      setLoading(false)
    }
  }, [itemId])

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => controller.abort()
  }, [load])

  async function mutate(key: string, action: () => Promise<unknown>, message: string) {
    setMutating(key)
    setError("")
    setNotice("")
    try {
      await action()
      setNotice(message)
      await load()
    } catch (reason) {
      setError((reason as Error).message)
    } finally {
      setMutating("")
    }
  }

  const history = useMemo(() => [...(data?.item.history || [])].reverse(), [data?.item.history])

  if (loading && !data) return <div className="py-20 text-center text-muted-foreground">正在读取工作项</div>
  if (!data) return <div><Button asChild size="sm" variant="ghost"><Link to="/work-items"><ArrowLeft data-icon="inline-start" />返回工作项</Link></Button><p className="mt-10 text-center text-destructive">{error || "工作项不存在"}</p></div>

  const { item, plan, links } = data
  const overdue = isOverdue(item, data.today)
  const explicitWorklogs = new Set(item.links.filter((link) => link.kind === "worklog").map((link) => link.ref))

  function setStatus(status: WorkItemStatus) {
    void mutate(`status:${status}`, () => updateWorkItem(item.id, { status, ...(note.trim() ? { note: note.trim() } : {}) }), `状态已改为「${STATUS_LABELS[status]}」`)
      .then(() => setNote(""))
  }

  function startTask() {
    const message = item.description ? `${item.title}\n\n${item.description}` : item.title
    navigate("/triage", { state: { triageDraft: { message: message.slice(0, 4000), work_item: { id: item.id, title: item.title } } } })
  }

  return <div>
    <Button asChild className="mb-4" size="sm" variant="ghost"><Link to={plan ? `/work-items/plans/${plan.id}` : "/work-items"}><ArrowLeft data-icon="inline-start" />{plan ? `返回计划「${plan.title}」` : "返回工作项"}</Link></Button>
    <PageHeader
      meta={`${SOURCE_LABELS[item.source]} · 创建于 ${formatTime(item.created_at)}${item.created_by ? ` · ${item.created_by}` : ""}`}
      title={item.title}
      actions={<>
        <Button onClick={startTask} variant="outline"><MessageSquareText data-icon="inline-start" />发起 Agent-Ops 任务</Button>
        <Button onClick={() => setEditOpen(true)} variant="outline"><Pencil data-icon="inline-start" />编辑</Button>
        <Button aria-label="刷新" disabled={loading} onClick={() => void load()} size="icon" title="刷新" variant="outline"><RefreshCw className={loading ? "animate-spin" : undefined} /></Button>
      </>}
    />
    {error ? <Alert className="mb-5" variant="destructive"><AlertDescription>{error}</AlertDescription></Alert> : null}
    {notice ? <p aria-live="polite" className="mb-5 text-sm text-[#247a56]">{notice}</p> : null}
    <div className="grid gap-8 xl:grid-cols-[minmax(0,1fr)_340px]">
      <main className="space-y-8">
        <section className="border-y py-4">
          <div className="flex flex-wrap items-center gap-2">
            {STATUS_ORDER.map((status) => <Button disabled={Boolean(mutating) || status === item.status} key={status} onClick={() => setStatus(status)} size="sm" variant={status === item.status ? "default" : "outline"}>
              {mutating === `status:${status}` ? <LoaderCircle className="animate-spin" data-icon="inline-start" /> : status === item.status ? <Check data-icon="inline-start" /> : null}{STATUS_LABELS[status]}
            </Button>)}
          </div>
          <Input aria-label="状态备注" className="mt-3" maxLength={500} onChange={(event) => setNote(event.target.value)} placeholder="改状态时附一句备注（选填），记进历史" value={note} />
        </section>
        {item.description ? <section><h2 className="mb-2 text-sm font-semibold">描述</h2><MarkdownView content={item.description} /></section> : null}
        <section>
          <div className="flex items-center justify-between gap-3"><h2 className="text-sm font-semibold">排查任务与记录</h2><Button onClick={() => setLinkOpen(true)} size="sm" variant="outline"><Link2 data-icon="inline-start" />挂载排查</Button></div>
          {links.tasks.length || links.troubleshoot.length ? <ul className="mt-3 space-y-4">
            {links.tasks.map((task) => <li className="flex gap-3 border-l-2 pl-3" key={task.task_id}>
              <div className="min-w-0 flex-1"><TaskLink task={task} /></div>
              <Button aria-label="解除挂载" disabled={Boolean(mutating)} onClick={() => void mutate(`unlink:${task.task_id}`, () => unlinkWorkItem(item.id, "task", task.task_id), "已解除任务挂载")} size="icon" title="解除挂载" variant="ghost"><Unlink /></Button>
            </li>)}
            {links.troubleshoot.map((run) => <li className="flex gap-3 border-l-2 pl-3" key={run.run_id}>
              <div className="min-w-0 flex-1"><p className="text-sm font-medium">{run.trigger || run.run_id}</p><RunFacts run={run} /></div>
              <Button aria-label="解除挂载" disabled={Boolean(mutating)} onClick={() => void mutate(`unlink:${run.run_id}`, () => unlinkWorkItem(item.id, "troubleshoot", run.run_id), "已解除排查记录挂载")} size="icon" title="解除挂载" variant="ghost"><Unlink /></Button>
            </li>)}
          </ul> : <p className="mt-3 text-sm text-muted-foreground">还没有挂载排查。可从这里发起 Agent-Ops 任务，建成即自动挂上。</p>}
        </section>
        <section>
          <h2 className="text-sm font-semibold">工作日志</h2>
          {links.worklogs.length ? <ul className="mt-3 space-y-2">{links.worklogs.map((entry) => <li className="flex items-start gap-3 border-l-2 pl-3 text-sm" key={entry.ref}>
            <div className="min-w-0 flex-1">
              <p className="flex flex-wrap items-center gap-2"><span className="font-mono text-xs text-muted-foreground">{entry.ref.replace("#", " #")}</span>{entry.status ? <Badge variant="outline">{entry.status}</Badge> : null}{entry.derived ? <Badge variant="secondary">随任务自动归属</Badge> : null}</p>
              <p className="mt-0.5 break-words">{entry.missing ? "条目不存在" : entry.title}</p>
            </div>
            {explicitWorklogs.has(entry.ref) ? <Button aria-label="解除挂载" disabled={Boolean(mutating)} onClick={() => void mutate(`unlink:${entry.ref}`, () => unlinkWorkItem(item.id, "worklog", entry.ref), "已解除工作日志挂载")} size="icon" title="解除挂载" variant="ghost"><Unlink /></Button> : null}
          </li>)}</ul> : <p className="mt-3 text-sm text-muted-foreground">还没有工作日志。在「工作日志」页给条目挂上本工作项，或补录时直接选它。</p>}
        </section>
      </main>
      <aside className="space-y-6 border-l pl-0 xl:pl-6">
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 border-y py-4 text-sm xl:border-t-0">
          <dt className="text-muted-foreground">状态</dt><dd><WorkItemStatusBadge status={item.status} /></dd>
          <dt className="text-muted-foreground">优先级</dt><dd>{PRIORITY_LABELS[item.priority]}</dd>
          <dt className="text-muted-foreground">负责人</dt><dd>{item.assignee || "未指定"}</dd>
          <dt className="text-muted-foreground">时间</dt><dd className={overdue ? "font-medium text-destructive" : ""}>{dateRangeLabel(item.start_date, item.due_date) || "未排期"}{overdue ? " · 逾期" : ""}</dd>
          {plan ? <><dt className="text-muted-foreground">计划</dt><dd><Link className="hover:underline" to={`/work-items/plans/${plan.id}`}>{plan.title}</Link>{item.plan_node ? <span className="block text-xs text-muted-foreground">{item.plan_node}{item.kind === "acceptance" ? " · 产物验证" : ""}</span> : null}</dd></> : null}
          {item.source_ref ? <><dt className="text-muted-foreground">来源</dt><dd className="break-all">{item.source === "alert" ? <Link className="hover:underline" to={`/alerts/${encodeURIComponent(item.source_ref)}`}>{item.source_ref}</Link> : item.source_ref}</dd></> : null}
          {item.closed_at ? <><dt className="text-muted-foreground">关闭于</dt><dd>{formatTime(item.closed_at)}</dd></> : null}
          <dt className="text-muted-foreground">ID</dt><dd className="break-all font-mono text-xs">{item.id}</dd>
        </dl>
        <section>
          <h2 className="flex items-center gap-2 text-sm font-semibold"><History className="size-4" />历史</h2>
          <ol className="mt-3 space-y-3">{history.map((event, index) => <li className="border-l-2 pl-3 text-xs" key={`${event.ts}-${index}`}>
            <p className="text-muted-foreground">{formatTime(event.ts)}{event.by ? ` · ${event.by}` : ""}</p>
            <p className="mt-0.5 break-words">{historyLabel(event)}</p>
            {event.note ? <p className="mt-0.5 break-words text-muted-foreground">备注：{event.note}</p> : null}
          </li>)}</ol>
        </section>
      </aside>
    </div>
    <WorkItemFormDialog
      initial={item}
      onOpenChange={setEditOpen}
      onSubmit={async (value) => { await updateWorkItem(item.id, formPatch(value)); setNotice("已保存"); await load() }}
      open={editOpen}
      title="编辑工作项"
    />
    <TaskPickerDialog
      onOpenChange={setLinkOpen}
      onPick={async (kind, ref) => {
        await linkWorkItem(item.id, kind, ref)
        setNotice("已挂载")
        await load()
      }}
      open={linkOpen}
    />
  </div>
}
