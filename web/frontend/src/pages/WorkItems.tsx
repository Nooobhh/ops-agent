import { useCallback, useEffect, useMemo, useState } from "react"
import { CalendarClock, ClipboardList, FileUp, Link2, LoaderCircle, Plus, RefreshCw, Search } from "lucide-react"
import { Link, useNavigate } from "react-router-dom"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { PageHeader } from "@/components/PageHeader"
import { ImportDialog, WorkItemFormDialog, WorkItemStatusBadge, formPatch } from "@/components/workitems/WorkItemBits"
import { createWorkItem, listPlans, listWorkItems } from "@/lib/workItemsApi"
import type { WorkItemPlanSummary, WorkItemStatus, WorkItemSummary } from "@/lib/workItemsTypes"
import { PRIORITY_LABELS, SOURCE_LABELS, STATUS_LABELS, STATUS_ORDER, dateRangeLabel, isOverdue } from "@/lib/workItemsView"

const OPEN = "open"
const ALL = "all"
const ANY_PLAN = "__any__"
const NO_PLAN = "__none__"

function ItemRow({ item, plans, today }: { item: WorkItemSummary; plans: Map<string, string>; today: string }) {
  const overdue = isOverdue(item, today)
  const range = dateRangeLabel(item.start_date, item.due_date)
  return <li className="flex items-start gap-3 border-b py-3 last:border-b-0">
    <WorkItemStatusBadge status={item.status} />
    <div className="min-w-0 flex-1">
      <Link className={`block break-words text-sm font-medium hover:underline ${item.status === "cancelled" ? "text-muted-foreground line-through" : ""}`} to={`/work-items/${item.id}`}>{item.title}</Link>
      <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
        {item.plan_id ? <Link className="hover:underline" to={`/work-items/plans/${item.plan_id}`}>{plans.get(item.plan_id) || "计划"}{item.plan_node ? ` · ${item.plan_node}` : ""}</Link> : <span>{SOURCE_LABELS[item.source]}</span>}
        {item.kind === "acceptance" ? <Badge variant="outline">产物验证</Badge> : null}
        {item.priority !== "normal" ? <span>优先级{PRIORITY_LABELS[item.priority]}</span> : null}
        {item.assignee ? <span>{item.assignee}</span> : null}
        {item.link_count ? <span className="inline-flex items-center gap-1"><Link2 className="size-3" />{item.link_count}</span> : null}
      </div>
    </div>
    {range ? <span className={`shrink-0 text-xs tabular-nums ${overdue ? "font-medium text-destructive" : "text-muted-foreground"}`} title={overdue ? "已过截止" : undefined}>{overdue ? "逾期 · " : ""}{range}</span> : null}
  </li>
}

function PlanCard({ plan }: { plan: WorkItemPlanSummary }) {
  const ratio = plan.progress.total ? plan.progress.done / plan.progress.total : 0
  return <li className="border-b py-3 last:border-b-0">
    <Link className="text-sm font-medium hover:underline" to={`/work-items/plans/${plan.id}`}>{plan.title}</Link>
    <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-muted"><div className="h-full rounded-full bg-[#247a56]" style={{ width: `${ratio * 100}%` }} /></div>
    <p className="mt-1.5 text-xs text-muted-foreground">{plan.progress.done}/{plan.progress.total} 完成 · {plan.nodes.length} 个节点{plan.progress.overdue ? <span className="text-destructive"> · {plan.progress.overdue} 项逾期</span> : null}</p>
  </li>
}

export default function WorkItems() {
  const navigate = useNavigate()
  const [items, setItems] = useState<WorkItemSummary[]>([])
  const [plans, setPlans] = useState<WorkItemPlanSummary[]>([])
  const [today, setToday] = useState("")
  const [statusFilter, setStatusFilter] = useState<string>(OPEN)
  const [planFilter, setPlanFilter] = useState(ANY_PLAN)
  const [query, setQuery] = useState("")
  const [quickTitle, setQuickTitle] = useState("")
  const [quickDue, setQuickDue] = useState("")
  const [quickSaving, setQuickSaving] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  const [importOpen, setImportOpen] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const [notice, setNotice] = useState("")

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true)
    try {
      const status = statusFilter === OPEN || statusFilter === ALL ? undefined : [statusFilter as WorkItemStatus]
      const [itemResult, planResult] = await Promise.all([
        listWorkItems({ status, includeClosed: statusFilter === ALL, planId: planFilter === ANY_PLAN || planFilter === NO_PLAN ? undefined : planFilter, q: query }, signal),
        listPlans(signal),
      ])
      setItems(planFilter === NO_PLAN ? itemResult.items.filter((item) => !item.plan_id) : itemResult.items)
      setToday(itemResult.today)
      setPlans(planResult.plans)
      setError("")
    } catch (reason) {
      if ((reason as Error).name !== "AbortError") setError((reason as Error).message)
    } finally {
      setLoading(false)
    }
  }, [planFilter, query, statusFilter])

  useEffect(() => {
    const controller = new AbortController()
    const timer = window.setTimeout(() => void load(controller.signal), query ? 250 : 0)
    return () => { controller.abort(); window.clearTimeout(timer) }
  }, [load, query])

  const planTitles = useMemo(() => new Map(plans.map((plan) => [plan.id, plan.title])), [plans])
  const overdueCount = items.filter((item) => isOverdue(item, today)).length

  async function quickAdd(event: React.FormEvent) {
    event.preventDefault()
    if (!quickTitle.trim()) return
    setQuickSaving(true)
    setError("")
    try {
      const { item } = await createWorkItem({ title: quickTitle.trim(), due_date: quickDue || null })
      setQuickTitle("")
      setNotice(`已登记：${item.title}`)
      await load()
    } catch (reason) {
      setError((reason as Error).message)
    } finally {
      setQuickSaving(false)
    }
  }

  return <div>
    <PageHeader
      meta={loading ? "正在读取工作项" : `${items.length} 项${overdueCount ? ` · ${overdueCount} 项逾期` : ""} · ${plans.length} 份计划`}
      title="工作项"
      actions={<>
        <Button onClick={() => setCreateOpen(true)}><Plus data-icon="inline-start" />新建工作项</Button>
        <Button onClick={() => setImportOpen(true)} variant="outline"><FileUp data-icon="inline-start" />导入计划 / 批量登记</Button>
        <Button aria-label="刷新工作项" disabled={loading} onClick={() => void load()} size="icon" title="刷新" variant="outline"><RefreshCw className={loading ? "animate-spin" : undefined} /></Button>
      </>}
    />
    {error ? <Alert className="mb-5" variant="destructive"><AlertDescription>{error}</AlertDescription></Alert> : null}
    {notice ? <p aria-live="polite" className="mb-5 text-sm text-[#247a56]">{notice}</p> : null}
    <div className="grid gap-8 xl:grid-cols-[minmax(0,1fr)_320px]">
      <main>
        <form className="flex flex-wrap items-center gap-2 border-y py-3" onSubmit={quickAdd}>
          <CalendarClock className="size-4 text-muted-foreground" />
          <Input aria-label="快速登记标题" className="min-w-[220px] flex-1" maxLength={200} onChange={(event) => setQuickTitle(event.target.value)} placeholder="快速登记：本周要做的事，回车即建" value={quickTitle} />
          <Input aria-label="截止日期" className="w-[160px]" onChange={(event) => setQuickDue(event.target.value)} title="截止日期（选填）" type="date" value={quickDue} />
          <Button disabled={quickSaving || !quickTitle.trim()} type="submit" variant="secondary">{quickSaving ? <LoaderCircle className="animate-spin" data-icon="inline-start" /> : <Plus data-icon="inline-start" />}登记</Button>
        </form>
        <div className="mt-4 flex flex-wrap items-center gap-3">
          <ToggleGroup onValueChange={(value) => value && setStatusFilter(value)} size="sm" type="single" value={statusFilter} variant="outline">
            <ToggleGroupItem value={OPEN}>未关闭</ToggleGroupItem>
            {STATUS_ORDER.map((status) => <ToggleGroupItem key={status} value={status}>{STATUS_LABELS[status]}</ToggleGroupItem>)}
            <ToggleGroupItem value={ALL}>全部</ToggleGroupItem>
          </ToggleGroup>
          <Select onValueChange={setPlanFilter} value={planFilter}><SelectTrigger aria-label="按计划筛选" className="w-[200px]"><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value={ANY_PLAN}>全部计划与临时事项</SelectItem><SelectItem value={NO_PLAN}>不属于计划</SelectItem>{plans.map((plan) => <SelectItem key={plan.id} value={plan.id}>{plan.title}</SelectItem>)}</SelectGroup></SelectContent></Select>
          <div className="relative min-w-[200px] flex-1"><Search className="pointer-events-none absolute left-2.5 top-2.5 size-4 text-muted-foreground" /><Input aria-label="搜索工作项" className="pl-8" onChange={(event) => setQuery(event.target.value)} placeholder="搜索标题或描述" value={query} /></div>
        </div>
        {loading && !items.length ? <div className="flex items-center gap-2 py-12 text-sm text-muted-foreground"><LoaderCircle className="size-4 animate-spin" />正在读取工作项</div> : null}
        {!loading && !items.length ? <div className="py-12 text-center text-sm text-muted-foreground"><ClipboardList className="mx-auto mb-2 size-6" />没有符合条件的工作项。</div> : null}
        <ul className="mt-2">{items.map((item) => <ItemRow item={item} key={item.id} plans={planTitles} today={today} />)}</ul>
      </main>
      <aside className="border-l pl-0 xl:pl-6">
        <section className="border-y py-4 xl:border-t-0">
          <h2 className="text-sm font-semibold">计划</h2>
          {plans.length ? <ul className="mt-1">{plans.map((plan) => <PlanCard key={plan.id} plan={plan} />)}</ul> : <p className="mt-2 text-xs leading-5 text-muted-foreground">还没有计划。把半年运维计划表从在线表格整块复制，用「导入计划」粘贴进来。</p>}
        </section>
      </aside>
    </div>
    <WorkItemFormDialog
      onOpenChange={setCreateOpen}
      onSubmit={async (value) => {
        const { item } = await createWorkItem(formPatch(value) as Parameters<typeof createWorkItem>[0])
        navigate(`/work-items/${item.id}`)
      }}
      open={createOpen}
      title="新建工作项"
    />
    <ImportDialog
      onImported={(result) => {
        setNotice(result.plan?.id ? `已导入「${result.plan.title}」：新建 ${result.create} 项，更新 ${result.update} 项` : `已批量登记 ${result.create} 项`)
        if (result.plan?.id) navigate(`/work-items/plans/${result.plan.id}`)
        else void load()
      }}
      onOpenChange={setImportOpen}
      open={importOpen}
    />
  </div>
}
