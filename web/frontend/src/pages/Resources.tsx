import { lazy, Suspense, useEffect, useMemo, useState, type KeyboardEvent as ReactKeyboardEvent } from "react"
import { ArrowDown, ArrowUp, ArrowUpDown, Database, Search, X } from "lucide-react"
import { useNavigate, useOutletContext, useSearchParams } from "react-router-dom"
import { ResourcePollStatus } from "@/components/resources/ResourcePollStatus"
import { ResourceSummaryCards } from "@/components/resources/ResourceSummaryCards"
import { PageHeader } from "@/components/PageHeader"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { formatTime } from "@/lib/api"
import { loadLatestResources, loadResourceRisks } from "@/lib/acquisitionApi"
import { createTriageTaskFromResourceRisk } from "@/lib/triageApi"
import {
  compareValues,
  metricTone,
  nextDirection,
  riskDescription,
  riskTitle,
  type MetricKind,
  type ResourceFinding,
  type SortDirection,
} from "@/lib/resourceView"
import { cn } from "@/lib/utils"
import type {
  ResourceBusinessMetric,
  ResourceChartTarget,
  ResourceNode,
  ResourceRisksResponse,
  ResourceService,
  AppShellOutletContext,
  ResourcesLatestResponse,
} from "@/types"

type ResourceTab = "overview" | "nodes" | "services" | "business"
type NodeSortKey = "identity" | "cpu" | "load" | "memory" | "available" | "disk"
type ServiceSortKey = "service" | "node" | "heap" | "heapMax" | "rss" | "status"
type BusinessSortKey = "service" | "metric" | "node" | "value" | "status" | "severity" | "updated"
type SortState = { key: string; direction: SortDirection }

const NATURAL = new Intl.Collator("zh-CN", { numeric: true, sensitivity: "base" })
const ResourceMetricDialog = lazy(() => import("@/components/resources/ResourceMetricDialog"))

function pct(value: number | null | undefined) {
  return value == null ? "—" : `${value.toFixed(1)}%`
}

function gib(value: number | null | undefined) {
  return value == null ? "—" : `${(value / 1024 ** 3).toFixed(1)} GiB`
}

function businessValue(metric: ResourceBusinessMetric) {
  if (metric.value == null) return "—"
  if (metric.unit === "seconds") return `${metric.value.toFixed(1)} s`
  if (metric.unit === "percent") return `${metric.value.toFixed(1)}%`
  if (metric.unit === "per_second") return `${metric.value.toFixed(2)} /s`
  if (metric.unit === "count") return metric.value.toLocaleString("zh-CN", { maximumFractionDigits: 1 })
  return metric.value.toLocaleString("zh-CN", { maximumFractionDigits: 2 })
}

function maxDisk(node: ResourceNode) {
  const values = node.filesystems
    .filter((item) => item.kind === "local" && item.used_pct != null)
    .map((item) => item.used_pct as number)
  return values.length ? Math.max(...values) : null
}

function loadRatio(node: ResourceNode) {
  const load = node.metrics.load1
  const cores = node.metrics.cpu_cores
  return load != null && cores ? load / cores : null
}

function MetricValue({ display, kind, value, neutral = false }: {
  display: string
  kind: MetricKind
  value: number | null | undefined
  neutral?: boolean
}) {
  return <span className={cn(
    "font-medium tabular-nums",
    neutral
      ? value == null ? "text-muted-foreground" : "text-emerald-600 dark:text-emerald-400"
      : metricTone(value, kind),
  )}>{display}</span>
}

function SortHead({ label, column, sort, onSort }: {
  label: string
  column: string
  sort: SortState
  onSort: (column: string) => void
}) {
  const active = sort.key === column
  const Icon = !active ? ArrowUpDown : sort.direction === "asc" ? ArrowUp : ArrowDown
  return <TableHead aria-sort={active ? (sort.direction === "asc" ? "ascending" : "descending") : "none"}>
    <button className="inline-flex items-center gap-1 font-medium hover:text-foreground" onClick={() => onSort(column)} type="button">
      {label}<Icon className={cn("size-3.5", active ? "text-foreground" : "text-muted-foreground/60")} />
    </button>
  </TableHead>
}

function FilterSelect({ id, label, value, options, onChange }: {
  id: string
  label: string
  value: string
  options: Array<{ value: string; label: string }>
  onChange: (value: string) => void
}) {
  return <div className="min-w-0 flex-1"><label className="mb-1.5 block text-xs font-medium" htmlFor={id}>{label}</label><select className="flex h-9 w-full rounded-md border border-input bg-background px-3 text-sm" id={id} onChange={(event) => onChange(event.target.value)} value={value}>{options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select></div>
}

function statusClass(status: string) {
  if (status === "ok" || status === "healthy") return "border-emerald-500/40 text-emerald-700 dark:text-emerald-300"
  if (status === "critical" || status === "target_down" || status === "target_missing" || status === "source_error") return "border-red-500/40 text-red-700 dark:text-red-300"
  if (status === "warning" || status === "no_data" || status === "stale") return "border-orange-500/40 text-orange-700 dark:text-orange-300"
  return "text-muted-foreground"
}

function alertTypeLabel(value: string | undefined) {
  return value === "forecast" ? "预测告警" : "现状告警"
}

function alertTypeClass(value: string | undefined) {
  return value === "forecast"
    ? "border-blue-500/50 bg-blue-500/10 text-blue-700 dark:text-blue-300"
    : "border-purple-500/50 bg-purple-500/10 text-purple-700 dark:text-purple-300"
}

function formatDuration(seconds: number | null | undefined) {
  if (seconds == null || !Number.isFinite(seconds)) return "—"
  if (seconds < 60) return "不足 1 分钟"
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} 分钟`
  const hours = Math.floor(minutes / 60)
  const remainingMinutes = minutes % 60
  if (hours < 24) return remainingMinutes ? `${hours} 小时 ${remainingMinutes} 分钟` : `${hours} 小时`
  const days = Math.floor(hours / 24)
  const remainingHours = hours % 24
  return remainingHours ? `${days} 天 ${remainingHours} 小时` : `${days} 天`
}

function ForecastStatus({ risks }: { risks: ResourceRisksResponse | null }) {
  const items = risks?.forecast_status || []
  if (!items.length) return null
  return <section className="mt-7">
    <div className="mb-3"><h2 className="font-semibold">容量预测状态</h2><p className="text-xs text-muted-foreground">磁盘趋势需至少 12 个有效小时桶</p></div>
    <div className="grid gap-3 md:grid-cols-2">{items.map((item) => {
      const ready = item.status === "ready"
      const health = item.health || (item.finding_count > 0 ? "warning" : "healthy")
      const healthy = ready && health === "healthy"
      const badgeLabel = !ready
        ? item.status === "no_data" ? "暂无数据" : "数据积累中"
        : health === "critical" ? "严重" : health === "warning" ? "警告" : "健康"
      const badgeClass = health === "critical"
        ? "border-red-500/40 text-red-700 dark:text-red-300"
        : health === "warning"
          ? "border-orange-500/40 text-orange-700 dark:text-orange-300"
          : ready
            ? "border-emerald-500/40 text-emerald-700 dark:text-emerald-300"
            : "border-amber-500/40 text-amber-700 dark:text-amber-300"
      return <Card key={item.platform}><CardHeader className="pb-3"><div className="flex items-center justify-between gap-3"><CardTitle className="text-sm">{item.platform.toUpperCase()} 磁盘容量预测</CardTitle><Badge className={badgeClass} variant="outline">{badgeLabel}</Badge></div><CardDescription>{ready
        ? healthy
          ? `未来 ${item.warning_eta_hours.toFixed(0)} 小时内无挂载点预计触线。`
          : `${item.finding_count} 个挂载点预计触线。`
        : `有效小时桶 ${item.observed_hours} / ${item.required_observed_hours}。`}</CardDescription></CardHeader><CardContent className="space-y-1 text-xs text-muted-foreground"><p>可预测挂载点 {item.ready_series_count} / {item.series_count} · 覆盖 {item.span_hours.toFixed(1)} 小时</p><p>更新 {item.last_updated_at ? formatTime(item.last_updated_at) : "—"}{item.has_data_gaps ? ` · 最大中断 ${formatDuration(item.largest_gap_seconds)}` : " · 采集连续"}</p></CardContent></Card>
    })}</div>
  </section>
}

function RiskFindings({ risks, onOpen, onTriage, busyKey }: {
  risks: ResourceRisksResponse | null
  onOpen: (finding: ResourceFinding) => void
  onTriage: (finding: ResourceFinding) => void
  busyKey: string | null
}) {
  const findings = risks?.findings || []
  return <section className="mt-7 scroll-mt-20" id="risk-findings">
    <div className="mb-3"><h2 className="font-semibold">风险发现</h2><p className="text-xs text-muted-foreground">基于最新成功快照与历史窗口，每次资源采集完成后重新计算 · 现状告警表示近期值越过阈值，预测告警表示趋势预计触线</p></div>
    {findings.length ? <div className="grid gap-3">{findings.map((finding) => {
      const tagClass = finding.severity === "critical"
        ? "border-red-500/50 bg-red-500/10 text-red-700 dark:text-red-300"
        : "border-amber-500/50 bg-amber-500/10 text-amber-700 dark:text-amber-300"
      return (
        <Card
          aria-label={`查看风险 ${riskTitle(finding)} 对应资源`}
          className="hover:bg-muted/35"
          key={finding.key}
        >
          <CardHeader className="flex-row items-start gap-3">
            <button
              className="flex min-w-0 flex-1 items-start gap-3 text-left"
              onClick={() => onOpen(finding)}
              type="button"
            >
              <Database className="mt-0.5 size-4 shrink-0 text-foreground" />
              <span className="min-w-0 flex-1">
                <span className="mb-1 flex flex-wrap items-center gap-2">
                  <span className="font-semibold leading-none tracking-tight text-sm text-foreground">{riskTitle(finding)}</span>
                  <Badge className={alertTypeClass(finding.alert_type)} variant="outline">{alertTypeLabel(finding.alert_type)}</Badge>
                  <Badge className={tagClass} variant="outline">{finding.severity}</Badge>
                </span>
                <span className="block text-sm leading-6 text-foreground/80">{riskDescription(finding)}</span>
                <p className="mt-2 text-xs text-muted-foreground">
                  持续时间 {formatDuration(finding.duration_seconds)} · 最后更新 {finding.last_updated_at ? formatTime(finding.last_updated_at) : "—"}
                </p>
              </span>
            </button>
            <Button
              aria-label={`排查 ${riskTitle(finding)}`}
              disabled={busyKey !== null}
              onClick={(event) => {
                event.stopPropagation()
                onTriage(finding)
              }}
              onKeyDown={(event) => event.stopPropagation()}
              size="sm"
              type="button"
              variant="outline"
            >
              <Search data-icon="inline-start" />
              {busyKey === finding.key ? "创建中" : "排查"}
            </Button>
          </CardHeader>
        </Card>
      )
    })}</div> : <Card><CardContent className="py-6 text-sm text-emerald-700 dark:text-emerald-300">当前快照窗口内没有达到风险判定条件的资源。</CardContent></Card>}
  </section>
}

export default function Resources() {
  const { resourceSummary: summary } = useOutletContext<AppShellOutletContext>()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const requestedTab = searchParams.get("tab")
  const activeTab: ResourceTab = requestedTab === "nodes" || requestedTab === "services" || requestedTab === "business" ? requestedTab : "overview"
  const [data, setData] = useState<ResourcesLatestResponse | null>(null)
  const [risks, setRisks] = useState<ResourceRisksResponse | null>(null)
  const [error, setError] = useState("")
  const [nodePlatform, setNodePlatform] = useState("all")
  const [nodeName, setNodeName] = useState("all")
  const [nodeSort, setNodeSort] = useState<SortState>({ key: "identity", direction: "asc" })
  const [serviceName, setServiceName] = useState("all")
  const [servicePlatform, setServicePlatform] = useState("all")
  const [serviceNode, setServiceNode] = useState("all")
  const [serviceStatus, setServiceStatus] = useState("all")
  const [serviceSort, setServiceSort] = useState<SortState>({ key: "service", direction: "asc" })
  const [businessService, setBusinessService] = useState("all")
  const [businessMetric, setBusinessMetric] = useState("all")
  const [businessPlatform, setBusinessPlatform] = useState("all")
  const [businessNode, setBusinessNode] = useState("all")
  const [businessStatus, setBusinessStatus] = useState("all")
  const [businessSeverity, setBusinessSeverity] = useState("all")
  const [businessSort, setBusinessSort] = useState<SortState>({ key: "service", direction: "asc" })
  const [chartTarget, setChartTarget] = useState<ResourceChartTarget | null>(null)
  const [hideEmpty, setHideEmpty] = useState({ heap: false, heapMax: false, rss: false })
  const [riskTaskBusy, setRiskTaskBusy] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    const load = () => Promise.all([
        loadLatestResources(controller.signal),
        loadResourceRisks(controller.signal),
      ]).then(([latest, findings]) => {
        setData(latest); setRisks(findings); setError("")
      }).catch((reason) => {
        if ((reason as Error).name !== "AbortError") setError((reason as Error).message)
      })
    void load()
    const refreshMs = Math.min(60_000, Math.max(15_000, (summary?.interval_seconds || 60) * 1000))
    const timer = window.setInterval(() => void load(), refreshMs)
    return () => { window.clearInterval(timer); controller.abort() }
  }, [summary?.interval_seconds, summary?.last_collected_at])

  const nodes = useMemo(
    () => (data?.items || []).flatMap((item) => item.nodes),
    [data],
  )
  const services = useMemo(() => {
    const observed = (data?.items || []).flatMap((item) => item.services)
    const observedKeys = new Set(observed.map(
      (item) => `${item.platform}:${item.node}:${item.job}:${item.role || ""}`,
    ))
    const empty: ResourceService[] = (data?.catalog.services || [])
      .filter((item) => item.status !== "ok" && !observedKeys.has(
        `${item.platform}:${item.node}:${item.job}:${item.role || ""}`,
      ))
      .map((item) => ({
        ...item, role: item.role || null, sample_at: null,
        metrics: {}, gc: {}, old_gen_after_gc_bytes: {},
      }))
    return [...observed, ...empty]
  }, [data])
  const businessMetrics = useMemo(
    () => (data?.items || []).flatMap((item) => item.business_metrics || []),
    [data],
  )

  const nodeNames = useMemo(() => [...new Set(nodes
    .filter((node) => nodePlatform === "all" || node.platform === nodePlatform)
    .map((node) => node.node))].sort(NATURAL.compare), [nodePlatform, nodes])
  const serviceNames = useMemo(
    () => [...new Set(services.map((service) => service.service))].sort(NATURAL.compare),
    [services],
  )
  const serviceNodes = useMemo(() => [...new Set(services
    .filter((service) => servicePlatform === "all" || service.platform === servicePlatform)
    .map((service) => service.node))].sort(NATURAL.compare), [servicePlatform, services])
  const serviceStatuses = useMemo(
    () => [...new Set(services.map((service) => service.status))].sort(NATURAL.compare),
    [services],
  )
  const nodePlatforms = useMemo(
    () => [...new Set(nodes.map((node) => node.platform))].sort(NATURAL.compare),
    [nodes],
  )
  const servicePlatforms = useMemo(
    () => [...new Set(services.map((service) => service.platform))].sort(NATURAL.compare),
    [services],
  )
  const businessServices = useMemo(
    () => [...new Set(businessMetrics.map((item) => item.service))].sort(NATURAL.compare),
    [businessMetrics],
  )
  const businessMetricNames = useMemo(
    () => [...new Set(businessMetrics.map((item) => item.metric))].sort(NATURAL.compare),
    [businessMetrics],
  )
  const businessMetricLabels = useMemo(() => Object.fromEntries(
    businessMetrics.map((item) => [item.metric, item.label]),
  ), [businessMetrics])
  const businessPlatforms = useMemo(
    () => [...new Set(businessMetrics.map((item) => item.platform))].sort(NATURAL.compare),
    [businessMetrics],
  )
  const businessNodes = useMemo(() => [...new Set(businessMetrics
    .filter((item) => businessPlatform === "all" || item.platform === businessPlatform)
    .map((item) => item.node))].sort(NATURAL.compare), [businessMetrics, businessPlatform])
  const businessStatuses = useMemo(
    () => [...new Set(businessMetrics.map((item) => item.status))].sort(NATURAL.compare),
    [businessMetrics],
  )
  const businessSeverities = useMemo(
    () => [...new Set(businessMetrics.map((item) => item.severity))].sort(NATURAL.compare),
    [businessMetrics],
  )

  const sortedNodes = useMemo(() => {
    const filtered = nodes.filter((node) => (
      (nodePlatform === "all" || node.platform === nodePlatform)
      && (nodeName === "all" || node.node === nodeName)
    ))
    const value = (node: ResourceNode, key: NodeSortKey) => {
      if (key === "identity") return `${node.platform}:${node.node}`
      if (key === "cpu") return node.metrics.cpu_used_pct
      if (key === "load") return loadRatio(node)
      if (key === "memory") return node.metrics.memory_used_pct
      if (key === "available") return node.metrics.memory_available_bytes
      return maxDisk(node)
    }
    return [...filtered].sort((left, right) => compareValues(
      value(left, nodeSort.key as NodeSortKey), value(right, nodeSort.key as NodeSortKey),
      nodeSort.direction,
    ))
  }, [nodeName, nodePlatform, nodeSort, nodes])

  const sortedServices = useMemo(() => {
    const filtered = services.filter((service) => {
      if (serviceName !== "all" && service.service !== serviceName) return false
      if (servicePlatform !== "all" && service.platform !== servicePlatform) return false
      if (serviceNode !== "all" && service.node !== serviceNode) return false
      if (serviceStatus !== "all" && service.status !== serviceStatus) return false
      if (hideEmpty.heap && service.metrics.heap_used_pct == null) return false
      if (hideEmpty.heapMax && service.metrics.heap_max_bytes == null) return false
      if (hideEmpty.rss && service.metrics.process_resident_bytes == null) return false
      return true
    })
    const value = (service: ResourceService, key: ServiceSortKey) => {
      if (key === "service") return `${service.service}:${service.component}`
      if (key === "node") return `${service.platform}:${service.node}`
      if (key === "heap") return service.metrics.heap_used_pct
      if (key === "heapMax") return service.metrics.heap_max_bytes
      if (key === "rss") return service.metrics.process_resident_bytes
      return service.status
    }
    return [...filtered].sort((left, right) => compareValues(
      value(left, serviceSort.key as ServiceSortKey), value(right, serviceSort.key as ServiceSortKey),
      serviceSort.direction,
    ))
  }, [hideEmpty, serviceName, serviceNode, servicePlatform, serviceSort, serviceStatus, services])

  const sortedBusinessMetrics = useMemo(() => {
    const filtered = businessMetrics.filter((item) => (
      (businessService === "all" || item.service === businessService)
      && (businessMetric === "all" || item.metric === businessMetric)
      && (businessPlatform === "all" || item.platform === businessPlatform)
      && (businessNode === "all" || item.node === businessNode)
      && (businessStatus === "all" || item.status === businessStatus)
      && (businessSeverity === "all" || item.severity === businessSeverity)
    ))
    const value = (item: ResourceBusinessMetric, key: BusinessSortKey) => {
      if (key === "service") return `${item.service}:${item.job}`
      if (key === "metric") return item.label
      if (key === "node") return `${item.platform}:${item.node}`
      if (key === "value") return item.value
      if (key === "updated") return item.sample_at
      if (key === "severity") return item.severity
      return item.status
    }
    return [...filtered].sort((left, right) => compareValues(
      value(left, businessSort.key as BusinessSortKey),
      value(right, businessSort.key as BusinessSortKey),
      businessSort.direction,
    ))
  }, [businessMetric, businessMetrics, businessNode, businessPlatform, businessService, businessSeverity, businessSort, businessStatus])

  function changeTab(value: string) {
    const tab = value as ResourceTab
    setSearchParams(tab === "overview" ? {} : { tab }, { replace: true })
  }
  function sortNodes(column: string) {
    setNodeSort((current) => ({ key: column, direction: nextDirection(current.key, column, current.direction) }))
  }
  function sortServices(column: string) {
    setServiceSort((current) => ({ key: column, direction: nextDirection(current.key, column, current.direction) }))
  }
  function sortBusiness(column: string) {
    setBusinessSort((current) => ({ key: column, direction: nextDirection(current.key, column, current.direction) }))
  }
  function openChartOnKey(
    event: ReactKeyboardEvent<HTMLTableRowElement>, target: ResourceChartTarget,
  ) {
    if (event.key !== "Enter" && event.key !== " ") return
    event.preventDefault()
    setChartTarget(target)
  }
  function openFinding(finding: ResourceFinding) {
    const evidence = finding.evidence || {}
    let tab: ResourceTab = "nodes"
    let section = "node-resources"
    if (finding.resource === "business") {
      tab = "business"
      section = "business-metrics"
      setBusinessPlatform(finding.platform)
      setBusinessNode(finding.node)
      setBusinessService(String(evidence.service || "all"))
      setBusinessMetric(String(evidence.metric || "all"))
      setBusinessStatus("all")
      setBusinessSeverity(finding.severity)
    } else if (finding.resource === "jvm") {
      tab = "services"
      section = "service-resources"
      setServicePlatform(finding.platform)
      setServiceNode(finding.node)
      setServiceName(String(evidence.job || "").split("_")[0] || "all")
      setServiceStatus("all")
    } else {
      setNodePlatform(finding.platform)
      setNodeName(finding.node)
    }
    setSearchParams({ tab }, { replace: true })
    window.setTimeout(() => {
      document.getElementById(section)?.scrollIntoView({ behavior: "smooth", block: "start" })
    }, 0)
  }

  async function startRiskInvestigation(finding: ResourceFinding) {
    setError("")
    setRiskTaskBusy(finding.key)
    try {
      const detail = await createTriageTaskFromResourceRisk(finding.key)
      navigate(`/triage?task=${encodeURIComponent(detail.task.task_id)}`)
    } catch (reason) {
      setError((reason as Error).message || "资源风险排查任务创建失败")
    } finally {
      setRiskTaskBusy(null)
    }
  }

  return <div id="resource-page-top">
    <PageHeader title="资源状态" meta={summary?.last_collected_at
      ? `最近采集 ${formatTime(summary.last_collected_at)} · 风险与预测在每次采集后更新`
      : "等待首次资源采集"} />
    {error ? <p className="mb-4 text-sm text-destructive">{error}</p> : null}
    <Tabs onValueChange={changeTab} value={activeTab}>
      <TabsList aria-label="资源状态分类" className="mb-5">
        <TabsTrigger value="overview">概览</TabsTrigger>
        <TabsTrigger value="nodes">节点资源</TabsTrigger>
        <TabsTrigger value="services">服务内存</TabsTrigger>
        <TabsTrigger value="business">业务指标</TabsTrigger>
      </TabsList>

      <TabsContent value="overview">
        <ResourceSummaryCards summary={summary} />
        <div className="mt-6"><ResourcePollStatus summary={summary} /></div>
        <ForecastStatus risks={risks} />
        <RiskFindings busyKey={riskTaskBusy} onOpen={openFinding} onTriage={(finding) => void startRiskInvestigation(finding)} risks={risks} />
      </TabsContent>

      <TabsContent className="scroll-mt-20" id="node-resources" value="nodes">
        <div className="mb-3"><h2 className="font-semibold">节点资源</h2><p className="text-xs text-muted-foreground">内存按 MemAvailable 口径；最高磁盘为本地 xfs/ext4 挂载点的最高使用率</p></div>
        <div className="mb-4 flex flex-wrap items-end gap-3 rounded-md border p-3">
          <FilterSelect id="node-platform-filter" label="平台" onChange={setNodePlatform} options={[{ value: "all", label: "全部平台" }, ...nodePlatforms.map((platform) => ({ value: platform, label: platform.toUpperCase() }))]} value={nodePlatform} />
          <FilterSelect id="node-name-filter" label="节点" onChange={setNodeName} options={[{ value: "all", label: "全部节点" }, ...nodeNames.map((node) => ({ value: node, label: node }))]} value={nodeName} />
          <Button onClick={() => { setNodePlatform("all"); setNodeName("all") }} size="sm" type="button" variant="outline"><X />清空筛选</Button>
        </div>
        <Table><TableHeader><TableRow>
          <SortHead column="identity" label="平台/节点" onSort={sortNodes} sort={nodeSort} />
          <SortHead column="cpu" label="CPU" onSort={sortNodes} sort={nodeSort} />
          <SortHead column="load" label="Load / 核" onSort={sortNodes} sort={nodeSort} />
          <SortHead column="memory" label="内存" onSort={sortNodes} sort={nodeSort} />
          <SortHead column="available" label="可用内存" onSort={sortNodes} sort={nodeSort} />
          <SortHead column="disk" label="最高磁盘" onSort={sortNodes} sort={nodeSort} />
          <TableHead>状态</TableHead>
        </TableRow></TableHeader><TableBody>{sortedNodes.map((node) => {
          const cpu = node.metrics.cpu_used_pct
          const load = loadRatio(node)
          const memory = node.metrics.memory_used_pct
          const disk = maxDisk(node)
          const target: ResourceChartTarget = { kind: "node", platform: node.platform, node: node.node, title: `${node.platform.toUpperCase()} / ${node.node}` }
          return <TableRow aria-label={`查看 ${target.title} 历史资源图表`} className="cursor-pointer hover:bg-muted/50" key={`${node.platform}:${node.node}`} onClick={() => setChartTarget(target)} onKeyDown={(event) => openChartOnKey(event, target)} role="button" tabIndex={0}><TableCell><strong>{node.node}</strong><div className="text-xs uppercase text-muted-foreground">{node.platform}</div></TableCell><TableCell><MetricValue display={pct(cpu)} kind="cpu" value={cpu} /></TableCell><TableCell><MetricValue display={`${node.metrics.load1?.toFixed(2) ?? "—"} / ${node.metrics.cpu_cores ?? "—"}`} kind="load" value={load} /></TableCell><TableCell><MetricValue display={pct(memory)} kind="memory" value={memory} /></TableCell><TableCell><MetricValue display={gib(node.metrics.memory_available_bytes)} kind="memory" value={memory} /></TableCell><TableCell><MetricValue display={pct(disk)} kind="disk" value={disk} /></TableCell><TableCell><Badge className={statusClass(node.status)} variant="outline">{node.status}</Badge></TableCell></TableRow>
        })}{sortedNodes.length === 0 ? <TableRow><TableCell className="h-24 text-center text-muted-foreground" colSpan={7}>没有符合条件的节点</TableCell></TableRow> : null}</TableBody></Table>
      </TabsContent>

      <TabsContent className="scroll-mt-20" id="service-resources" value="services">
        <div className="mb-3"><h2 className="font-semibold">服务内存</h2><p className="text-xs text-muted-foreground">无 JMX、target down 与无数据分别显示；Heap 为使用率，Heap Max/RSS 为 GiB</p></div>
        <div className="mb-4 rounded-md border p-3">
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <FilterSelect id="service-name-filter" label="服务" onChange={setServiceName} options={[{ value: "all", label: "全部服务" }, ...serviceNames.map((service) => ({ value: service, label: service }))]} value={serviceName} />
            <FilterSelect id="service-platform-filter" label="平台" onChange={setServicePlatform} options={[{ value: "all", label: "全部平台" }, ...servicePlatforms.map((platform) => ({ value: platform, label: platform.toUpperCase() }))]} value={servicePlatform} />
            <FilterSelect id="service-node-filter" label="节点" onChange={setServiceNode} options={[{ value: "all", label: "全部节点" }, ...serviceNodes.map((node) => ({ value: node, label: node }))]} value={serviceNode} />
            <FilterSelect id="service-status-filter" label="状态" onChange={setServiceStatus} options={[{ value: "all", label: "全部状态" }, ...serviceStatuses.map((status) => ({ value: status, label: status }))]} value={serviceStatus} />
          </div>
          <div className="mt-3 flex flex-wrap items-center justify-between gap-3"><Button onClick={() => { setServiceName("all"); setServicePlatform("all"); setServiceNode("all"); setServiceStatus("all"); setHideEmpty({ heap: false, heapMax: false, rss: false }) }} size="sm" type="button" variant="outline"><X />清空筛选</Button><span className="text-xs text-muted-foreground">显示 {sortedServices.length} / {services.length} 条</span></div>
          <div className="mt-3 flex flex-wrap gap-x-5 gap-y-2"><label className="flex cursor-pointer items-center gap-2 text-sm"><Checkbox checked={hideEmpty.heap} onCheckedChange={(heap) => setHideEmpty((current) => ({ ...current, heap: Boolean(heap) }))} />隐藏 Heap 空值</label><label className="flex cursor-pointer items-center gap-2 text-sm"><Checkbox checked={hideEmpty.heapMax} onCheckedChange={(heapMax) => setHideEmpty((current) => ({ ...current, heapMax: Boolean(heapMax) }))} />隐藏 Heap Max 空值</label><label className="flex cursor-pointer items-center gap-2 text-sm"><Checkbox checked={hideEmpty.rss} onCheckedChange={(rss) => setHideEmpty((current) => ({ ...current, rss: Boolean(rss) }))} />隐藏 RSS 空值</label></div>
        </div>
        <Table><TableHeader><TableRow>
          <SortHead column="service" label="服务" onSort={sortServices} sort={serviceSort} />
          <SortHead column="node" label="平台/节点" onSort={sortServices} sort={serviceSort} />
          <TableHead>角色</TableHead>
          <SortHead column="heap" label="Heap" onSort={sortServices} sort={serviceSort} />
          <SortHead column="heapMax" label="Heap Max" onSort={sortServices} sort={serviceSort} />
          <SortHead column="rss" label="RSS" onSort={sortServices} sort={serviceSort} />
          <SortHead column="status" label="状态" onSort={sortServices} sort={serviceSort} />
        </TableRow></TableHeader><TableBody>{sortedServices.map((service) => {
          const heap = service.metrics.heap_used_pct
          const target: ResourceChartTarget = { kind: "service", platform: service.platform, node: service.node, title: `${service.service} / ${service.node}`, job: service.job, role: service.role }
          return <TableRow aria-label={`查看 ${target.title} 历史资源图表`} className="cursor-pointer [content-visibility:auto] [contain-intrinsic-size:48px] hover:bg-muted/50" key={`${service.platform}:${service.node}:${service.job}:${service.role || ""}:${service.component}`} onClick={() => setChartTarget(target)} onKeyDown={(event) => openChartOnKey(event, target)} role="button" tabIndex={0}><TableCell><strong>{service.service}</strong><div className="font-mono text-[11px] text-muted-foreground">{service.job}</div></TableCell><TableCell>{service.platform.toUpperCase()} / {service.node}</TableCell><TableCell>{service.role || "—"}</TableCell><TableCell><MetricValue display={pct(heap)} kind="heap" value={heap} /></TableCell><TableCell><MetricValue display={gib(service.metrics.heap_max_bytes)} kind="heap" neutral value={service.metrics.heap_max_bytes} /></TableCell><TableCell><MetricValue display={gib(service.metrics.process_resident_bytes)} kind="heap" neutral value={service.metrics.process_resident_bytes} /></TableCell><TableCell><Badge className={statusClass(service.status)} variant="outline">{service.status}</Badge></TableCell></TableRow>
        })}{sortedServices.length === 0 ? <TableRow><TableCell className="h-24 text-center text-muted-foreground" colSpan={7}>没有符合条件的服务</TableCell></TableRow> : null}</TableBody></Table>
      </TabsContent>

      <TabsContent className="scroll-mt-20" id="business-metrics" value="business">
        <div className="mb-3"><h2 className="font-semibold">业务指标</h2><p className="text-xs text-muted-foreground">按集群业务 exporter 采集；当前包括 MySQL 主从延迟、连接使用率、QPS、慢查询速率、行锁等待和 Buffer Pool 使用率</p></div>
        <div className="mb-4 rounded-md border p-3">
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            <FilterSelect id="business-service-filter" label="服务" onChange={setBusinessService} options={[{ value: "all", label: "全部服务" }, ...businessServices.map((service) => ({ value: service, label: service }))]} value={businessService} />
            <FilterSelect id="business-metric-filter" label="指标" onChange={setBusinessMetric} options={[{ value: "all", label: "全部指标" }, ...businessMetricNames.map((metric) => ({ value: metric, label: businessMetricLabels[metric] || metric }))]} value={businessMetric} />
            <FilterSelect id="business-platform-filter" label="平台" onChange={setBusinessPlatform} options={[{ value: "all", label: "全部平台" }, ...businessPlatforms.map((platform) => ({ value: platform, label: platform.toUpperCase() }))]} value={businessPlatform} />
            <FilterSelect id="business-node-filter" label="节点" onChange={setBusinessNode} options={[{ value: "all", label: "全部节点" }, ...businessNodes.map((node) => ({ value: node, label: node }))]} value={businessNode} />
            <FilterSelect id="business-status-filter" label="采集状态" onChange={setBusinessStatus} options={[{ value: "all", label: "全部状态" }, ...businessStatuses.map((status) => ({ value: status, label: status }))]} value={businessStatus} />
            <FilterSelect id="business-severity-filter" label="风险级别" onChange={setBusinessSeverity} options={[{ value: "all", label: "全部级别" }, ...businessSeverities.map((severity) => ({ value: severity, label: severity }))]} value={businessSeverity} />
          </div>
          <div className="mt-3 flex flex-wrap items-center justify-between gap-3"><Button onClick={() => { setBusinessService("all"); setBusinessMetric("all"); setBusinessPlatform("all"); setBusinessNode("all"); setBusinessStatus("all"); setBusinessSeverity("all") }} size="sm" type="button" variant="outline"><X />清空筛选</Button><p className="text-xs text-muted-foreground">显示 {sortedBusinessMetrics.length} / {businessMetrics.length} 条</p></div>
        </div>
        <Table><TableHeader><TableRow>
          <SortHead column="service" label="服务" onSort={sortBusiness} sort={businessSort} />
          <SortHead column="metric" label="指标" onSort={sortBusiness} sort={businessSort} />
          <SortHead column="node" label="平台/节点" onSort={sortBusiness} sort={businessSort} />
          <SortHead column="value" label="当前值" onSort={sortBusiness} sort={businessSort} />
          <SortHead column="status" label="采集状态" onSort={sortBusiness} sort={businessSort} />
          <SortHead column="severity" label="风险级别" onSort={sortBusiness} sort={businessSort} />
          <SortHead column="updated" label="更新时间" onSort={sortBusiness} sort={businessSort} />
        </TableRow></TableHeader><TableBody>{sortedBusinessMetrics.map((item) => {
          const target: ResourceChartTarget = { kind: "business", platform: item.platform, node: item.node, title: `${item.service} / ${item.node}`, job: item.job, service: item.service }
          return <TableRow aria-label={`查看 ${target.title} 业务指标历史图表`} className="cursor-pointer hover:bg-muted/50" key={`${item.platform}:${item.node}:${item.job}:${item.metric}`} onClick={() => setChartTarget(target)} onKeyDown={(event) => openChartOnKey(event, target)} role="button" tabIndex={0}><TableCell><strong>{item.service}</strong><div className="font-mono text-[11px] text-muted-foreground">{item.job}</div></TableCell><TableCell><strong className="text-sm">{item.label}</strong><div className="font-mono text-[11px] text-muted-foreground">{item.metric}</div></TableCell><TableCell>{item.platform.toUpperCase()} / {item.node}{item.details.master_host ? <div className="text-[11px] text-muted-foreground">主库 {item.details.master_host}</div> : null}</TableCell><TableCell className="font-medium tabular-nums">{businessValue(item)}</TableCell><TableCell><Badge className={statusClass(item.status)} variant="outline">{item.status}</Badge></TableCell><TableCell><Badge className={statusClass(item.severity)} variant="outline">{item.severity}</Badge></TableCell><TableCell className="text-xs text-muted-foreground">{item.sample_at ? formatTime(item.sample_at) : "—"}</TableCell></TableRow>
        })}{sortedBusinessMetrics.length === 0 ? <TableRow><TableCell className="h-24 text-center text-muted-foreground" colSpan={7}>暂无业务指标；新字段会在中台重启后的下一轮采集写入</TableCell></TableRow> : null}</TableBody></Table>
      </TabsContent>
    </Tabs>
    {chartTarget ? <Suspense fallback={null}><ResourceMetricDialog onClose={() => setChartTarget(null)} target={chartTarget} /></Suspense> : null}
  </div>
}
