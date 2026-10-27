import { type FormEvent, useCallback, useEffect, useRef, useState } from "react"
import { ChevronDown, ChevronLeft, ChevronRight, RefreshCw, Search } from "lucide-react"
import { Link, useSearchParams } from "react-router-dom"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { AlertMeta } from "@/components/AlertMeta"
import { PageHeader } from "@/components/PageHeader"
import { SeverityBadge } from "@/components/SeverityBadge"
import { StatusBadge } from "@/components/StatusBadge"
import { fetchJson, formatTime, toApiTimestamp } from "@/lib/api"
import {
  ALERT_CLASS_FILTER_OPTIONS,
  alertClassSelectionSummary,
  normalizeAlertClassFilter,
  type AlertClassFilter,
} from "@/lib/alertFilters"
import type { AlertListResponse, FacetsResponse } from "@/types"

type FacetField = "source" | "platform" | "severity" | "service"
type Filters = Record<FacetField | "host" | "from" | "to", string> & {
  status: string
  chronic: string
  classes: AlertClassFilter[]
  first_seen_from: string
  suppressed_today: string
}

const facetFields: FacetField[] = ["source", "platform", "severity", "service"]

function filtersFromUrl(params: URLSearchParams): Filters {
  return {
    source: params.get("source") || "",
    platform: params.get("platform") || "",
    host: params.get("host") || "",
    severity: params.get("severity") || "",
    service: params.get("service") || "",
    status: params.get("status") || "all",
    chronic: params.get("chronic") || "all",
    classes: normalizeAlertClassFilter(params.get("class")),
    from: toDatetimeLocal(params.get("from")),
    to: toDatetimeLocal(params.get("to")),
    first_seen_from: params.get("first_seen_from") || "",
    suppressed_today: params.get("suppressed_today") || "",
  }
}

function toDatetimeLocal(value: string | null) {
  if (!value) return ""
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ""
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000)
  return local.toISOString().slice(0, 16)
}

export default function Alerts() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [draft, setDraft] = useState<Filters>(() => filtersFromUrl(searchParams))
  const [filters, setFilters] = useState<Filters>(() => filtersFromUrl(searchParams))
  const [facets, setFacets] = useState<FacetsResponse | null>(null)
  const [offset, setOffset] = useState(0)
  const [data, setData] = useState<AlertListResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const [validationError, setValidationError] = useState("")
  const limit = 50

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setLoading(true); setError("")
      const params = new URLSearchParams({ limit: String(limit), offset: String(offset) })
      Object.entries(filters).forEach(([key, value]) => {
        if (Array.isArray(value)) {
          if (value.length) params.set("class", value.join(","))
        } else if (value && value !== "all") {
          const apiValue = key === "from" || key === "to"
            ? toApiTimestamp(value, key)
            : value
          params.set(key, apiValue)
        }
      })
      setData(await fetchJson<AlertListResponse>(`/api/alerts?${params}`, signal))
    } catch (reason) {
      if ((reason as Error).name !== "AbortError") setError((reason as Error).message)
    } finally { setLoading(false) }
  }, [filters, offset])

  const loadFacets = useCallback(async (signal?: AbortSignal) => {
    try {
      setFacets(await fetchJson<FacetsResponse>("/api/alerts/facets", signal))
    } catch (reason) {
      if ((reason as Error).name !== "AbortError") setFacets(null)
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => controller.abort()
  }, [load])

  useEffect(() => {
    const controller = new AbortController()
    void loadFacets(controller.signal)
    return () => controller.abort()
  }, [loadFacets])

  useEffect(() => {
    const next = filtersFromUrl(searchParams)
    setDraft(next)
    setFilters(next)
    setOffset(0)
  }, [searchParams])

  function refresh() {
    void load()
    void loadFacets()
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    try {
      toApiTimestamp(draft.from, "from")
      toApiTimestamp(draft.to, "to")
    } catch (reason) {
      setValidationError((reason as Error).message)
      return
    }
    const nextParams = new URLSearchParams()
    Object.entries(draft).forEach(([key, value]) => {
      if (Array.isArray(value)) {
        if (value.length) nextParams.set("class", value.join(","))
        return
      }
      if (!value || value === "all") return
      nextParams.set(
        key,
        key === "from" || key === "to" ? toApiTimestamp(value, key) : value,
      )
    })
    setValidationError(""); setOffset(0); setFilters(draft); setSearchParams(nextParams)
  }

  return (
    <div>
      <PageHeader title="告警列表" meta={data ? `${data.total} 条结果` : "按来源、节点与状态筛选"} actions={<Button onClick={refresh} size="sm" variant="outline"><RefreshCw data-icon="inline-start" />刷新</Button>} />
      <form className="filter-band" onSubmit={submit}>
        {facetFields.map((field) => {
          const candidates = facets?.[field] || []
          if (candidates.length === 0) {
            return <label className="filter-field" key={field}><span>{field}</span><Input onChange={(event) => setDraft((current) => ({ ...current, [field]: event.target.value }))} value={draft[field]} /></label>
          }
          const options = Array.from(new Set([
            ...candidates,
            ...(draft[field] ? [draft[field]] : []),
          ]))
          return <label className="filter-field" key={field}><span>{field}</span><Select onValueChange={(value) => setDraft((current) => ({ ...current, [field]: value === "all" ? "" : value }))} value={draft[field] || "all"}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="all">全部</SelectItem>{options.map((value) => <SelectItem key={value} value={value}>{value}</SelectItem>)}</SelectGroup></SelectContent></Select></label>
        })}
        <label className="filter-field"><span>host</span><Input onChange={(event) => setDraft((current) => ({ ...current, host: event.target.value }))} value={draft.host} /></label>
        <label className="filter-field"><span>status</span><Select onValueChange={(value) => setDraft((current) => ({ ...current, status: value }))} value={draft.status}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="all">全部</SelectItem><SelectItem value="active">活跃</SelectItem><SelectItem value="resolved">已恢复</SelectItem><SelectItem value="suppressed">已抑制</SelectItem></SelectGroup></SelectContent></Select></label>
        <ClassMultiSelect
          onChange={(classes) => setDraft((current) => ({ ...current, classes }))}
          value={draft.classes}
        />
        <label className="filter-field"><span>from</span><Input aria-invalid={Boolean(validationError)} onChange={(event) => { setValidationError(""); setDraft((current) => ({ ...current, from: event.target.value })) }} type="datetime-local" value={draft.from} /></label>
        <label className="filter-field"><span>to</span><Input aria-invalid={Boolean(validationError)} onChange={(event) => { setValidationError(""); setDraft((current) => ({ ...current, to: event.target.value })) }} type="datetime-local" value={draft.to} /></label>
        <Button className="self-end" type="submit"><Search data-icon="inline-start" />筛选</Button>
      </form>
      {validationError ? <Alert className="mb-4 rounded-md" role="alert" variant="destructive"><AlertDescription>{validationError}</AlertDescription></Alert> : null}
      {data?.filter_notice ? <Alert className="mb-4 rounded-md"><AlertDescription>{data.filter_notice}</AlertDescription></Alert> : null}
      {error ? <p className="mb-4 text-sm text-destructive">{error}</p> : null}
      <div className="border-y">
        <Table>
          <TableHeader><TableRow><TableHead>告警</TableHead><TableHead>来源</TableHead><TableHead>节点</TableHead><TableHead>服务</TableHead><TableHead>级别</TableHead><TableHead>状态</TableHead><TableHead className="text-right">首次出现</TableHead></TableRow></TableHeader>
          <TableBody>
            {(data?.items || []).map((item) => <TableRow key={item.key}>
              <TableCell className="min-w-[240px]">
                <div className="flex flex-wrap items-center gap-2">
                  <Link className="font-medium text-[var(--link)] hover:underline" to={`/alerts/${encodeURIComponent(item.key)}`}>{item.alertname}</Link>
                  <AlertMeta item={item} />
                </div>
                <div className="max-w-[360px] truncate font-mono text-[11px] text-muted-foreground" title={item.key}>{item.key}</div>
              </TableCell>
              <TableCell>{item.source}<div className="text-xs text-muted-foreground">{item.platform}</div></TableCell><TableCell>{item.host || "-"}</TableCell><TableCell>{item.service || "-"}</TableCell><TableCell><SeverityBadge severity={item.severity} /></TableCell><TableCell><StatusBadge value={item.status} /></TableCell><TableCell className="text-right text-xs text-muted-foreground">{formatTime(item.first_seen)}</TableCell>
            </TableRow>)}
            {!loading && data?.items.length === 0 ? <TableRow><TableCell className="h-28 text-center text-muted-foreground" colSpan={7}>没有符合条件的告警</TableCell></TableRow> : null}
          </TableBody>
        </Table>
      </div>
      <div className="mt-4 flex items-center justify-end gap-3 text-xs text-muted-foreground">
        <span>{data ? `${offset + 1}-${Math.min(offset + limit, data.total)} / ${data.total}` : "-"}</span>
        <Button aria-label="上一页" disabled={offset === 0 || loading} onClick={() => setOffset((value) => Math.max(0, value - limit))} size="icon" title="上一页" variant="outline"><ChevronLeft /></Button>
        <Button aria-label="下一页" disabled={!data || offset + limit >= data.total || loading} onClick={() => setOffset((value) => value + limit)} size="icon" title="下一页" variant="outline"><ChevronRight /></Button>
      </div>
    </div>
  )
}

function ClassMultiSelect({
  onChange,
  value,
}: {
  onChange: (value: AlertClassFilter[]) => void
  value: AlertClassFilter[]
}) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const closeOutside = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false)
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false)
    }
    document.addEventListener("pointerdown", closeOutside)
    document.addEventListener("keydown", closeOnEscape)
    return () => {
      document.removeEventListener("pointerdown", closeOutside)
      document.removeEventListener("keydown", closeOnEscape)
    }
  }, [open])

  function toggle(item: AlertClassFilter, checked: boolean) {
    const selected = new Set(value)
    if (checked) selected.add(item)
    else selected.delete(item)
    onChange(
      ALERT_CLASS_FILTER_OPTIONS
        .map((option) => option.value)
        .filter((option) => selected.has(option)),
    )
  }

  return (
    <div className="filter-field">
      <span>class</span>
      <div className="relative" ref={rootRef}>
        <Button
          aria-expanded={open}
          aria-haspopup="listbox"
          className="w-full justify-between px-3 font-normal"
          onClick={() => setOpen((current) => !current)}
          type="button"
          variant="outline"
        >
          <span className="truncate">{alertClassSelectionSummary(value)}</span>
          <ChevronDown className="text-muted-foreground" />
        </Button>
        {open ? (
          <div
            aria-multiselectable="true"
            className="absolute z-50 mt-1 w-full min-w-[140px] rounded-md border bg-popover p-1 text-popover-foreground shadow-md"
            role="listbox"
          >
            {ALERT_CLASS_FILTER_OPTIONS.map((option) => (
              <label
                className="flex cursor-pointer items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent"
                key={option.value}
                role="option"
                aria-selected={value.includes(option.value)}
              >
                <Checkbox
                  checked={value.includes(option.value)}
                  onCheckedChange={(checked) => toggle(option.value, checked)}
                />
                <span>{option.label}</span>
              </label>
            ))}
          </div>
        ) : null}
      </div>
    </div>
  )
}
