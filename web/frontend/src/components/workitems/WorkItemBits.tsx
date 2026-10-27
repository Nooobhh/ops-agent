import { useEffect, useMemo, useState } from "react"
import { Check, LoaderCircle, Search, Sparkles, Upload } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { applyImport, linkWorkItem, listPlans, listWorkItems, previewImport, suggestMatches } from "@/lib/workItemsApi"
import type { ImportResult, MatchSuggestion, WorkItem, WorkItemPatch, WorkItemPlanSummary, WorkItemPriority, WorkItemStatus, WorkItemSummary } from "@/lib/workItemsTypes"
import { PRIORITY_LABELS, STATUS_LABELS, STATUS_ORDER, dateRangeLabel, statusBadgeClass } from "@/lib/workItemsView"

const NONE = "__none__"

export function WorkItemStatusBadge({ status }: { status: WorkItemStatus }) {
  return <Badge className={`shrink-0 ${statusBadgeClass(status)}`} variant="outline">{STATUS_LABELS[status]}</Badge>
}

export type WorkItemFormValue = Required<Pick<WorkItem, "title" | "status" | "priority">> & {
  description: string
  start_date: string
  due_date: string
  plan_id: string
  plan_node: string
}

export function formValue(item?: Partial<WorkItem> | null): WorkItemFormValue {
  return {
    title: item?.title || "",
    description: item?.description || "",
    status: item?.status || "todo",
    priority: item?.priority || "normal",
    start_date: item?.start_date || "",
    due_date: item?.due_date || "",
    plan_id: item?.plan_id || "",
    plan_node: item?.plan_node || "",
  }
}

export function formPatch(value: WorkItemFormValue): WorkItemPatch {
  return {
    title: value.title.trim(),
    description: value.description.trim() || null,
    status: value.status,
    priority: value.priority,
    start_date: value.start_date || null,
    due_date: value.due_date || null,
    plan_id: value.plan_id || null,
    plan_node: value.plan_id ? value.plan_node || null : null,
  }
}

/** 新建或编辑工作项;计划/节点可选,挂到长期计划的某个节点下。 */
export function WorkItemFormDialog({ open, title, initial, onOpenChange, onSubmit }: {
  open: boolean
  title: string
  initial?: Partial<WorkItem> | null
  onOpenChange: (open: boolean) => void
  onSubmit: (value: WorkItemFormValue) => Promise<void>
}) {
  const [value, setValue] = useState<WorkItemFormValue>(formValue(initial))
  const [plans, setPlans] = useState<WorkItemPlanSummary[]>([])
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState("")

  useEffect(() => {
    if (!open) return
    setValue(formValue(initial))
    setError("")
    const controller = new AbortController()
    listPlans(controller.signal).then((result) => setPlans(result.plans)).catch(() => setPlans([]))
    return () => controller.abort()
  }, [initial, open])

  const nodes = plans.find((plan) => plan.id === value.plan_id)?.nodes || []
  const patch = (update: Partial<WorkItemFormValue>) => setValue((current) => ({ ...current, ...update }))

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setSaving(true)
    setError("")
    try {
      await onSubmit(value)
      onOpenChange(false)
    } catch (reason) {
      setError((reason as Error).message)
    } finally {
      setSaving(false)
    }
  }

  return <Dialog onOpenChange={onOpenChange} open={open}>
    <DialogContent className="max-h-[90vh] max-w-[min(680px,calc(100vw-2rem))] overflow-y-auto">
      <DialogHeader>
        <DialogTitle>{title}</DialogTitle>
        <DialogDescription className="sr-only">填写工作项的标题、状态、日期与所属计划</DialogDescription>
      </DialogHeader>
      <form className="space-y-5" onSubmit={submit}>
        <div className="space-y-2"><Label htmlFor="work-item-title">标题</Label><Input id="work-item-title" maxLength={200} onChange={(event) => patch({ title: event.target.value })} placeholder="一件要做的事" required value={value.title} /></div>
        <div className="space-y-2"><Label htmlFor="work-item-description">描述（选填）</Label><Textarea className="min-h-24 resize-y" id="work-item-description" maxLength={4000} onChange={(event) => patch({ description: event.target.value })} placeholder="目标、背景、验收点" value={value.description} /></div>
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-2"><Label>状态</Label><Select onValueChange={(next: WorkItemStatus) => patch({ status: next })} value={value.status}><SelectTrigger aria-label="工作项状态"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>{STATUS_ORDER.map((status) => <SelectItem key={status} value={status}>{STATUS_LABELS[status]}</SelectItem>)}</SelectGroup></SelectContent></Select></div>
          <div className="space-y-2"><Label>优先级</Label><Select onValueChange={(next: WorkItemPriority) => patch({ priority: next })} value={value.priority}><SelectTrigger aria-label="工作项优先级"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>{(["high", "normal", "low"] as WorkItemPriority[]).map((priority) => <SelectItem key={priority} value={priority}>{PRIORITY_LABELS[priority]}</SelectItem>)}</SelectGroup></SelectContent></Select></div>
          <div className="space-y-2"><Label htmlFor="work-item-start">开始日期（选填）</Label><Input id="work-item-start" onChange={(event) => patch({ start_date: event.target.value })} type="date" value={value.start_date} /></div>
          <div className="space-y-2"><Label htmlFor="work-item-due">截止日期（选填）</Label><Input id="work-item-due" onChange={(event) => patch({ due_date: event.target.value })} type="date" value={value.due_date} /></div>
          <div className="space-y-2"><Label>所属计划（选填）</Label><Select onValueChange={(next) => patch({ plan_id: next === NONE ? "" : next, plan_node: "" })} value={value.plan_id || NONE}><SelectTrigger aria-label="所属计划"><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value={NONE}>不属于计划</SelectItem>{plans.map((plan) => <SelectItem key={plan.id} value={plan.id}>{plan.title}</SelectItem>)}</SelectGroup></SelectContent></Select></div>
          <div className="space-y-2"><Label>项目节点</Label><Select disabled={!value.plan_id} onValueChange={(next) => patch({ plan_node: next === NONE ? "" : next })} value={value.plan_node || NONE}><SelectTrigger aria-label="项目节点"><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value={NONE}>不指定节点</SelectItem>{nodes.map((node) => <SelectItem key={node} value={node}>{node}</SelectItem>)}</SelectGroup></SelectContent></Select></div>
        </div>
        {error ? <p className="text-sm text-destructive" role="alert">{error}</p> : null}
        <DialogFooter><Button disabled={saving || !value.title.trim()} type="submit">{saving ? <LoaderCircle className="animate-spin" data-icon="inline-start" /> : <Check data-icon="inline-start" />}{saving ? "正在保存" : "保存"}</Button></DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}

/** 从未关闭工作项里挑一个(挂 worklog / 任务用)。 */
export function WorkItemPickerDialog({ open, title, description, onOpenChange, onPick }: {
  open: boolean
  title: string
  description: string
  onOpenChange: (open: boolean) => void
  onPick: (item: WorkItemSummary) => Promise<void>
}) {
  const [items, setItems] = useState<WorkItemSummary[]>([])
  const [query, setQuery] = useState("")
  const [loading, setLoading] = useState(false)
  const [picking, setPicking] = useState("")
  const [error, setError] = useState("")

  useEffect(() => {
    if (!open) return
    setQuery("")
    setError("")
    setLoading(true)
    const controller = new AbortController()
    listWorkItems({}, controller.signal).then((result) => setItems(result.items))
      .catch((reason) => { if ((reason as Error).name !== "AbortError") setError((reason as Error).message) })
      .finally(() => setLoading(false))
    return () => controller.abort()
  }, [open])

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return needle ? items.filter((item) => `${item.title} ${item.plan_node || ""}`.toLowerCase().includes(needle)) : items
  }, [items, query])

  async function pick(item: WorkItemSummary) {
    setPicking(item.id)
    setError("")
    try {
      await onPick(item)
      onOpenChange(false)
    } catch (reason) {
      setError((reason as Error).message)
    } finally {
      setPicking("")
    }
  }

  return <Dialog onOpenChange={onOpenChange} open={open}>
    <DialogContent className="flex max-h-[85vh] max-w-[min(640px,calc(100vw-2rem))] flex-col gap-0 overflow-hidden p-0">
      <DialogHeader className="shrink-0 border-b px-6 pb-4 pt-6">
        <DialogTitle>{title}</DialogTitle>
        <DialogDescription>{description}</DialogDescription>
      </DialogHeader>
      <div className="shrink-0 border-b px-6 py-3">
        <div className="relative"><Search className="pointer-events-none absolute left-2.5 top-2.5 size-4 text-muted-foreground" /><Input aria-label="搜索工作项" className="pl-8" onChange={(event) => setQuery(event.target.value)} placeholder="按标题或节点搜索" value={query} /></div>
        {error ? <p className="mt-2 text-sm text-destructive" role="alert">{error}</p> : null}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2">
        {loading ? <div className="flex items-center gap-2 p-4 text-sm text-muted-foreground"><LoaderCircle className="size-4 animate-spin" />正在读取工作项</div> : null}
        {!loading && !visible.length ? <p className="p-4 text-sm text-muted-foreground">没有未关闭的工作项。</p> : null}
        {visible.map((item) => <button className="flex w-full items-start gap-2 rounded-md px-3 py-2 text-left hover:bg-muted/50 disabled:opacity-60" disabled={Boolean(picking)} key={item.id} onClick={() => void pick(item)} type="button">
          <WorkItemStatusBadge status={item.status} />
          <span className="min-w-0 flex-1">
            <span className="block text-sm">{item.title}</span>
            <span className="block text-xs text-muted-foreground">{[item.plan_node, dateRangeLabel(item.start_date, item.due_date)].filter(Boolean).join(" · ")}</span>
          </span>
          {picking === item.id ? <LoaderCircle className="mt-0.5 size-4 animate-spin" /> : null}
        </button>)}
      </div>
    </DialogContent>
  </Dialog>
}

const IMPORT_ACTION_LABELS = { create: "新建", update: "更新日期", unchanged: "不变" } as const
const NEW_PLAN = "__new__"

/** 粘贴在线表格(或 CSV):有「项目节点」列导入为计划,没有则批量登记单条事项。 */
export function ImportDialog({ open, presetPlanId, onOpenChange, onImported }: {
  open: boolean
  presetPlanId?: string
  onOpenChange: (open: boolean) => void
  onImported: (result: ImportResult) => void
}) {
  const [text, setText] = useState("")
  const [plans, setPlans] = useState<WorkItemPlanSummary[]>([])
  const [target, setTarget] = useState(NEW_PLAN)
  const [planTitle, setPlanTitle] = useState("")
  const [preview, setPreview] = useState<ImportResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")

  useEffect(() => {
    if (!open) return
    setText("")
    setPlanTitle("")
    setPreview(null)
    setError("")
    setTarget(presetPlanId || NEW_PLAN)
    const controller = new AbortController()
    listPlans(controller.signal).then((result) => setPlans(result.plans)).catch(() => setPlans([]))
    return () => controller.abort()
  }, [open, presetPlanId])

  // 新计划标题留空即「不导入到计划」:无节点列的表格批量登记,带节点的由后端提示补标题
  function body() {
    return target === NEW_PLAN ? { text, plan_title: planTitle.trim() || undefined } : { text, plan_id: target }
  }

  async function runPreview() {
    setBusy(true)
    setError("")
    try {
      setPreview(await previewImport(body()))
    } catch (reason) {
      setPreview(null)
      setError((reason as Error).message)
    } finally {
      setBusy(false)
    }
  }

  async function confirm() {
    if (!preview) return
    setBusy(true)
    setError("")
    try {
      onImported(await applyImport(body()))
      onOpenChange(false)
    } catch (reason) {
      setError((reason as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return <Dialog onOpenChange={onOpenChange} open={open}>
    <DialogContent className="flex max-h-[90vh] max-w-[min(960px,calc(100vw-2rem))] flex-col gap-0 overflow-hidden p-0">
      <DialogHeader className="shrink-0 border-b px-6 pb-4 pt-6">
        <DialogTitle>导入计划 / 批量登记</DialogTitle>
        <DialogDescription>从在线表格整块复制粘贴（含表头）。有「项目节点」列导入为计划，「产物验证」行作为该节点的验收项；没有则逐行建成单条事项。</DialogDescription>
      </DialogHeader>
      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-6 py-5">
        <Textarea aria-label="粘贴表格" className="min-h-40 resize-y font-mono text-xs" onChange={(event) => { setText(event.target.value); setPreview(null) }} placeholder={"项目节点\t完成状态\t具体事项\t开始时间\t截止时间\n重复计算治理\tFALSE\t…\t2026/8/17\t2026/8/21"} value={text} />
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-2"><Label>导入到计划（表格带项目节点时）</Label><Select onValueChange={(next) => { setTarget(next); setPreview(null) }} value={target}><SelectTrigger aria-label="导入目标计划"><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value={NEW_PLAN}>新建计划</SelectItem>{plans.map((plan) => <SelectItem key={plan.id} value={plan.id}>{plan.title}</SelectItem>)}</SelectGroup></SelectContent></Select></div>
          {target === NEW_PLAN ? <div className="space-y-2"><Label htmlFor="import-plan-title">新计划标题</Label><Input id="import-plan-title" maxLength={200} onChange={(event) => { setPlanTitle(event.target.value); setPreview(null) }} placeholder="如：2026 下半年运维计划（批量登记留空）" value={planTitle} /></div> : null}
        </div>
        {error ? <p className="text-sm text-destructive" role="alert">{error}</p> : null}
        {preview ? <section>
          <p className="text-sm">{preview.has_nodes ? `导入到「${preview.plan?.title}」` : "批量登记为单条事项"}：新建 {preview.create} 项，更新日期 {preview.update} 项，不变 {preview.unchanged} 项。{preview.update ? "已有事项的状态不会被覆盖。" : ""}</p>
          <div className="mt-3 max-h-72 overflow-auto rounded-md border">
            <table className="w-full text-left text-xs">
              <thead className="sticky top-0 bg-muted/60"><tr><th className="px-3 py-2">动作</th><th className="px-3 py-2">节点</th><th className="px-3 py-2">事项</th><th className="px-3 py-2">日期</th><th className="px-3 py-2">完成</th></tr></thead>
              <tbody>{preview.rows.map((row) => <tr className="border-t" key={`${row.line}-${row.title}`}>
                <td className="whitespace-nowrap px-3 py-1.5">{IMPORT_ACTION_LABELS[row.action]}</td>
                <td className="whitespace-nowrap px-3 py-1.5">{row.node || "-"}{row.kind === "acceptance" ? <Badge className="ml-1" variant="outline">产物验证</Badge> : null}</td>
                <td className="px-3 py-1.5">{row.title}</td>
                <td className="whitespace-nowrap px-3 py-1.5 tabular-nums">{dateRangeLabel(row.start_date, row.due_date) || "-"}</td>
                <td className="px-3 py-1.5">{row.done ? "✓" : ""}</td>
              </tr>)}</tbody>
            </table>
          </div>
        </section> : null}
      </div>
      <DialogFooter className="shrink-0 border-t px-6 py-4">
        <Button disabled={busy || !text.trim()} onClick={() => void runPreview()} variant="outline">{busy && !preview ? <LoaderCircle className="animate-spin" data-icon="inline-start" /> : <Search data-icon="inline-start" />}预览</Button>
        <Button disabled={busy || !preview || (preview.create + preview.update === 0)} onClick={() => void confirm()}>{busy && preview ? <LoaderCircle className="animate-spin" data-icon="inline-start" /> : <Upload data-icon="inline-start" />}确认导入</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
}

/** 模型给本期未挂载的日志推荐工作项;只是建议,勾选确认后才逐条挂载。 */
export function MatchSuggestionsDialog({ open, startDate, endDate, titles, onOpenChange, onLinked }: {
  open: boolean
  startDate: string
  endDate: string
  titles: Record<string, string>
  onOpenChange: (open: boolean) => void
  onLinked: (count: number) => void
}) {
  const [suggestions, setSuggestions] = useState<MatchSuggestion[]>([])
  const [unassigned, setUnassigned] = useState(0)
  const [checked, setChecked] = useState<Record<string, boolean>>({})
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState("")

  useEffect(() => {
    if (!open) return
    setSuggestions([])
    setError("")
    setLoading(true)
    suggestMatches(startDate, endDate).then((result) => {
      setSuggestions(result.suggestions)
      setUnassigned(result.unassigned)
      setChecked(Object.fromEntries(result.suggestions.map((item) => [item.worklog_ref, true])))
    }).catch((reason) => setError((reason as Error).message)).finally(() => setLoading(false))
  }, [endDate, open, startDate])

  async function confirm() {
    setSaving(true)
    setError("")
    let linked = 0
    const failures: string[] = []
    for (const item of suggestions.filter((entry) => checked[entry.worklog_ref])) {
      try {
        await linkWorkItem(item.work_item_id, "worklog", item.worklog_ref)
        linked += 1
      } catch (reason) {
        failures.push(`${item.worklog_ref}：${(reason as Error).message}`)
      }
    }
    setSaving(false)
    onLinked(linked)
    if (failures.length) setError(failures.join("；"))
    else onOpenChange(false)
  }

  const selected = suggestions.filter((item) => checked[item.worklog_ref]).length
  return <Dialog onOpenChange={onOpenChange} open={open}>
    <DialogContent className="flex max-h-[85vh] max-w-[min(760px,calc(100vw-2rem))] flex-col gap-0 overflow-hidden p-0">
      <DialogHeader className="shrink-0 border-b px-6 pb-4 pt-6">
        <DialogTitle>匹配建议</DialogTitle>
        <DialogDescription>周报默认模型为本期未挂载的日志推荐所属工作项。只是建议：勾选确认后才挂上，不勾的保持不挂。</DialogDescription>
      </DialogHeader>
      <div className="min-h-0 flex-1 overflow-y-auto px-6 py-4">
        {loading ? <p className="flex items-center gap-2 text-sm text-muted-foreground"><LoaderCircle className="size-4 animate-spin" />模型正在比对日志与工作项</p> : null}
        {!loading && !error && !suggestions.length ? <p className="text-sm text-muted-foreground">{unassigned ? `本期 ${unassigned} 条未挂载的日志没有明显对应的工作项。` : "本期日志都已挂载或没有可匹配的工作项。"}</p> : null}
        <ul className="space-y-3">{suggestions.map((item) => <li className="flex gap-3" key={item.worklog_ref}>
          <input aria-label={`采纳建议：${item.worklog_ref}`} checked={Boolean(checked[item.worklog_ref])} className="mt-1" onChange={(event) => setChecked((current) => ({ ...current, [item.worklog_ref]: event.target.checked }))} type="checkbox" />
          <div className="min-w-0 text-sm">
            <p className="break-words"><span className="mr-2 font-mono text-xs text-muted-foreground">{item.worklog_ref}</span>{titles[item.worklog_ref] || ""}</p>
            <p className="mt-0.5 break-words">→ {item.work_item_title}</p>
            {item.reason ? <p className="mt-0.5 break-words text-xs text-muted-foreground">{item.reason}</p> : null}
          </div>
        </li>)}</ul>
        {error ? <p className="mt-3 text-sm text-destructive" role="alert">{error}</p> : null}
      </div>
      <DialogFooter className="shrink-0 border-t px-6 py-4">
        <Button disabled={saving || loading || !selected} onClick={() => void confirm()}>{saving ? <LoaderCircle className="animate-spin" data-icon="inline-start" /> : <Sparkles data-icon="inline-start" />}挂上所选 {selected} 条</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
}
