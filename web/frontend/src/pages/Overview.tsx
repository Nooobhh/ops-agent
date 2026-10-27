import { useCallback, useEffect, useState } from "react"
import { BellRing, Clock3, EyeOff, RefreshCw, TimerReset, TriangleAlert } from "lucide-react"
import { Link, useOutletContext } from "react-router-dom"
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { AlertMeta } from "@/components/AlertMeta"
import { ConsistencyAlert } from "@/components/ConsistencyAlert"
import { PageHeader } from "@/components/PageHeader"
import { SeverityBadge } from "@/components/SeverityBadge"
import { StatusBadge } from "@/components/StatusBadge"
import { ResourceSummaryCards } from "@/components/resources/ResourceSummaryCards"
import { fetchJson, formatTime } from "@/lib/api"
import { zonedDate, zonedMidnightIso } from "@/lib/envTime"
import { OVERVIEW_STAT_CARDS, overviewStatTone } from "@/lib/overviewStats"
import type { AlertChange, AppShellOutletContext, OverviewResponse } from "@/types"

type RangeKey = "24h" | "7d" | "30d"

function todayMidnightIso(now = new Date()) {
  return zonedMidnightIso(zonedDate(now))
}

const statIcons = {
  active_non_chronic: BellRing,
  new_today: Clock3,
  suppressed_today: EyeOff,
  chronic: TimerReset,
} as const

const changeLabels: Record<AlertChange["change_type"], string> = {
  new: "新增",
  resolved: "已恢复",
  escalated: "升级",
}

export default function Overview() {
  const { poll, resourceSummary } = useOutletContext<AppShellOutletContext>()
  const [range, setRange] = useState<RangeKey>("24h")
  const [data, setData] = useState<OverviewResponse | null>(null)
  const [error, setError] = useState("")
  const [loading, setLoading] = useState(true)

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setError("")
      setData(await fetchJson<OverviewResponse>(`/api/overview?range=${range}`, signal))
    } catch (reason) {
      if ((reason as Error).name !== "AbortError") setError((reason as Error).message)
    } finally { setLoading(false) }
  }, [range])

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    void load(controller.signal)
    const timer = window.setInterval(() => void load(), 60_000)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [load])

  const chartData = (data?.distribution || []).map((item) => ({
    bucket: formatTime(item.bucket),
    critical: item.by_severity.critical || 0,
    warning: item.by_severity.warning || 0,
    other: item.count - (item.by_severity.critical || 0) - (item.by_severity.warning || 0),
  }))
  // Older long-running web processes may not have the credential warning
  // field yet. A missing optional section must not take down the whole SPA.
  const credentialWarnings = data?.credential_warnings
  return (
    <div>
      <PageHeader title="运维概览" meta={data ? `告警数据截止 ${formatTime(poll?.last_tick_at)} · 页面更新 ${formatTime(data.generated_at)}` : "读取运维状态"} actions={
        <>
          <Button aria-label="刷新概览" onClick={() => void load()} size="icon" title="刷新" variant="outline"><RefreshCw /></Button>
        </>
      } />
      {credentialWarnings?.available && credentialWarnings.count > 0 ? <div className="mb-5 rounded-md border-2 border-red-500 bg-red-50 px-4 py-3 text-red-900 dark:bg-red-950/30 dark:text-red-100">
        <div className="flex items-start gap-3">
          <TriangleAlert className="mt-0.5 size-5 shrink-0 text-red-600 dark:text-red-400" />
          <div className="min-w-0">
            <p className="font-semibold">节点 SSH 凭证时效预警</p>
            <p className="mt-1 text-sm">{credentialWarnings.expired_count > 0 ? `${credentialWarnings.expired_count} 个节点已过期，` : ""}{credentialWarnings.count - credentialWarnings.expired_count} 个节点将在 {credentialWarnings.threshold_days} 天内过期，请到设置页手动生成长效凭证。</p>
            <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs">
              {credentialWarnings.items.slice(0, 8).map((item) => <span key={item.host}>{item.target || item.host}：{item.state === "expired" ? "已过期" : `${Math.max(0, item.remaining_days)} 天内过期`}</span>)}
              {credentialWarnings.items.length > 8 ? <Link className="font-medium underline" to="/settings/credentials">查看全部</Link> : null}
            </div>
          </div>
        </div>
      </div> : null}
      {error ? <p className="mb-4 text-sm text-destructive">{error}</p> : null}
      <section className="mb-7">
        <div className="mb-3"><h2 className="text-base font-semibold">资源概览</h2><p className="text-xs text-muted-foreground">节点覆盖、服务内存采集与本地挂载点</p></div>
        <ResourceSummaryCards links summary={resourceSummary} />
      </section>
      <div className="mb-3"><h2 className="text-base font-semibold">告警概览</h2><p className="text-xs text-muted-foreground">生产告警生命周期、变化与通知状态</p></div>
      <ConsistencyAlert consistency={data?.consistency} />
      <div className="mt-5 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {OVERVIEW_STAT_CARDS.map(({ key, label, filterUrl }) => {
          const Icon = statIcons[key]
          const value = data?.stats[key] ?? 0
          const cardUrl = filterUrl === "today"
            ? `/alerts?first_seen_from=${todayMidnightIso()}`
            : filterUrl
          return (
          <Card className="group relative rounded-md shadow-sm" key={key}>
            <Link
              aria-label={`跳转到${label}的告警列表`}
              className="absolute inset-0 z-10 rounded-md focus:outline-none focus:ring-2 focus:ring-ring"
              to={cardUrl}
            />
            <CardHeader className="flex-row items-center justify-between gap-3 p-4 pb-2">
              <CardDescription className="group-hover:text-foreground">{label}</CardDescription><Icon className="size-4 text-muted-foreground" />
            </CardHeader>
            <CardContent className="p-4 pt-0">
              {loading && !data ? <Skeleton className="h-9 w-16" /> : (
                <div className="flex items-baseline gap-2">
                  <strong className={`text-3xl font-semibold tabular-nums ${overviewStatTone(key, value)}`} title={key === "active_non_chronic" || key === "chronic" ? `共 ${data?.stats.active ?? 0} 条活跃，其中常驻 ${data?.stats.chronic ?? 0} 条` : undefined}>{value}</strong>
                  {key === "new_today" ? <span className="text-xs text-muted-foreground">恢复 {data?.stats.resolved_today ?? 0}</span> : null}
                  {key === "active_non_chronic" ? <span className="text-xs text-muted-foreground">共 {data?.stats.active ?? 0}</span> : null}
                </div>
              )}
            </CardContent>
          </Card>
          )
        })}
      </div>
      <section className="mt-6 border-y py-5">
        <div className="mb-4 flex items-baseline justify-between gap-4">
          <div><h2 className="text-base font-semibold">时间分布</h2><p className="text-xs text-muted-foreground">按首次出现时间统计</p></div>
          <div className="flex flex-wrap items-center justify-end gap-3">
            <ToggleGroup aria-label="时间范围" onValueChange={(value) => value && setRange(value as RangeKey)} size="sm" type="single" value={range} variant="outline">
              <ToggleGroupItem value="24h">24 小时</ToggleGroupItem>
              <ToggleGroupItem value="7d">7 天</ToggleGroupItem>
              <ToggleGroupItem value="30d">30 天</ToggleGroupItem>
            </ToggleGroup>
            <div className="flex items-center gap-2 text-xs text-muted-foreground"><span className="legend critical" />严重<span className="legend warning" />警告<span className="legend other" />其他</div>
          </div>
        </div>
        <div className="h-[300px] w-full">
          <ResponsiveContainer height="100%" width="100%">
            <BarChart data={chartData} margin={{ top: 8, right: 8, bottom: 0, left: -18 }}>
              <CartesianGrid stroke="var(--line)" strokeDasharray="3 3" vertical={false} />
              <XAxis axisLine={false} dataKey="bucket" fontSize={11} interval="preserveStartEnd" tickLine={false} />
              <YAxis allowDecimals={false} axisLine={false} fontSize={11} tickLine={false} />
              <Tooltip contentStyle={{ background: "var(--bg)", border: "1px solid var(--line)", borderRadius: 6 }} />
              <Bar dataKey="critical" fill="#b5332e" stackId="alerts" />
              <Bar dataKey="warning" fill="#d65a2f" stackId="alerts" />
              <Bar dataKey="other" fill="#e49a2f" radius={[3, 3, 0, 0]} stackId="alerts" />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </section>
      <section className="mt-6">
        <div className="mb-3 flex items-start justify-between gap-4"><div><h2 className="text-base font-semibold">最近变化</h2><p className="text-xs text-muted-foreground">时间窗口跟随顶部范围切换 · {data?.changes.total ?? 0} 条告警</p></div><Button asChild size="sm" variant="ghost"><Link to="/alerts">查看全部</Link></Button></div>
        <Table>
          <TableHeader><TableRow><TableHead>告警</TableHead><TableHead>节点</TableHead><TableHead>级别</TableHead><TableHead>出现次数</TableHead><TableHead>状态</TableHead><TableHead className="text-right">时间</TableHead></TableRow></TableHeader>
          <TableBody>
            {(data?.changes.items || []).map((item) => <TableRow key={item.key}>
              <TableCell><div className="flex flex-wrap items-center gap-2"><Link className="font-medium text-[var(--link)] hover:underline" to={`/alerts/${encodeURIComponent(item.key)}`}>{item.alertname}</Link><AlertMeta item={item} /></div><div className="text-xs text-muted-foreground">{item.source} · {item.platform}</div></TableCell>
              <TableCell>{item.host || "-"}</TableCell><TableCell><SeverityBadge severity={item.severity} /></TableCell><TableCell><span className="tabular-nums" title={`最近一次变化：${changeLabels[item.change_type]}`}>{item.occurrence_count > 0 ? `${item.occurrence_count} 次` : "—"}</span></TableCell><TableCell><StatusBadge value={item.status} /></TableCell><TableCell className="text-right text-xs text-muted-foreground">{formatTime(item.changed_at)}</TableCell>
            </TableRow>)}
            {!loading && data?.changes.items.length === 0 ? <TableRow><TableCell className="h-24 text-center text-muted-foreground" colSpan={6}>该时间范围内没有告警变化</TableCell></TableRow> : null}
          </TableBody>
        </Table>
      </section>
    </div>
  )
}
