import { useCallback, useEffect, useMemo, useState } from "react"
import { CalendarRange, ChevronLeft, ChevronRight, ClipboardList, Eye, EyeOff, FileText, FolderOpen, Link2, LoaderCircle, NotebookPen, Plus, RefreshCw, RotateCcw, Trash2, WandSparkles, X } from "lucide-react"
import { Link } from "react-router-dom"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { MarkdownView } from "@/components/MarkdownView"
import { PageHeader } from "@/components/PageHeader"
import { MatchSuggestionsDialog, WorkItemPickerDialog } from "@/components/workitems/WorkItemBits"
import { createWorklog, deleteWeeklyReport, generateWeeklyReport, getWeeklyReportJob, listWeeklyReportJobs, listWeeklyReports, loadWeeklyReportPreview, loadWeeklyReportSettings, loadWorklogDetail, loadWorklogs, regenerateWeeklyReport, revealWeeklyReport, setWorklogWeeklyReportExcluded } from "@/lib/worklogApi"
import { zonedDate } from "@/lib/envTime"
import { filterWeeklyReports } from "@/lib/worklogView"
import { linkWorkItem, listWorkItems, loadWeeklyBuckets, loadWorklogWorkItems } from "@/lib/workItemsApi"
import type { WeeklyBucketsResponse, WorkItemSummary, WorklogWorkItemRef } from "@/lib/workItemsTypes"
import { BUCKET_LABELS, groupBuckets, worklogRef } from "@/lib/workItemsView"
import type { WeeklyReportDocumentPreview, WeeklyReportItem, WeeklyReportJob, WeeklyReportSettings, WorklogCreateInput, WorklogDay, WorklogDetailResponse, WorklogEntry } from "@/types"

const WORKLOG_STATUSES = ["DONE", "WIP", "BLOCKED"] as const
const WORKLOG_CATEGORIES = ["BUGFIX", "FEATURE", "RESEARCH", "OPS", "DOC", "MEETING"] as const
const WEEKLY_REPORT_STAGES = ["事实聚合", "初稿", "规则审查", "二次改写", "生成DOCX"] as const
const NO_WORK_ITEM = "__none__"

function WeeklyReportProgress({ job }: { job: WeeklyReportJob | null }) {
  if (!job) return null
  const active = job.status === "queued" || job.status === "running"
  return <div aria-live="polite" className="mt-4 rounded-md border bg-muted/20 p-3 text-xs" role="status">
    <div className="flex items-center justify-between gap-3">
      <p className="font-medium">{active ? "周报后台生成中" : job.status === "completed" ? "周报生成完成" : "周报生成失败"}</p>
      {active ? <LoaderCircle className="size-4 shrink-0 animate-spin" /> : null}
    </div>
    <ol className="mt-2 space-y-1.5">
      {WEEKLY_REPORT_STAGES.map((stage) => {
        const completed = job.completed_stages.includes(stage)
        const current = active && job.current_stage === stage
        return <li className={completed || current ? "text-foreground" : "text-muted-foreground"} key={stage}>
          <span className="mr-1 inline-block w-3 text-center">{completed ? "✓" : current ? "…" : "○"}</span>
          {stage}{current ? "中" : completed ? "已完成" : ""}
        </li>
      })}
    </ol>
    {job.error ? <p className="mt-2 break-words text-destructive">{job.error}</p> : null}
  </div>
}

function addDays(value: string, amount: number): string {
  const date = new Date(`${value}T12:00:00Z`)
  date.setUTCDate(date.getUTCDate() + amount)
  return date.toISOString().slice(0, 10)
}

function displayDate(value: string): string {
  const [year, month, day] = value.split("-")
  return `${year}年${month}月${day}日`
}

function rangeDate(value: string): string { return value.replaceAll("-", "/") }
function compactDate(value: string): string { return value.slice(5).replace("-", "/") }

function envToday(): string {
  return zonedDate()
}

type WorklogSelection = { date: string; entry: WorklogEntry }

function Entry({ busy, date, entry, workItem, onAttach, onOpen, onToggleWeeklyReport }: {
  busy: boolean
  date: string
  entry: WorklogEntry
  workItem?: WorklogWorkItemRef
  onAttach: (date: string, entry: WorklogEntry) => void
  onOpen: (date: string, entry: WorklogEntry) => void
  onToggleWeeklyReport: (date: string, entry: WorklogEntry) => void
}) {
  const excluded = entry.weekly_report_excluded
  const actionLabel = excluded ? "恢复到周报" : "在周报中隐藏"
  return <article className={`border-l-2 pl-3 ${excluded ? "border-[#b7791f]/60" : "border-border"}`}>
    <div className="flex items-start gap-2">
      <button aria-label={`查看工作日志详情：${entry.title}`} className="min-w-0 flex-1 rounded-sm p-1 text-left transition-colors hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" onClick={() => onOpen(date, entry)} title="查看工作日志详情" type="button">
        <div className="flex items-start gap-2">
          <Badge className="shrink-0" variant={entry.status === "DONE" ? "secondary" : "outline"}>{entry.status}</Badge>
          <Badge className="shrink-0" variant="outline">{entry.category}</Badge>
          {excluded ? <Badge className="shrink-0 border-[#b7791f]/40 bg-[#fff8eb] text-[#8a5a12] dark:bg-[#b7791f]/10 dark:text-[#e7b55d]" variant="outline">周报已忽略</Badge> : null}
          <h3 className={`min-w-0 flex-1 text-sm font-medium ${excluded ? "text-muted-foreground line-through" : ""}`}>{entry.title}</h3>
        </div>
        <dl className="mt-2 grid gap-x-4 gap-y-1 text-xs text-muted-foreground sm:grid-cols-[auto_1fr]">
          <dt>产出</dt><dd className="break-words">{entry.output || "-"}</dd>
          <dt>关联</dt><dd className="break-words">{entry.related || "-"}</dd>
          {entry.process.length ? <><dt>过程</dt><dd>{entry.process.join("；")}</dd></> : null}
        </dl>
      </button>
      {workItem
        ? <Link className="mt-1.5 inline-flex max-w-[40%] shrink-0 items-center gap-1 truncate rounded border px-1.5 py-0.5 text-xs text-muted-foreground hover:text-foreground" title={workItem.derived ? `随任务自动归属：${workItem.title}` : `工作项：${workItem.title}`} to={`/work-items/${workItem.id}`}><ClipboardList className="size-3 shrink-0" /><span className="truncate">{workItem.title}</span></Link>
        : <Button aria-label={`挂到工作项：${entry.title}`} className="mt-1 size-7 shrink-0" onClick={() => onAttach(date, entry)} size="icon" title="挂到工作项" variant="ghost"><Link2 /></Button>}
      <Button aria-label={`${actionLabel}：${entry.title}`} className="mt-1 size-7 shrink-0" disabled={busy} onClick={() => onToggleWeeklyReport(date, entry)} size="icon" title={actionLabel} variant="ghost">
        {busy ? <LoaderCircle className="animate-spin" /> : excluded ? <Eye /> : <EyeOff />}
      </Button>
    </div>
  </article>
}

function DaySection({ day, workItems, onAttach, onOpenWorklog, onToggleWeeklyReport, togglingKey }: {
  day: WorklogDay
  workItems: Record<string, WorklogWorkItemRef>
  onAttach: (date: string, entry: WorklogEntry) => void
  onOpenWorklog: (date: string, entry: WorklogEntry) => void
  onToggleWeeklyReport: (date: string, entry: WorklogEntry) => void
  togglingKey: string
}) {
  return <section className="border-b py-5 last:border-b-0">
    <div className="flex items-baseline gap-3">
      <h2 className="text-base font-semibold">{displayDate(day.date)}</h2>
      <span className="text-xs text-muted-foreground">星期{day.weekday}</span>
      {day.entries.length ? <span className="text-xs text-muted-foreground">{day.entries.length} 条</span> : null}
    </div>
    {day.entries.length
      ? <div className="mt-4 flex flex-col gap-4">{day.entries.map((entry) => <Entry busy={togglingKey === `${day.date}:${entry.entry_index}`} date={day.date} entry={entry} key={`${day.date}-${entry.entry_index}-${entry.title}`} onAttach={onAttach} onOpen={onOpenWorklog} onToggleWeeklyReport={onToggleWeeklyReport} workItem={workItems[worklogRef(day.date, entry.entry_index)]} />)}</div>
      : <p className="mt-3 text-sm text-muted-foreground">当天没有工作日志。</p>}
  </section>
}

function WorklogDetailDialog({ detail, error, loading, onOpenChange, selection }: {
  detail: WorklogDetailResponse | null
  error: string
  loading: boolean
  onOpenChange: (open: boolean) => void
  selection: WorklogSelection | null
}) {
  const entry = detail?.entry ?? selection?.entry
  return <Dialog onOpenChange={onOpenChange} open={Boolean(selection)}>
    <DialogContent className="flex max-h-[90vh] max-w-[min(920px,calc(100vw-2rem))] flex-col gap-0 overflow-hidden p-0">
      {entry && selection ? <>
        <DialogHeader className="shrink-0 border-b px-6 pb-4 pt-6">
          <DialogTitle>工作日志详情</DialogTitle>
          <DialogDescription>{displayDate(selection.date)} · {entry.source}</DialogDescription>
        </DialogHeader>
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-6 py-5">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={entry.status === "DONE" ? "secondary" : "outline"}>{entry.status}</Badge>
            <Badge variant="outline">{entry.category}</Badge>
            {entry.weekly_report_excluded ? <Badge className="border-[#b7791f]/40 bg-[#fff8eb] text-[#8a5a12] dark:bg-[#b7791f]/10 dark:text-[#e7b55d]" variant="outline">周报已忽略</Badge> : null}
          </div>
          <h2 className="mt-3 break-words text-base font-semibold">{entry.title}</h2>
          <dl className="mt-5 grid gap-x-5 gap-y-3 border-y py-4 text-sm sm:grid-cols-[auto_1fr]">
            <dt className="text-muted-foreground">产出</dt><dd className="break-words">{entry.output || "-"}</dd>
            <dt className="text-muted-foreground">关联</dt><dd className="break-words">{entry.related || "-"}</dd>
            <dt className="text-muted-foreground">过程</dt><dd>{entry.process.length ? <ol className="list-decimal space-y-1 pl-5">{entry.process.map((item, index) => <li key={`${index}-${item}`}>{item.replace(/^\d+\.\s*/, "")}</li>)}</ol> : "-"}</dd>
          </dl>
          {loading ? <div className="flex items-center gap-2 py-8 text-sm text-muted-foreground"><LoaderCircle className="size-4 animate-spin" />正在读取完整工作记录</div> : null}
          {error ? <Alert className="mt-5" variant="destructive"><AlertDescription>{error}</AlertDescription></Alert> : null}
          {!loading && detail?.report_markdown ? <section className="mt-6 border-t pt-5">
            <div className="mb-4">
              <h3 className="text-sm font-semibold">完整排查报告</h3>
              {detail.report_path ? <p className="mt-1 break-all text-xs text-muted-foreground">{detail.report_path}</p> : null}
            </div>
            <div className="overflow-x-auto"><MarkdownView content={detail.report_markdown} /></div>
          </section> : null}
        </div>
      </> : null}
    </DialogContent>
  </Dialog>
}

function WorklogCreateDialog({
  open,
  onOpenChange,
  onCreate,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  onCreate: (input: WorklogCreateInput) => Promise<void>
}) {
  const [date, setDate] = useState(envToday())
  const [status, setStatus] = useState<WorklogCreateInput["status"]>("DONE")
  const [category, setCategory] = useState<WorklogCreateInput["category"]>("OPS")
  const [title, setTitle] = useState("")
  const [output, setOutput] = useState("")
  const [related, setRelated] = useState("")
  const [processText, setProcessText] = useState("")
  const [workItemId, setWorkItemId] = useState(NO_WORK_ITEM)
  const [workItems, setWorkItems] = useState<WorkItemSummary[]>([])
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState("")
  const process = processText.split("\n").map((item) => item.trim()).filter(Boolean)

  useEffect(() => {
    if (!open) return
    setDate(envToday())
    setStatus("DONE")
    setCategory("OPS")
    setTitle("")
    setOutput("")
    setRelated("")
    setProcessText("")
    setWorkItemId(NO_WORK_ITEM)
    setFormError("")
    const controller = new AbortController()
    listWorkItems({}, controller.signal).then((result) => setWorkItems(result.items)).catch(() => setWorkItems([]))
    return () => controller.abort()
  }, [open])

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (process.length > 8) {
      setFormError("过程最多填写 8 个步骤。")
      return
    }
    setSaving(true)
    setFormError("")
    try {
      await onCreate({
        date,
        status,
        category,
        title: title.trim(),
        output: output.trim() || "-",
        related: related.trim() || "-",
        process,
        ...(workItemId !== NO_WORK_ITEM ? { work_item_id: workItemId } : {}),
      })
      onOpenChange(false)
    } catch (reason) {
      setFormError((reason as Error).message)
    } finally {
      setSaving(false)
    }
  }

  return <Dialog onOpenChange={onOpenChange} open={open}>
    <DialogContent className="max-h-[90vh] max-w-[min(680px,calc(100vw-2rem))] overflow-y-auto">
      <DialogHeader>
        <DialogTitle>添加工作日志</DialogTitle>
        <DialogDescription className="sr-only">添加指定日期的结构化工作日志</DialogDescription>
      </DialogHeader>
      <form className="space-y-5" onSubmit={submit}>
        <div className="grid gap-4 sm:grid-cols-3">
          <div className="space-y-2"><Label htmlFor="worklog-date">日期</Label><Input id="worklog-date" max={envToday()} onChange={(event) => setDate(event.target.value)} required type="date" value={date} /></div>
          <div className="space-y-2"><Label>状态</Label><Select onValueChange={(value: WorklogCreateInput["status"]) => setStatus(value)} value={status}><SelectTrigger aria-label="工作日志状态"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>{WORKLOG_STATUSES.map((item) => <SelectItem key={item} value={item}>{item}</SelectItem>)}</SelectGroup></SelectContent></Select></div>
          <div className="space-y-2"><Label>类型</Label><Select onValueChange={(value: WorklogCreateInput["category"]) => setCategory(value)} value={category}><SelectTrigger aria-label="工作日志类型"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>{WORKLOG_CATEGORIES.map((item) => <SelectItem key={item} value={item}>{item}</SelectItem>)}</SelectGroup></SelectContent></Select></div>
        </div>
        <div className="space-y-2"><Label htmlFor="worklog-title">标题</Label><Input id="worklog-title" maxLength={300} onChange={(event) => setTitle(event.target.value)} placeholder="简明描述本次工作" required value={title} /></div>
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-2"><Label htmlFor="worklog-output">产出</Label><Input id="worklog-output" onChange={(event) => setOutput(event.target.value)} placeholder="文件、报告或其他产出，无则留空" value={output} /></div>
          <div className="space-y-2"><Label htmlFor="worklog-related">关联</Label><Input id="worklog-related" onChange={(event) => setRelated(event.target.value)} placeholder="相关文件或上游依赖，无则留空" value={related} /></div>
        </div>
        <div className="space-y-2"><Label>挂到工作项（选填）</Label><Select onValueChange={setWorkItemId} value={workItemId}><SelectTrigger aria-label="挂到工作项"><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value={NO_WORK_ITEM}>不挂</SelectItem>{workItems.map((item) => <SelectItem key={item.id} value={item.id}>{item.plan_node ? `${item.plan_node} · ` : ""}{item.title}</SelectItem>)}</SelectGroup></SelectContent></Select></div>
        <div className="space-y-2"><Label htmlFor="worklog-process">过程（选填）</Label><Textarea className="min-h-32 resize-y" id="worklog-process" onChange={(event) => setProcessText(event.target.value)} placeholder={"每行填写一个步骤，最多 8 条"} value={processText} /><p className="text-xs text-muted-foreground">当前 {process.length} 条</p></div>
        {formError ? <p className="text-sm text-destructive" role="alert">{formError}</p> : null}
        <DialogFooter><Button disabled={saving || !date || !title.trim()} type="submit">{saving ? <LoaderCircle className="animate-spin" data-icon="inline-start" /> : <Plus data-icon="inline-start" />}{saving ? "正在添加" : "添加日志"}</Button></DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}

function ReportDialog({ preview, onOpenChange, onReveal, onRegenerate, onDelete, busy }: { preview: WeeklyReportDocumentPreview | null; onOpenChange: (open: boolean) => void; onReveal: () => void; onRegenerate: () => void; onDelete: () => void; busy: boolean }) {
  return <Dialog onOpenChange={onOpenChange} open={Boolean(preview)}>
    <DialogContent className="max-h-[90vh] max-w-[min(960px,calc(100vw-2rem))] overflow-y-auto p-5 sm:p-7">
      {preview ? <>
        <DialogHeader className="text-center sm:text-center">
          <DialogTitle className="text-xl">项　目　周　报</DialogTitle>
          <DialogDescription>（时间：{displayDate(preview.report.start_date)}至{displayDate(preview.report.end_date)}）</DialogDescription>
        </DialogHeader>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[680px] table-fixed border-collapse border border-foreground/40 text-sm">
            <tbody>
              <tr><th className="border border-foreground/40 bg-muted/40 px-3 py-2 text-left" colSpan={4}>一、项目信息</th></tr>
              <tr><th className="w-[14%] border border-foreground/40 px-3 py-2 text-left">用户</th><td className="w-[40%] border border-foreground/40 px-3 py-2">{preview.customer}</td><th className="w-[14%] border border-foreground/40 px-3 py-2 text-left">合同编号</th><td className="w-[32%] border border-foreground/40 px-3 py-2">{preview.contract_number}</td></tr>
              <tr><th className="border border-foreground/40 px-3 py-2 text-left">项目名称</th><td className="border border-foreground/40 px-3 py-2">{preview.project_name}</td><th className="border border-foreground/40 px-3 py-2 text-left">项目经理</th><td className="border border-foreground/40 px-3 py-2">{preview.project_manager}</td></tr>
              <tr><th className="border border-foreground/40 bg-muted/40 px-3 py-2 text-left" colSpan={4}>二、本周工作总结</th></tr>
              <tr><td className="border border-foreground/40 px-5 py-3" colSpan={4}><ol className="list-decimal space-y-2 pl-5 leading-6">{preview.summary.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ol></td></tr>
              <tr><th className="border border-foreground/40 bg-muted/40 px-3 py-2 text-left" colSpan={4}>三、下周待办</th></tr>
              <tr><td className="border border-foreground/40 px-5 py-3" colSpan={4}><ol className="list-decimal space-y-2 pl-5 leading-6">{preview.todos.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ol></td></tr>
            </tbody>
          </table>
        </div>
        <DialogFooter className="sm:justify-between">
          <Button disabled={busy} onClick={onDelete} variant="destructive"><Trash2 data-icon="inline-start" />删除</Button>
          <div className="flex flex-col-reverse gap-2 sm:flex-row">
            <Button disabled={busy} onClick={onReveal} variant="outline"><FolderOpen data-icon="inline-start" />在文件夹中显示</Button>
            <Button disabled={busy} onClick={onRegenerate} variant="outline">{busy ? <LoaderCircle className="animate-spin" data-icon="inline-start" /> : <RotateCcw data-icon="inline-start" />}{busy ? "正在重新生成" : "重新生成"}</Button>
          </div>
        </DialogFooter>
      </> : null}
    </DialogContent>
  </Dialog>
}

function ExistingReport({ report, onOpen, onReveal, loading }: { report: WeeklyReportItem; onOpen: () => void; onReveal: () => void; loading: boolean }) {
  return <li className="flex items-center gap-3 border-b py-3 last:border-b-0">
    <FileText className="size-4 shrink-0 text-muted-foreground" />
    <div className="min-w-0 flex-1">
      <button className="block max-w-full truncate text-left text-sm font-medium text-[var(--link)] hover:underline disabled:opacity-60" disabled={loading} onClick={onOpen} type="button" title={report.filename}>{compactDate(report.start_date)} - {compactDate(report.end_date)}</button>
      <p className="mt-0.5 text-[11px] text-muted-foreground">更新于 {report.updated_at.slice(0, 16).replace("T", " ")}</p>
    </div>
    <button aria-label="在文件夹中显示周报" className="text-muted-foreground hover:text-foreground disabled:opacity-50" disabled={loading} onClick={onReveal} title="在文件夹中显示" type="button"><FolderOpen className="size-4" /></button>
  </li>
}

function HistoryReportsDialog({
  open,
  onOpenChange,
  reports,
  previewLoading,
  onOpenReport,
  onRevealReport,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  reports: WeeklyReportItem[]
  previewLoading: string
  onOpenReport: (report: WeeklyReportItem) => void
  onRevealReport: (report: WeeklyReportItem) => void
}) {
  const [filterStart, setFilterStart] = useState("")
  const [filterEnd, setFilterEnd] = useState("")
  const invalidRange = Boolean(filterStart && filterEnd && filterStart > filterEnd)
  const filteredReports = useMemo(
    () => filterWeeklyReports(reports, filterStart, filterEnd),
    [filterEnd, filterStart, reports],
  )

  function clearFilter() {
    setFilterStart("")
    setFilterEnd("")
  }

  return <Dialog onOpenChange={onOpenChange} open={open}>
    <DialogContent className="flex max-h-[90vh] max-w-[min(760px,calc(100vw-2rem))] flex-col gap-0 overflow-hidden p-0">
      <DialogHeader className="shrink-0 border-b px-6 pb-4 pt-6">
        <DialogTitle>全部历史周报</DialogTitle>
        <DialogDescription>共 {reports.length} 份周报，可按周报周期筛选。</DialogDescription>
      </DialogHeader>
      <div className="shrink-0 border-b bg-muted/25 px-6 py-4">
        <div className="grid gap-3 sm:grid-cols-[1fr_auto_1fr_auto] sm:items-end">
          <div className="space-y-1.5">
            <label className="text-xs font-medium text-muted-foreground" htmlFor="report-filter-start">起始日期</label>
            <Input id="report-filter-start" onChange={(event) => setFilterStart(event.target.value)} type="date" value={filterStart} />
          </div>
          <span className="hidden pb-2 text-sm text-muted-foreground sm:block">至</span>
          <div className="space-y-1.5">
            <label className="text-xs font-medium text-muted-foreground" htmlFor="report-filter-end">结束日期</label>
            <Input id="report-filter-end" onChange={(event) => setFilterEnd(event.target.value)} type="date" value={filterEnd} />
          </div>
          <Button aria-label="清空时间筛选" disabled={!filterStart && !filterEnd} onClick={clearFilter} size="icon" title="清空时间筛选" variant="outline"><X /></Button>
        </div>
        <p className={`mt-2 text-xs ${invalidRange ? "text-destructive" : "text-muted-foreground"}`}>
          {invalidRange ? "起始日期不能晚于结束日期。" : `当前显示 ${filteredReports.length} 份周报`}
        </p>
      </div>
      <div className="min-h-0 flex-1 touch-pan-y overflow-y-auto overscroll-contain px-6 pb-6">
        {filteredReports.length
          ? <ul>{filteredReports.map((report) => <ExistingReport key={report.filename} loading={previewLoading === report.filename} onOpen={() => onOpenReport(report)} onReveal={() => onRevealReport(report)} report={report} />)}</ul>
          : <div className="py-12 text-center text-sm text-muted-foreground">当前时间范围内没有周报。</div>}
      </div>
    </DialogContent>
  </Dialog>
}

export default function Worklog() {
  const [settings, setSettings] = useState<WeeklyReportSettings | null>(null)
  const [startDate, setStartDate] = useState("")
  const [endDate, setEndDate] = useState("")
  const [days, setDays] = useState<WorklogDay[]>([])
  const [reports, setReports] = useState<WeeklyReportItem[]>([])
  const [weeklyJob, setWeeklyJob] = useState<WeeklyReportJob | null>(null)
  const [dialogPreview, setDialogPreview] = useState<WeeklyReportDocumentPreview | null>(null)
  const [historyOpen, setHistoryOpen] = useState(false)
  const [worklogCreateOpen, setWorklogCreateOpen] = useState(false)
  const [returnToHistoryAfterPreview, setReturnToHistoryAfterPreview] = useState(false)
  const [previewLoading, setPreviewLoading] = useState("")
  const [revealingReport, setRevealingReport] = useState("")
  const [loading, setLoading] = useState(true)
  const [generating, setGenerating] = useState(false)
  const [reportMutating, setReportMutating] = useState(false)
  const [togglingWorklog, setTogglingWorklog] = useState("")
  const [selectedWorklog, setSelectedWorklog] = useState<WorklogSelection | null>(null)
  const [worklogDetail, setWorklogDetail] = useState<WorklogDetailResponse | null>(null)
  const [worklogDetailLoading, setWorklogDetailLoading] = useState(false)
  const [worklogDetailError, setWorklogDetailError] = useState("")
  const [error, setError] = useState("")
  const [notice, setNotice] = useState("")
  const [workItemRefs, setWorkItemRefs] = useState<Record<string, WorklogWorkItemRef>>({})
  const [weeklyBuckets, setWeeklyBuckets] = useState<WeeklyBucketsResponse | null>(null)
  const [attachTarget, setAttachTarget] = useState<WorklogSelection | null>(null)
  const [matchOpen, setMatchOpen] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    loadWeeklyReportSettings(controller.signal).then((result) => {
      setSettings(result)
      setStartDate(result.current_start_date)
      setEndDate(result.current_end_date)
    }).catch((reason) => { if ((reason as Error).name !== "AbortError") setError((reason as Error).message) })
    return () => controller.abort()
  }, [])

  const load = useCallback(async (signal?: AbortSignal) => {
    if (!startDate || !endDate) return
    setLoading(true)
    try {
      const [worklog, reportList, jobList] = await Promise.all([
        loadWorklogs(startDate, endDate, signal),
        listWeeklyReports(signal),
        listWeeklyReportJobs(startDate, endDate, signal),
      ])
      setDays(worklog.days)
      setReports(reportList.items)
      // 工作项是附加信息:读不到不影响日志与周报
      const [refs, buckets] = await Promise.all([
        loadWorklogWorkItems(startDate, endDate, signal).catch(() => null),
        loadWeeklyBuckets(startDate, endDate, signal).catch(() => null),
      ])
      setWorkItemRefs(refs?.refs || {})
      setWeeklyBuckets(buckets)
      setWeeklyJob(
        jobList.items.find((item) => item.status === "queued" || item.status === "running")
        ?? jobList.items.find((item) => item.status === "failed")
        ?? null,
      )
      setError("")
    } catch (reason) {
      if ((reason as Error).name !== "AbortError") setError((reason as Error).message)
    } finally { setLoading(false) }
  }, [endDate, startDate])

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => controller.abort()
  }, [load])

  useEffect(() => {
    if (!selectedWorklog) {
      setWorklogDetail(null)
      setWorklogDetailLoading(false)
      setWorklogDetailError("")
      return
    }
    const controller = new AbortController()
    setWorklogDetail(null)
    setWorklogDetailLoading(true)
    setWorklogDetailError("")
    loadWorklogDetail(
      selectedWorklog.date,
      selectedWorklog.entry.entry_index,
      controller.signal,
    ).then(setWorklogDetail).catch((reason) => {
      if ((reason as Error).name !== "AbortError") setWorklogDetailError((reason as Error).message)
    }).finally(() => {
      if (!controller.signal.aborted) setWorklogDetailLoading(false)
    })
    return () => controller.abort()
  }, [selectedWorklog])

  useEffect(() => {
    if (!weeklyJob || (weeklyJob.status !== "queued" && weeklyJob.status !== "running")) return
    let cancelled = false
    const poll = async () => {
      try {
        const updated = await getWeeklyReportJob(weeklyJob.job_id)
        if (cancelled) return
        if (updated.status === "completed") {
          if (updated.report) {
            const report = updated.report
            setReports((current) => [report, ...current.filter((item) => item.filename !== report.filename)].sort((a, b) => b.start_date.localeCompare(a.start_date)))
            if (weeklyJob.operation === "regenerate") {
              try {
                const preview = await loadWeeklyReportPreview(report.filename)
                if (!cancelled) setDialogPreview(preview)
              } catch (reason) {
                if (!cancelled) setError((reason as Error).message)
              }
            }
          }
          setNotice("周报已在后台生成完成，可查看最新周报。")
          setGenerating(false)
          setReportMutating(false)
          setWeeklyJob(null)
          return
        }
        if (updated.status === "failed") {
          setError(updated.error || "周报后台生成失败")
          setGenerating(false)
          setReportMutating(false)
        }
        setWeeklyJob(updated)
      } catch (reason) {
        if (!cancelled) setError((reason as Error).message)
      }
    }
    void poll()
    const timer = window.setInterval(() => void poll(), 1500)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [weeklyJob?.job_id, weeklyJob?.status])

  function moveWeek(amount: number) {
    setStartDate(addDays(startDate, amount * 7))
    setEndDate(addDays(endDate, amount * 7))
  }

  function handleWorklogDetailOpenChange(open: boolean) {
    if (open) return
    setSelectedWorklog(null)
  }

  async function openReport(report: WeeklyReportItem, returnToHistory = false) {
    setPreviewLoading(report.filename)
    setError("")
    try {
      const preview = await loadWeeklyReportPreview(report.filename)
      if (returnToHistory) {
        setHistoryOpen(false)
        setReturnToHistoryAfterPreview(true)
      }
      setDialogPreview(preview)
    }
    catch (reason) { setError((reason as Error).message) }
    finally { setPreviewLoading("") }
  }

  function handlePreviewOpenChange(open: boolean) {
    if (open) return
    setDialogPreview(null)
    if (returnToHistoryAfterPreview) {
      setReturnToHistoryAfterPreview(false)
      setHistoryOpen(true)
    }
  }

  async function handleGenerate() {
    setGenerating(true)
    setError("")
    try {
      const job = await generateWeeklyReport(startDate, endDate)
      setWeeklyJob(job)
      setGenerating(false)
      setNotice("周报已提交后台生成，可切换页面；中台保持运行即可继续。")
    } catch (reason) { setError((reason as Error).message) }
    finally { setGenerating(false) }
  }

  async function handleCreateWorklog(input: WorklogCreateInput) {
    const result = await createWorklog(input)
    setNotice(`已记录 ${displayDate(result.date)} 的工作日志：${result.entry.title}`)
    if (result.date >= startDate && result.date <= endDate) await load()
  }

  async function handleToggleWeeklyReport(date: string, entry: WorklogEntry) {
    const excluded = !entry.weekly_report_excluded
    const key = `${date}:${entry.entry_index}`
    setTogglingWorklog(key)
    setError("")
    setNotice("")
    try {
      await setWorklogWeeklyReportExcluded(date, entry.entry_index, excluded)
      setDays((current) => current.map((day) => day.date !== date ? day : {
        ...day,
        entries: day.entries.map((item) => item.entry_index === entry.entry_index ? {
          ...item,
          weekly_report_excluded: excluded,
        } : item),
      }))
    } catch (reason) {
      setError((reason as Error).message)
    } finally {
      setTogglingWorklog("")
    }
  }

  async function handleRegenerateReport() {
    if (!dialogPreview || !window.confirm("确认重新生成该周期周报？当前 DOCX 将被新内容替换。")) return
    setReportMutating(true)
    setError("")
    try {
      const job = await regenerateWeeklyReport(dialogPreview.report.filename)
      setWeeklyJob(job)
      setNotice("周报已提交后台重新生成，可切换页面；中台保持运行即可继续。")
    } catch (reason) {
      setReportMutating(false)
      setError((reason as Error).message)
    }
  }

  async function handleRevealReport(report: WeeklyReportItem) {
    setRevealingReport(report.filename)
    setError("")
    try {
      const result = await revealWeeklyReport(report.filename)
      setNotice(`已在文件夹中定位周报：${result.filename}`)
    } catch (reason) { setError((reason as Error).message) }
    finally { setRevealingReport("") }
  }

  async function handleDeleteReport() {
    if (!dialogPreview || !window.confirm("确认删除该周报？删除后无法在中台预览或文件夹中定位。")) return
    const filename = dialogPreview.report.filename
    const shouldReturnToHistory = returnToHistoryAfterPreview
    setReportMutating(true)
    setError("")
    try {
      const result = await deleteWeeklyReport(filename)
      setReports((current) => current.filter((item) => item.filename !== filename))
      setDialogPreview(null)
      setReturnToHistoryAfterPreview(false)
      if (shouldReturnToHistory) setHistoryOpen(true)
      setNotice(`已删除 ${compactDate(result.report.start_date)} - ${compactDate(result.report.end_date)} 周报`)
    } catch (reason) { setError((reason as Error).message) }
    finally { setReportMutating(false) }
  }

  async function handleAttach(item: WorkItemSummary) {
    if (!attachTarget) return
    await linkWorkItem(item.id, "worklog", worklogRef(attachTarget.date, attachTarget.entry.entry_index))
    setNotice(`已挂到工作项：${item.title}`)
    await load()
  }

  const totalEntries = useMemo(() => days.reduce((sum, day) => sum + day.entries.length, 0), [days])
  const bucketGroups = useMemo(() => groupBuckets(weeklyBuckets?.items || []), [weeklyBuckets])
  const entryTitles = useMemo(() => Object.fromEntries(days.flatMap((day) => day.entries.map((entry) => [worklogRef(day.date, entry.entry_index), entry.title]))), [days])
  const unlinkedCount = useMemo(() => days.reduce((sum, day) => sum + day.entries.filter((entry) => !entry.weekly_report_excluded && !workItemRefs[worklogRef(day.date, entry.entry_index)]).length, 0), [days, workItemRefs])
  const currentReport = useMemo(() => reports.find((report) => report.start_date === startDate && report.end_date === endDate), [endDate, reports, startDate])
  const recentReports = useMemo(() => reports.slice(0, 5), [reports])
  const atCurrentWeek = Boolean(settings && startDate >= settings.current_start_date)
  const weeklyJobActive = Boolean(weeklyJob && (weeklyJob.status === "queued" || weeklyJob.status === "running"))
  const actionBusy = generating || weeklyJobActive || previewLoading === currentReport?.filename

  return <div>
    <PageHeader meta={startDate ? `${displayDate(startDate)}至${displayDate(endDate)} · ${totalEntries} 条日志` : "正在读取周报周期"} title="工作日志与周报" actions={<div className="flex items-center gap-2"><Button onClick={() => setWorklogCreateOpen(true)}><Plus data-icon="inline-start" />添加工作日志</Button><Button aria-label="刷新工作日志" disabled={loading || !startDate} onClick={() => void load()} size="icon" title="刷新" variant="outline"><RefreshCw className={loading ? "animate-spin" : undefined} /></Button></div>} />
    {error ? <Alert className="mb-5" variant="destructive"><AlertDescription>{error}</AlertDescription></Alert> : null}
    {notice ? <p aria-live="polite" className="mb-5 text-sm text-[#247a56]">{notice}</p> : null}
    <div className="grid gap-8 xl:grid-cols-[minmax(0,1fr)_320px]">
      <main>
        <section className="border-y py-4"><div className="flex flex-wrap items-center gap-2">
          <Button disabled={!startDate} onClick={() => moveWeek(-1)} variant="outline"><ChevronLeft data-icon="inline-start" />上一周</Button>
          <span className="min-w-[205px] text-center text-sm font-medium tabular-nums">{startDate ? `${rangeDate(startDate)}-${rangeDate(endDate)}` : "加载中"}</span>
          <Button disabled={!startDate || atCurrentWeek} onClick={() => moveWeek(1)} variant="outline">下一周<ChevronRight data-icon="inline-end" /></Button>
        </div></section>
        <section className="mt-2">{loading && !days.length ? <div className="flex items-center gap-2 py-12 text-sm text-muted-foreground"><LoaderCircle className="size-4 animate-spin" />正在加载工作日志</div> : days.map((day) => <DaySection day={day} key={day.date} onAttach={(date, entry) => setAttachTarget({ date, entry })} onOpenWorklog={(date, entry) => setSelectedWorklog({ date, entry })} onToggleWeeklyReport={handleToggleWeeklyReport} togglingKey={togglingWorklog} workItems={workItemRefs} />)}</section>
      </main>
      <aside className="border-l pl-0 xl:pl-6">
        <section className="border-y py-4 xl:border-t-0">
          <div className="flex items-center gap-2"><NotebookPen className="size-4 text-[#b7791f]" /><h2 className="text-sm font-semibold">{currentReport ? "本期周报" : "生成周报"}</h2></div>
          <p className="mt-2 text-xs leading-5 text-muted-foreground">{currentReport ? "该周期已生成周报，可直接查看表格内容或在文件夹中定位 DOCX。" : "按当前周期读取 worklog，生成符合 weekly-report skill 规则的 DOCX。"}</p>
          <Button className="mt-4 w-full" disabled={!startDate || actionBusy} onClick={() => currentReport ? void openReport(currentReport) : void handleGenerate()}>{actionBusy ? <LoaderCircle className="animate-spin" data-icon="inline-start" /> : currentReport ? <FileText data-icon="inline-start" /> : <WandSparkles data-icon="inline-start" />}{actionBusy ? "正在处理" : currentReport ? "查看周报" : "生成周报"}</Button>
          <WeeklyReportProgress job={weeklyJob} />
        </section>
        {weeklyBuckets?.items.length || unlinkedCount ? <section className="mt-6 border-b pb-4">
          <div className="flex items-center gap-2"><ClipboardList className="size-4 text-muted-foreground" /><h2 className="text-sm font-semibold">本期工作项</h2>{unlinkedCount ? <Button className="ml-auto h-7 px-2 text-xs" onClick={() => setMatchOpen(true)} variant="outline">匹配建议</Button> : null}</div>
          {weeklyBuckets ? <p className="mt-1 text-[11px] text-muted-foreground">周报按此归纳；下周计划取 {weeklyBuckets.next_start_date.slice(5)} 至 {weeklyBuckets.next_end_date.slice(5)} 排期的事项{unlinkedCount ? `；${unlinkedCount} 条日志未挂工作项` : ""}</p> : null}
          {(["completed", "in_progress", "planned"] as const).map((bucket) => bucketGroups[bucket].length ? <div className="mt-3" key={bucket}>
            <p className="text-xs font-medium">{BUCKET_LABELS[bucket]} · {bucketGroups[bucket].length}</p>
            <ul className="mt-1 space-y-1">{bucketGroups[bucket].slice(0, 6).map((entry) => <li className="truncate text-xs" key={entry.item.id}><Link className={`hover:underline ${entry.overdue ? "text-destructive" : "text-muted-foreground"}`} title={entry.item.title} to={`/work-items/${entry.item.id}`}>{entry.item.title}</Link></li>)}</ul>
            {bucketGroups[bucket].length > 6 ? <p className="mt-1 text-[11px] text-muted-foreground">另有 {bucketGroups[bucket].length - 6} 项</p> : null}
          </div> : null)}
        </section> : null}
        <section className="mt-6">
          <h2 className="text-sm font-semibold">历史周报</h2>
          {recentReports.length ? <>
            <ul className="mt-2">{recentReports.map((report) => <ExistingReport key={report.filename} loading={previewLoading === report.filename || revealingReport === report.filename} onOpen={() => void openReport(report)} onReveal={() => void handleRevealReport(report)} report={report} />)}</ul>
            {reports.length > 5 ? <Button className="mt-3 w-full" onClick={() => setHistoryOpen(true)} variant="outline"><CalendarRange data-icon="inline-start" />查看更多</Button> : null}
          </> : <p className="mt-3 text-sm text-muted-foreground">还没有生成过周报。</p>}
        </section>
      </aside>
    </div>
    <HistoryReportsDialog onOpenChange={setHistoryOpen} onOpenReport={(report) => void openReport(report, true)} onRevealReport={(report) => void handleRevealReport(report)} open={historyOpen} previewLoading={previewLoading || revealingReport} reports={reports} />
    <WorklogCreateDialog onCreate={handleCreateWorklog} onOpenChange={setWorklogCreateOpen} open={worklogCreateOpen} />
    <WorkItemPickerDialog description={attachTarget ? `「${attachTarget.entry.title}」挂上后计入该工作项，周报按工作项归纳。` : ""} onOpenChange={(open) => { if (!open) setAttachTarget(null) }} onPick={handleAttach} open={Boolean(attachTarget)} title="挂到工作项" />
    <MatchSuggestionsDialog endDate={endDate} onLinked={(count) => { if (count) { setNotice(`已按建议挂上 ${count} 条日志`); void load() } }} onOpenChange={setMatchOpen} open={matchOpen} startDate={startDate} titles={entryTitles} />
    <WorklogDetailDialog detail={worklogDetail} error={worklogDetailError} loading={worklogDetailLoading} onOpenChange={handleWorklogDetailOpenChange} selection={selectedWorklog} />
    <ReportDialog busy={reportMutating || weeklyJobActive || Boolean(revealingReport)} onDelete={() => void handleDeleteReport()} onOpenChange={handlePreviewOpenChange} onReveal={() => dialogPreview && void handleRevealReport(dialogPreview.report)} onRegenerate={() => void handleRegenerateReport()} preview={dialogPreview} />
  </div>
}
