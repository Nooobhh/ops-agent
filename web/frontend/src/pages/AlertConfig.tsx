import { useCallback, useEffect, useState } from "react"
import { RefreshCw } from "lucide-react"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Separator } from "@/components/ui/separator"
import { Switch } from "@/components/ui/switch"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { SettingsPageLayout } from "@/components/SettingsLayout"
import { SeverityBadge } from "@/components/SeverityBadge"
import { fetchJson, formatTime, postJson, putJson } from "@/lib/api"
import type { AlertConfigAlert, AlertConfigResponse, AlertConfigSourceId, AlertSourceNotify } from "@/types"


const overrideField = ["severity", "override"].join("_")

type RawResponse = {
  catalog_available?: boolean
  sources?: Record<string, { updated_at?: unknown; alerts?: Array<Record<string, unknown>> }>
  source_notify?: Record<string, Partial<AlertSourceNotify>>
  sources_meta?: Array<{ id?: unknown; label?: unknown }>
}

function optionalString(value: unknown) {
  return typeof value === "string" ? value : undefined
}

function normalizeAlert(raw: Record<string, unknown>): AlertConfigAlert {
  const override = raw[overrideField]
  return {
    alertname: String(raw.alertname || ""),
    severity: optionalString(raw.severity) || null,
    severityOverride: override === "critical" || override === "warning" || override === "info"
      ? override : null,
    class: String(raw.class || "watch"),
    notify: raw.notify !== false,
    platform: optionalString(raw.platform),
    group: optionalString(raw.group),
    state: optionalString(raw.state),
    label: optionalString(raw.label),
    enabled: typeof raw.enabled === "boolean" ? raw.enabled : undefined,
    service: optionalString(raw.service),
  }
}

function normalizeResponse(raw: RawResponse): AlertConfigResponse {
  const normalized: AlertConfigResponse = {
    catalog_available: raw.catalog_available === true,
    sources: {},
    source_notify: {},
    sources_meta: (raw.sources_meta || [])
      .filter((item) => typeof item.id === "string" && item.id)
      .map((item) => ({ id: String(item.id), label: typeof item.label === "string" && item.label ? item.label : String(item.id) })),
  }
  for (const { id } of normalized.sources_meta) {
    const notify = raw.source_notify?.[id]
    if (notify) {
      normalized.source_notify[id] = {
        notify: notify.notify === true,
        default: notify.default === true,
        overridden: notify.overridden === true,
      }
    }
    const source = raw.sources?.[id]
    if (!source) continue
    normalized.sources[id] = {
      updated_at: typeof source.updated_at === "string" ? source.updated_at : null,
      alerts: (source.alerts || []).map(normalizeAlert),
    }
  }
  return normalized
}

export default function AlertConfig() {
  const [selectedTab, setSelectedTab] = useState<AlertConfigSourceId>("")
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [query, setQuery] = useState("")
  const [sevFilter, setSevFilter] = useState<"all" | "critical" | "warning" | "info">("all")
  const [notifyFilter, setNotifyFilter] = useState<"all" | "on" | "off">("all")
  const [data, setData] = useState<AlertConfigResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState("")
  const [bulkBusy, setBulkBusy] = useState(false)
  const [error, setError] = useState("")

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setLoading(true)
      setError("")
      const raw = await fetchJson<RawResponse>("/api/alert-config", signal)
      const normalized = normalizeResponse(raw)
      setData(normalized)
      // 来源随告警源配置:当前 tab 不在配置里时落到第一个来源
      setSelectedTab((current) => normalized.sources_meta.some((item) => item.id === current)
        ? current : normalized.sources_meta[0]?.id || "")
    } catch (reason) {
      if ((reason as Error).name !== "AbortError") setError((reason as Error).message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => controller.abort()
  }, [load])

  useEffect(() => {
    setSelected(new Set())
  }, [selectedTab])

  async function update(alertname: string, body: Record<string, unknown>) {
    const rowKey = `${selectedTab}:${alertname}`
    try {
      setBusy(rowKey)
      setError("")
      await postJson<Record<string, unknown>>(
        `/api/alert-config/${encodeURIComponent(selectedTab)}/${encodeURIComponent(alertname)}`,
        body,
      )
      await load()
    } catch (reason) {
      setError((reason as Error).message)
    } finally {
      setBusy("")
    }
  }

  async function updateSourceNotify(notify: boolean | null) {
    try {
      setBusy(`source:${selectedTab}`)
      setError("")
      await putJson<Record<string, unknown>>(`/api/alert-sources/${encodeURIComponent(selectedTab)}/notify`, { notify })
      await load()
    } catch (reason) {
      setError((reason as Error).message)
    } finally {
      setBusy("")
    }
  }

  async function bulkUpdate(body: Record<string, unknown>) {
    setBulkBusy(true)
    setError("")
    try {
      const failures = (await Promise.all([...selected].map(async (alertname) => {
        try {
          await postJson<Record<string, unknown>>(
            `/api/alert-config/${encodeURIComponent(selectedTab)}/${encodeURIComponent(alertname)}`,
            body,
          )
          return null
        } catch (reason) {
          return `${alertname}: ${(reason as Error).message}`
        }
      }))).filter((failure): failure is string => failure !== null)
      await load()
      if (failures.length > 0) {
        setError(`批量更新部分失败：${failures.join("；")}`)
      } else {
        setSelected(new Set())
      }
    } finally {
      setBulkBusy(false)
    }
  }

  async function bulkUpdateSeverity(value: string) {
    await bulkUpdate({ severity: value === "source" ? null : value })
  }

  async function bulkUpdateNotify(value: string) {
    await bulkUpdate({ notify: value === "on" })
  }

  const current = data?.sources[selectedTab]
  const sourceNotify = data?.source_notify[selectedTab]
  const columnCount = 6
  const normalizedQuery = query.trim().toLowerCase()
  const filteredAlerts = (current?.alerts || []).filter((alert) => {
    const searchable = `${alert.alertname} ${alert.service || ""}`.toLowerCase()
    if (normalizedQuery && !searchable.includes(normalizedQuery)) return false
    if (sevFilter !== "all" && (alert.severityOverride || alert.severity) !== sevFilter) {
      return false
    }
    if (notifyFilter === "on" && !alert.notify) return false
    if (notifyFilter === "off" && alert.notify) return false
    return true
  })

  return (
    <SettingsPageLayout group="alerts">
      <div className="settings-notice-row" role="status">
        <AlertDescription>配置改动将在下一轮询生效（≤5 分钟）。目录更新于 {formatTime(current?.updated_at)}</AlertDescription>
        <Button onClick={() => void load()} size="sm" variant="outline"><RefreshCw data-icon="inline-start" />刷新</Button>
      </div>
      {error ? <Alert className="mb-4 rounded-md" variant="destructive"><AlertDescription>{error}</AlertDescription></Alert> : null}
      <Tabs onValueChange={(value) => setSelectedTab(value as AlertConfigSourceId)} value={selectedTab}>
        <TabsList className="mb-4 h-auto flex-wrap justify-start">
          {(data?.sources_meta || []).map((source) => <TabsTrigger key={source.id} value={source.id}>{source.label}</TabsTrigger>)}
        </TabsList>
      </Tabs>
      {sourceNotify ? (
        <div className="mb-4 flex flex-wrap items-center gap-3 rounded-md border p-3">
          <Switch aria-label="该来源中台推送开关" checked={sourceNotify.notify} disabled={busy === `source:${selectedTab}`} onCheckedChange={(checked) => void updateSourceNotify(checked)} />
          <div className="min-w-0 flex-1 text-sm">
            <strong>中台推送{sourceNotify.notify ? "已开启" : "已关闭"}</strong>
            <p className="text-xs text-muted-foreground">关闭后该来源只接收不推送:时间线、分级与自动排查照常,下表逐条推送开关不生效。默认{sourceNotify.default ? "开启" : "关闭"}{sourceNotify.overridden ? ",已在本机改过" : ""}。</p>
          </div>
          {sourceNotify.overridden ? <Button disabled={busy === `source:${selectedTab}`} onClick={() => void updateSourceNotify(null)} size="sm" variant="ghost">恢复默认</Button> : null}
        </div>
      ) : null}
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <Input
          className="min-w-[240px] flex-1"
          onChange={(event) => setQuery(event.target.value)}
          placeholder="搜索告警名 / service"
          value={query}
        />
        <ToggleGroup
          aria-label="按告警等级筛选"
          onValueChange={(value) => {
            if (value) setSevFilter(value as typeof sevFilter)
          }}
          type="single"
          value={sevFilter}
          variant="outline"
        >
          <ToggleGroupItem value="all">全部等级</ToggleGroupItem>
          <ToggleGroupItem value="critical">critical</ToggleGroupItem>
          <ToggleGroupItem value="warning">warning</ToggleGroupItem>
          <ToggleGroupItem value="info">info</ToggleGroupItem>
        </ToggleGroup>
        <ToggleGroup
          aria-label="按推送状态筛选"
          onValueChange={(value) => {
            if (value) setNotifyFilter(value as typeof notifyFilter)
          }}
          type="single"
          value={notifyFilter}
          variant="outline"
        >
          <ToggleGroupItem value="all">全部推送</ToggleGroupItem>
          <ToggleGroupItem value="on">未静音</ToggleGroupItem>
          <ToggleGroupItem value="off">已静音</ToggleGroupItem>
        </ToggleGroup>
      </div>
      {selected.size > 0 ? (
        <div className="sticky top-14 z-10 mb-4 flex items-center gap-3 rounded-md border bg-background p-3 shadow-sm">
          <span className="text-sm">选中 {selected.size} 条</span>
          <Separator className="h-6" orientation="vertical" />
          <Select disabled={bulkBusy} onValueChange={(value) => void bulkUpdateSeverity(value)}>
            <SelectTrigger className="w-[140px]"><SelectValue placeholder="改覆盖等级" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="source">跟随来源</SelectItem>
              <SelectItem value="critical">critical</SelectItem>
              <SelectItem value="warning">warning</SelectItem>
              <SelectItem value="info">info</SelectItem>
            </SelectContent>
          </Select>
          <Select disabled={bulkBusy} onValueChange={(value) => void bulkUpdateNotify(value)}>
            <SelectTrigger className="w-[120px]"><SelectValue placeholder="改推送" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="on">开启</SelectItem>
              <SelectItem value="off">关闭</SelectItem>
            </SelectContent>
          </Select>
          <Button disabled={bulkBusy} onClick={() => setSelected(new Set())} size="sm" variant="ghost">清空</Button>
        </div>
      ) : null}
      {!loading && data && !data.catalog_available ? (
        <div className="flex min-h-48 items-center justify-center border-y text-sm text-muted-foreground">目录待下一轮询生成</div>
      ) : (
        <div className="border-y">
          <Table>
            <TableHeader><TableRow>
              <TableHead className="w-8"><Checkbox aria-label="全选当前筛选结果" checked={selected.size === filteredAlerts.length && filteredAlerts.length > 0 ? true : selected.size > 0 && selected.size < filteredAlerts.length ? "indeterminate" : false} disabled={filteredAlerts.length === 0 || bulkBusy} onCheckedChange={(checked) => setSelected(checked ? new Set(filteredAlerts.map((alert) => alert.alertname)) : new Set())} /></TableHead>
              <TableHead>告警名</TableHead><TableHead>源端等级</TableHead><TableHead>覆盖等级</TableHead><TableHead>基线分级</TableHead><TableHead>推送</TableHead>
            </TableRow></TableHeader>
            <TableBody>
              {filteredAlerts.map((alert) => {
                const rowBusy = busy === `${selectedTab}:${alert.alertname}`
                return <TableRow key={alert.alertname}>
                  <TableCell className="w-8"><Checkbox aria-label={`选择 ${alert.alertname}`} checked={selected.has(alert.alertname)} disabled={bulkBusy} onCheckedChange={(checked) => setSelected((currentSelected) => {
                    const next = new Set(currentSelected)
                    if (checked) next.add(alert.alertname)
                    else next.delete(alert.alertname)
                    return next
                  })} /></TableCell>
                  <TableCell className="min-w-[220px]"><strong>{alert.alertname}</strong>{alert.label ? <p className="text-xs text-muted-foreground">{alert.label}</p> : null}{alert.enabled === false ? <Badge className="ml-2 status-badge" variant="outline">未启用</Badge> : null}</TableCell>
                  <TableCell><SeverityBadge severity={alert.severity} /></TableCell>
                  <TableCell>
                    <Select disabled={rowBusy} onValueChange={(value) => void update(alert.alertname, { severity: value === "source" ? null : value })} value={alert.severityOverride || "source"}>
                      <SelectTrigger className="w-[130px]"><SelectValue /></SelectTrigger>
                      <SelectContent><SelectGroup><SelectItem value="source">跟随来源</SelectItem><SelectItem value="critical">critical</SelectItem><SelectItem value="warning">warning</SelectItem><SelectItem value="info">info</SelectItem></SelectGroup></SelectContent>
                    </Select>
                  </TableCell>
                  <TableCell><Badge className="status-badge class-badge" data-status={alert.class} variant="outline">{alert.class}</Badge></TableCell>
                  <TableCell><Switch aria-label={`${alert.alertname} 推送开关`} checked={alert.notify} disabled={rowBusy} onCheckedChange={(checked) => void update(alert.alertname, { notify: checked })} /></TableCell>
                </TableRow>
              })}
              {loading ? <TableRow><TableCell className="h-28 text-center text-muted-foreground" colSpan={columnCount}>正在读取告警目录</TableCell></TableRow> : null}
              {!loading && data?.catalog_available && (current?.alerts.length || 0) === 0 ? <TableRow><TableCell className="h-28 text-center text-muted-foreground" colSpan={columnCount}>该来源暂无告警定义</TableCell></TableRow> : null}
              {!loading && data?.catalog_available && (current?.alerts.length || 0) > 0 && filteredAlerts.length === 0 ? <TableRow><TableCell className="h-28 text-center text-muted-foreground" colSpan={columnCount}>无匹配</TableCell></TableRow> : null}
            </TableBody>
          </Table>
        </div>
      )}
    </SettingsPageLayout>
  )
}
