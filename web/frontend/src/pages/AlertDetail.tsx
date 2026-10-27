import { useCallback, useEffect, useState } from "react"
import { AlertTriangle, ArrowLeft, BookOpen, ClipboardList, ExternalLink, MessageSquareText, RefreshCw, Volume2, VolumeX } from "lucide-react"
import { Link, useNavigate, useParams } from "react-router-dom"
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Separator } from "@/components/ui/separator"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { PageHeader } from "@/components/PageHeader"
import { SeverityBadge } from "@/components/SeverityBadge"
import { StatusBadge } from "@/components/StatusBadge"
import { fetchJson, formatDuration, formatTime, postJson } from "@/lib/api"
import {
  ALERT_CHART_RANGES,
  hourlyFrequency,
  occurrenceTrend,
  type AlertChartRange,
} from "@/lib/alertCharts"
import { alertTaskDraft } from "@/lib/triageNavigation"
import { WorkItemApiError, createWorkItem } from "@/lib/workItemsApi"
import type { AlertDetailResponse, TimelineEvent } from "@/types"

const SEVERITY_BAR_COLORS: Record<string, string> = { critical: "#b03636", warning: "#d69e2e" }

function RangeToggle({ label, onChange, value }: {
  label: string
  onChange: (next: AlertChartRange) => void
  value: AlertChartRange
}) {
  return (
    <ToggleGroup aria-label={label} onValueChange={(next) => next && onChange(next as AlertChartRange)} size="sm" type="single" value={value} variant="outline">
      {ALERT_CHART_RANGES.map((item) => <ToggleGroupItem key={item.value} value={item.value}>{item.label}</ToggleGroupItem>)}
    </ToggleGroup>
  )
}

function CountChart({ bars, color, interval }: {
  bars: { label: string; count: number }[]
  color: string
  interval: number | "preserveStartEnd"
}) {
  return (
    <div className="h-[220px] w-full">
      <ResponsiveContainer height="100%" width="100%">
        <BarChart data={bars} margin={{ top: 8, right: 8, bottom: 0, left: -18 }}>
          <CartesianGrid stroke="var(--line)" strokeDasharray="3 3" vertical={false} />
          <XAxis axisLine={false} dataKey="label" fontSize={11} interval={interval} tickLine={false} />
          <YAxis allowDecimals={false} axisLine={false} fontSize={11} tickLine={false} />
          <Tooltip contentStyle={{ background: "var(--bg)", border: "1px solid var(--line)", borderRadius: 6 }} />
          <Bar dataKey="count" fill={color} name="出现次数" radius={[3, 3, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

function JsonPanel({ value, empty }: { value: unknown; empty: string }) {
  if (value === null || value === undefined) return <div className="flex min-h-28 items-center justify-center bg-muted px-4 text-sm text-muted-foreground">{empty}</div>
  return <pre className="max-h-[420px] overflow-auto bg-[var(--pre-bg)] p-4 font-mono text-xs leading-5 text-[var(--pre-fg)]">{JSON.stringify(value, null, 2)}</pre>
}

export default function AlertDetail() {
  const { key = "" } = useParams()
  const navigate = useNavigate()
  const [data, setData] = useState<AlertDetailResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const [muteBusy, setMuteBusy] = useState(false)
  const [workItemBusy, setWorkItemBusy] = useState(false)
  const [tlExpanded, setTlExpanded] = useState(false)
  const [freqRange, setFreqRange] = useState<AlertChartRange>("7d")
  const [trendRange, setTrendRange] = useState<AlertChartRange>("7d")

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setLoading(true); setError("")
      setData(await fetchJson<AlertDetailResponse>(`/api/alerts/${encodeURIComponent(key)}`, signal))
    } catch (reason) {
      if ((reason as Error).name !== "AbortError") setError((reason as Error).message)
    } finally { setLoading(false) }
  }, [key])

  useEffect(() => {
    const controller = new AbortController(); void load(controller.signal)
    return () => controller.abort()
  }, [load])

  async function toggleMute() {
    if (!data || muteBusy) return
    const sourceId = data.source_id || data.key.split(":")[0]
    try {
      setMuteBusy(true)
      setError("")
      await postJson(
        `/api/alert-config/${encodeURIComponent(sourceId)}/${encodeURIComponent(data.alertname)}`,
        { notify: data.current.muted },
      )
      await load()
    } catch (reason) {
      setError((reason as Error).message)
    } finally {
      setMuteBusy(false)
    }
  }

  // 告警不自动建工作项:人工点按钮建,同一告警已有未关闭工作项时直接打开它
  async function createAlertWorkItem() {
    if (!data) return
    setWorkItemBusy(true)
    setError("")
    try {
      const { item } = await createWorkItem({
        title: `${data.alertname}${data.host ? ` @ ${data.host}` : ""}`.slice(0, 200),
        source: "alert",
        source_ref: data.key,
      })
      navigate(`/work-items/${item.id}`)
    } catch (reason) {
      if (reason instanceof WorkItemApiError && reason.ownerId) navigate(`/work-items/${reason.ownerId}`)
      else setError((reason as Error).message)
    } finally {
      setWorkItemBusy(false)
    }
  }

  if (loading && !data) return <div className="py-20 text-center text-muted-foreground">正在读取告警详情</div>
  if (error || !data) return <div><Button asChild size="sm" variant="ghost"><Link to="/alerts"><ArrowLeft data-icon="inline-start" />返回列表</Link></Button><p className="mt-10 text-center text-destructive">{error || "告警不存在"}</p></div>

  const indexedOccurrences = data.occurrences.map((item, index) => ({ item, index }))
  const timelineRows = tlExpanded
    ? [...indexedOccurrences].reverse()
    : indexedOccurrences.slice(-3).reverse()
  const startedEvents = [...data.events].reverse().filter((event) => event.type === "new")
  const barColor = SEVERITY_BAR_COLORS[data.current.severity || ""] || "#718096"
  const frequencyBars = hourlyFrequency(data.events, freqRange)
  const trendBars = occurrenceTrend(data.events, trendRange)
  const sumCount = (bars: { count: number }[]) => bars.reduce((total, bar) => total + bar.count, 0)

  return (
    <div>
      <Button asChild className="mb-4" size="sm" variant="ghost"><Link to="/alerts"><ArrowLeft data-icon="inline-start" />返回列表</Link></Button>
      <PageHeader title={data.alertname} meta={data.key} actions={<>
        <Button
          onClick={() => navigate("/triage", {
            state: {triageDraft: alertTaskDraft(data)},
          })}
          variant="outline"
        >
          <MessageSquareText data-icon="inline-start" />
          开始人工排查
        </Button>
        <Button disabled={workItemBusy} onClick={() => void createAlertWorkItem()} variant="outline">
          <ClipboardList data-icon="inline-start" />
          建工作项
        </Button>
        <Button disabled={muteBusy} onClick={() => void toggleMute()} size="default" variant={data.current.muted ? "destructive" : "default"}>
          {data.current.muted ? <VolumeX data-icon="inline-start" /> : <Volume2 data-icon="inline-start" />}
          {data.current.muted ? "推送已关闭(点击恢复)" : "推送开启中(点击静音)"}
        </Button>
        <Button onClick={() => void load()} size="sm" variant="outline"><RefreshCw data-icon="inline-start" />刷新</Button>
      </>} />

      <section className="detail-section pt-0">
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge value={data.current.status} /><SeverityBadge severity={data.current.severity} />{data.current.muted ? <Badge className="status-badge" variant="outline">已静音</Badge> : null}<span className="text-sm text-muted-foreground">{data.source} · {data.platform} · {data.host || "-"} · {data.current.service || "-"}</span>
        </div>
        {data.description ? <p className="mt-3 border-l-2 pl-3 text-sm text-muted-foreground">{data.description}</p> : null}
        <dl className="detail-facts"><div><dt>首次出现</dt><dd>{formatTime(data.current.first_seen)}</dd></div><div><dt>最后出现</dt><dd>{formatTime(data.current.last_seen)}</dd></div><div><dt>出现次数</dt><dd>{data.current.occurrences}</dd></div><div><dt>分类</dt><dd>{data.current.class || "-"}</dd></div>{data.current.source_active_at ? <div><dt>源端触发</dt><dd title="监控源记录的触发时间，比轮询首见更精确">{formatTime(data.current.source_active_at)}</dd></div> : null}</dl>
      </section>

      <section className="detail-section">
        <div className="mb-4 flex flex-wrap items-baseline justify-between gap-3">
          <div><h2>频率分布</h2><p className="text-xs text-muted-foreground">窗口内每次出现按「一天中的钟点」折叠累加 · 共 {sumCount(frequencyBars)} 次</p></div>
          <RangeToggle label="频率分布时间范围" onChange={setFreqRange} value={freqRange} />
        </div>
        <CountChart bars={frequencyBars} color={barColor} interval={2} />
      </section>

      <section className="detail-section">
        <div className="mb-4 flex flex-wrap items-baseline justify-between gap-3">
          <div><h2>出现次数</h2><p className="text-xs text-muted-foreground">{trendRange === "1d" ? "按小时" : "按天"}统计该告警的出现次数 · 共 {sumCount(trendBars)} 次</p></div>
          <RangeToggle label="出现次数时间范围" onChange={setTrendRange} value={trendRange} />
        </div>
        <CountChart bars={trendBars} color={barColor} interval="preserveStartEnd" />
      </section>

      <section className="detail-section"><div className="mb-4"><h2>事件时间线</h2><p className="text-xs text-muted-foreground">每一行代表一次告警周期；活跃表示仍在持续，已恢复表示该周期已闭合。时间、状态、时长和数量统一以生命周期数据为准。</p></div><div className={tlExpanded ? "max-h-[400px] overflow-y-auto" : ""}><Table><TableHeader className="sticky top-0 z-10 bg-[var(--bg)]"><TableRow><TableHead>时间</TableHead><TableHead>状态</TableHead><TableHead>分类</TableHead><TableHead>抑制方</TableHead></TableRow></TableHeader><TableBody>{timelineRows.map(({ item, index }) => {
        const started = startedEvents[index]
        return <TableRow key={`${item.started_at}-${index}`}>
          <TableCell><span title={started && item.started_at !== started.ts ? `源端触发时间；轮询首见于 ${formatTime(started.ts)}` : undefined}>{formatTime(item.started_at)}</span><span className="text-muted-foreground"> → {item.open ? "仍在持续" : formatTime(item.resolved_at)}</span></TableCell>
          <TableCell>{item.open ? `活跃（已持续 ${formatDuration(item.duration_seconds)}）` : `已恢复（持续 ${formatDuration(item.duration_seconds)}）`}</TableCell>
          <TableCell>{started?.alert_class || "-"}</TableCell>
          <TableCell className="font-mono text-xs">{started?.suppressed_by || "-"}</TableCell>
        </TableRow>
      })}</TableBody></Table></div>{data.occurrences.length > 3 ? <Button className="mt-2" onClick={() => setTlExpanded((value) => !value)} size="sm" variant="link">{tlExpanded ? "收起" : `展开全部 (${data.occurrences.length} 次)`}</Button> : null}</section>

      <section className="detail-section"><h2>原始数据</h2>{!data.payload.available ? <Alert className="mb-3 rounded-md"><AlertTriangle /><AlertTitle>原始数据不可用</AlertTitle><AlertDescription>原始数据（采集源返回的完整 payload）只保留在活跃告警的快照里；该告警已恢复，快照条目已移除，时间线只保留事件流，因此无法回看。</AlertDescription></Alert> : null}<JsonPanel empty="该告警已恢复，原始 payload 未保留" value={data.payload.data} /></section>

      <section className="detail-section"><h2>知识库路由</h2>{data.routing ? <div className="flex flex-wrap items-center gap-4"><BookOpen className="size-5 text-[var(--link)]" /><div><strong>{data.routing.doc_title}</strong><p className="font-mono text-xs text-muted-foreground">{data.routing.doc}</p></div>{data.routing.doc_url ? <Button asChild className="ml-auto" size="sm" variant="outline"><Link to={data.routing.doc_url}><BookOpen data-icon="inline-start" />打开文档</Link></Button> : null}</div> : <p className="text-sm text-muted-foreground">未命中知识库路由</p>}</section>

      <section className="detail-section"><h2>去重关系</h2><p className="mb-3 text-xs text-muted-foreground">同一故障常被多个监控源各报一条，去重规则会把它们折叠为一条推送。「实时重算」按当前活跃告警与规则现算，是权威结果；「历史落盘」是通知管道当时写进时间线的抑制记录，可能残缺，仅供对照。</p>{data.dedup.live.excluded_sources.length ? <Alert className="mb-3 rounded-md" variant="destructive"><AlertDescription>{data.dedup.live.excluded_sources.join("、")} 采集失败中，其抑制关系暂不可算。</AlertDescription></Alert> : null}<div className="grid gap-4 md:grid-cols-2"><div><h3>实时重算</h3><p className="mt-2 text-sm">被抑制于：<span className="font-mono text-xs">{data.dedup.live.suppressed_by || "-"}</span></p><p className="mt-1 text-sm">抑制其他：{data.dedup.live.suppresses.length || 0} 条</p></div><div><h3>历史落盘</h3><p className="mt-2 font-mono text-xs">{data.dedup.historical.suppressed_by || "无已落盘关系"}</p>{data.dedup.rule ? <p className="mt-2 text-xs text-muted-foreground">{data.dedup.rule.winner} 优先 · {data.dedup.rule.reason}</p> : null}</div></div>
        {data.peers.length ? <><Separator className="my-5" /><h3>多源对照</h3><Tabs className="mt-3" defaultValue="peer-0"><TabsList>{data.peers.map((peer, index) => <TabsTrigger key={`${peer.key}-${peer.relation}`} value={`peer-${index}`}>{peer.source} · {peer.relation === "suppressed_by" ? "抑制方" : "被抑制方"}</TabsTrigger>)}</TabsList>{data.peers.map((peer, index) => <TabsContent key={`${peer.key}-${peer.relation}`} value={`peer-${index}`}><div className="comparison-grid"><div><p className="comparison-label">当前源 · {data.source}</p><JsonPanel empty="无数据" value={data.payload.data} /></div><div><p className="comparison-label">对端 · {peer.source}</p><JsonPanel empty="对端原始数据不可用" value={peer.payload} /></div></div></TabsContent>)}</Tabs></> : <p className="mt-4 text-sm text-muted-foreground">当前没有关联来源</p>}
      </section>
    </div>
  )
}
