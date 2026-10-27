import { useCallback, useEffect, useMemo, useState } from "react"
import { ArrowLeft, Check, FileUp, LoaderCircle, Pencil, RefreshCw } from "lucide-react"
import { Link, useParams } from "react-router-dom"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import { PageHeader } from "@/components/PageHeader"
import { ImportDialog, WorkItemStatusBadge } from "@/components/workitems/WorkItemBits"
import { loadPlan, updatePlan, updateWorkItem } from "@/lib/workItemsApi"
import type { WorkItemPlanDetail, WorkItemSummary } from "@/lib/workItemsTypes"
import { barPosition, dateRangeLabel, isClosed, isOverdue, markerPosition, planSpan } from "@/lib/workItemsView"
import type { PlanSpan } from "@/lib/workItemsView"

function Timeline({ item, span, today }: { item: WorkItemSummary; span: PlanSpan | null; today: string }) {
  if (!span) return null
  const bar = barPosition(item, span)
  const marker = markerPosition(today, span)
  const tone = item.status === "done" ? "bg-[#247a56]/70" : isOverdue(item, today) ? "bg-destructive/70" : item.status === "in_progress" ? "bg-[#2f6fd6]/70" : "bg-muted-foreground/35"
  return <div className="relative hidden h-2 w-full rounded-full bg-muted/60 md:block">
    {bar ? <div className={`absolute top-0 h-2 rounded-full ${tone}`} style={{ left: `${bar.left}%`, width: `${Math.max(bar.width, 1)}%` }} /> : null}
    {marker !== null ? <div className="absolute -top-1 h-4 w-px bg-foreground/50" style={{ left: `${marker}%` }} title="今天" /> : null}
  </div>
}

function PlanEditDialog({ open, title, description, onOpenChange, onSave }: {
  open: boolean
  title: string
  description: string
  onOpenChange: (open: boolean) => void
  onSave: (title: string, description: string) => Promise<void>
}) {
  const [value, setValue] = useState({ title, description })
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState("")
  useEffect(() => { if (open) { setValue({ title, description }); setError("") } }, [description, open, title])

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setSaving(true)
    try {
      await onSave(value.title.trim(), value.description.trim())
      onOpenChange(false)
    } catch (reason) {
      setError((reason as Error).message)
    } finally {
      setSaving(false)
    }
  }

  return <Dialog onOpenChange={onOpenChange} open={open}>
    <DialogContent className="max-w-[min(560px,calc(100vw-2rem))]">
      <DialogHeader><DialogTitle>编辑计划</DialogTitle><DialogDescription className="sr-only">修改计划标题与说明</DialogDescription></DialogHeader>
      <form className="space-y-4" onSubmit={submit}>
        <div className="space-y-2"><Label htmlFor="plan-title">标题</Label><Input id="plan-title" maxLength={200} onChange={(event) => setValue((current) => ({ ...current, title: event.target.value }))} required value={value.title} /></div>
        <div className="space-y-2"><Label htmlFor="plan-description">说明（选填）</Label><Textarea className="min-h-24" id="plan-description" maxLength={4000} onChange={(event) => setValue((current) => ({ ...current, description: event.target.value }))} value={value.description} /></div>
        {error ? <p className="text-sm text-destructive" role="alert">{error}</p> : null}
        <DialogFooter><Button disabled={saving || !value.title.trim()} type="submit">{saving ? <LoaderCircle className="animate-spin" data-icon="inline-start" /> : <Check data-icon="inline-start" />}保存</Button></DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}

export default function WorkPlan() {
  const { planId = "" } = useParams()
  const [data, setData] = useState<WorkItemPlanDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const [notice, setNotice] = useState("")
  const [toggling, setToggling] = useState("")
  const [importOpen, setImportOpen] = useState(false)
  const [editOpen, setEditOpen] = useState(false)

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true)
    try {
      setData(await loadPlan(planId, signal))
      setError("")
    } catch (reason) {
      if ((reason as Error).name !== "AbortError") setError((reason as Error).message)
    } finally {
      setLoading(false)
    }
  }, [planId])

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => controller.abort()
  }, [load])

  const span = useMemo(() => planSpan(data ? [...data.nodes.flatMap((node) => node.items), ...data.unassigned] : []), [data])

  async function toggleDone(item: WorkItemSummary) {
    setToggling(item.id)
    setError("")
    try {
      await updateWorkItem(item.id, { status: item.status === "done" ? "in_progress" : "done" })
      await load()
    } catch (reason) {
      setError((reason as Error).message)
    } finally {
      setToggling("")
    }
  }

  if (loading && !data) return <div className="py-20 text-center text-muted-foreground">正在读取计划</div>
  if (!data) return <div><Button asChild size="sm" variant="ghost"><Link to="/work-items"><ArrowLeft data-icon="inline-start" />返回工作项</Link></Button><p className="mt-10 text-center text-destructive">{error || "计划不存在"}</p></div>

  const { plan, progress, today } = data
  const groups = [...data.nodes, ...(data.unassigned.length ? [{ name: "未分节点", progress: null, items: data.unassigned }] : [])]

  return <div>
    <Button asChild className="mb-4" size="sm" variant="ghost"><Link to="/work-items"><ArrowLeft data-icon="inline-start" />返回工作项</Link></Button>
    <PageHeader
      meta={`${progress.done}/${progress.total} 完成${progress.overdue ? ` · ${progress.overdue} 项逾期` : ""}${span ? ` · ${span.start} 至 ${span.end}` : ""}`}
      title={plan.title}
      actions={<>
        <Button onClick={() => setImportOpen(true)} variant="outline"><FileUp data-icon="inline-start" />重新导入</Button>
        <Button onClick={() => setEditOpen(true)} variant="outline"><Pencil data-icon="inline-start" />编辑</Button>
        <Button aria-label="刷新" disabled={loading} onClick={() => void load()} size="icon" title="刷新" variant="outline"><RefreshCw className={loading ? "animate-spin" : undefined} /></Button>
      </>}
    />
    {plan.description ? <p className="-mt-3 mb-5 whitespace-pre-wrap text-sm text-muted-foreground">{plan.description}</p> : null}
    {error ? <Alert className="mb-5" variant="destructive"><AlertDescription>{error}</AlertDescription></Alert> : null}
    {notice ? <p aria-live="polite" className="mb-5 text-sm text-[#247a56]">{notice}</p> : null}
    <div className="space-y-8">
      {groups.map((group) => <section key={group.name}>
        <div className="flex items-baseline gap-3 border-b pb-2">
          <h2 className="text-base font-semibold">{group.name}</h2>
          {group.progress ? <span className="text-xs text-muted-foreground">{group.progress.done}/{group.progress.total} 完成</span> : null}
        </div>
        <ul>{group.items.map((item) => {
          const overdue = isOverdue(item, today)
          return <li className={`grid items-center gap-x-4 gap-y-1 border-b py-2.5 md:grid-cols-[minmax(0,1fr)_120px_minmax(160px,28%)] ${item.kind === "acceptance" ? "bg-muted/30" : ""}`} key={item.id}>
            <div className="flex min-w-0 items-start gap-2">
              <button aria-label={item.status === "done" ? `标记未完成：${item.title}` : `标记完成：${item.title}`} className="mt-0.5 flex size-4 shrink-0 items-center justify-center rounded border disabled:opacity-50" disabled={Boolean(toggling) || item.status === "cancelled"} onClick={() => void toggleDone(item)} title={item.status === "done" ? "标记未完成" : "标记完成"} type="button">
                {toggling === item.id ? <LoaderCircle className="size-3 animate-spin" /> : item.status === "done" ? <Check className="size-3" /> : null}
              </button>
              <div className="min-w-0">
                <Link className={`break-words text-sm hover:underline ${isClosed(item.status) ? "text-muted-foreground" : ""} ${item.status === "cancelled" ? "line-through" : ""}`} to={`/work-items/${item.id}`}>{item.title}</Link>
                <div className="mt-0.5 flex flex-wrap items-center gap-2">
                  {item.kind === "acceptance" ? <Badge variant="outline">产物验证</Badge> : null}
                  {item.status !== "todo" && item.status !== "done" ? <WorkItemStatusBadge status={item.status} /> : null}
                  {item.link_count ? <span className="text-xs text-muted-foreground">{item.link_count} 项挂载</span> : null}
                </div>
              </div>
            </div>
            <span className={`text-xs tabular-nums ${overdue ? "font-medium text-destructive" : "text-muted-foreground"}`}>{dateRangeLabel(item.start_date, item.due_date) || "—"}{overdue ? " 逾期" : ""}</span>
            <Timeline item={item} span={span} today={today} />
          </li>
        })}</ul>
      </section>)}
    </div>
    <ImportDialog
      onImported={(result) => { setNotice(`重新导入完成：新建 ${result.create} 项，更新日期 ${result.update} 项`); void load() }}
      onOpenChange={setImportOpen}
      open={importOpen}
      presetPlanId={plan.id}
    />
    <PlanEditDialog
      description={plan.description || ""}
      onOpenChange={setEditOpen}
      onSave={async (title, description) => { await updatePlan(plan.id, { title, description }); await load() }}
      open={editOpen}
      title={plan.title}
    />
  </div>
}
