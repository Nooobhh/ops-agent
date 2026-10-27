import { useEffect, useState } from "react"
import { Save } from "lucide-react"
import { useLocation } from "react-router-dom"
import { SettingsPageLayout } from "@/components/SettingsLayout"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Switch } from "@/components/ui/switch"
import { loadAcquisition, saveAcquisition } from "@/lib/acquisitionApi"
import type { AcquisitionResponse, AcquisitionSettings } from "@/types"

function bytes(value: number | null | undefined) {
  if (value == null) return "未知"
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(1)} GiB`
  if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(1)} MiB`
  return `${value} B`
}

function SettingRow({
  title,
  description,
  detail,
  children,
}: {
  title: string
  description?: string
  detail?: string
  children: React.ReactNode
}) {
  return <div className="settings-setting-row">
    <div className="min-w-0">
      <p className="text-sm font-medium">{title}</p>
      {description ? <p className="mt-1 text-xs text-muted-foreground">{description}</p> : null}
      {detail ? <p className="mt-1 break-all text-xs text-muted-foreground">{detail}</p> : null}
    </div>
    <div className="settings-setting-control">{children}</div>
  </div>
}

function NumberInput({id, value, onChange, min, max}: {
  id: string; value: number; onChange: (value: number) => void; min: number; max: number
}) {
  return <Input id={id} max={max} min={min} onChange={(event) => onChange(Number(event.target.value))} type="number" value={value} />
}

export default function AcquisitionSettingsPage() {
  const location = useLocation()
  const activeSection = location.pathname.startsWith("/settings/alerts/")
    ? "alerts"
    : location.pathname === "/settings/maintenance"
      ? "maintenance"
      : "resources"
  const pageGroup = activeSection === "alerts" ? "alerts" : activeSection === "maintenance" ? "settings" : "resources"
  const [response, setResponse] = useState<AcquisitionResponse | null>(null)
  const [settings, setSettings] = useState<AcquisitionSettings | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState("")
  const [message, setMessage] = useState("")

  useEffect(() => {
    const controller = new AbortController()
    loadAcquisition(controller.signal).then((value) => { setResponse(value); setSettings(value.settings) }).catch((reason) => {
      if ((reason as Error).name !== "AbortError") setError((reason as Error).message)
    })
    return () => controller.abort()
  }, [])

  function patch<K extends "alerts" | "resources" | "maintenance">(section: K, value: Partial<AcquisitionSettings[K]>) {
    setSettings((current) => current ? { ...current, [section]: { ...current[section], ...value } } : current)
  }

  async function save() {
    if (!settings) return
    setSaving(true); setError(""); setMessage("")
    try {
      const value = await saveAcquisition(settings)
      setResponse(value); setSettings(value.settings); setMessage("采集设置已保存并应用到内嵌调度器")
    } catch (reason) { setError((reason as Error).message) }
    finally { setSaving(false) }
  }

  return <SettingsPageLayout group={pageGroup}>
    {error ? <Alert className="mb-5" variant="destructive"><AlertDescription>{error}</AlertDescription></Alert> : null}
    {!settings ? <p className="text-sm text-muted-foreground">正在读取设置</p> : activeSection === "alerts" ? <div className="space-y-5">
      <Card className="settings-card">
        <CardHeader><CardTitle>告警采集</CardTitle><CardDescription>各告警源的生命周期事件</CardDescription></CardHeader>
        <CardContent className="settings-card-content">
          <SettingRow description="控制告警生命周期采集是否运行" title="启用告警采集"><Switch aria-label="启用告警采集" checked={settings.alerts.enabled} onCheckedChange={(enabled) => patch("alerts", { enabled })} /></SettingRow>
          <SettingRow title="采集周期（秒）"><NumberInput id="alert-interval" max={3600} min={60} onChange={(interval_seconds) => patch("alerts", { interval_seconds })} value={settings.alerts.interval_seconds} /></SettingRow>
          <SettingRow title="保留天数"><NumberInput id="alert-retention" max={3650} min={1} onChange={(retention_days) => patch("alerts", { retention_days })} value={settings.alerts.retention_days} /></SettingRow>
          <SettingRow detail={`${response?.paths.alerts.resolved} · ${response?.paths.alerts.writable ? "可写" : "不可写"} · ${response?.storage.alerts.file_count ?? 0} 个文件 / ${bytes(response?.storage.alerts.total_bytes)}`} title="历史路径"><Input id="alert-history-root" onChange={(event) => patch("alerts", { history_root: event.target.value })} value={settings.alerts.history_root} /></SettingRow>
        </CardContent>
      </Card>
    </div> : activeSection === "resources" ? <div className="space-y-5">
      <Card className="settings-card">
        <CardHeader><CardTitle>资源采集</CardTitle><CardDescription>节点资源、JVM heap/RSS/GC 与本地趋势</CardDescription></CardHeader>
        <CardContent className="settings-card-content">
          <SettingRow description="控制节点资源和服务指标采集是否运行" title="启用资源采集"><Switch aria-label="启用资源采集" checked={settings.resources.enabled} onCheckedChange={(enabled) => patch("resources", { enabled })} /></SettingRow>
          <SettingRow title="采集周期（秒）"><NumberInput id="resource-interval" max={3600} min={60} onChange={(interval_seconds) => patch("resources", { interval_seconds })} value={settings.resources.interval_seconds} /></SettingRow>
          <SettingRow title="保留天数"><NumberInput id="resource-retention" max={3650} min={1} onChange={(retention_days) => patch("resources", { retention_days })} value={settings.resources.retention_days} /></SettingRow>
          <SettingRow detail={`${response?.paths.resources.resolved} · ${response?.paths.resources.writable ? "可写" : "不可写"} · ${response?.storage.resources.file_count ?? 0} 个文件 / ${bytes(response?.storage.resources.total_bytes)}`} title="快照路径"><Input id="resource-snapshot-root" onChange={(event) => patch("resources", { snapshot_root: event.target.value })} value={settings.resources.snapshot_root} /></SettingRow>
          <SettingRow description="当天 JSONL 可追加，次日原子压缩" title="跨日 gzip 封存"><Switch aria-label="启用跨日 gzip 封存" checked={settings.resources.compression === "gzip"} onCheckedChange={(enabled) => patch("resources", { compression: enabled ? "gzip" : "none" })} /></SettingRow>
          <SettingRow description="同一资源异常持续期间只推送一次，恢复后再次异常会重新推送" detail={settings.resources.risk_notifications_enabled ? `当前 ${response?.resource_notifications.active_count ?? 0} 个异常 · 已通知 ${response?.resource_notifications.notified_count ?? 0} 个` : "当前未启用"} title="资源风险通知"><Switch aria-label="启用资源风险通知" checked={settings.resources.risk_notifications_enabled} onCheckedChange={(enabled) => patch("resources", { risk_notifications_enabled: enabled })} /></SettingRow>
        </CardContent>
      </Card>
    </div> : <div className="space-y-5">
      <Card className="settings-card">
        <CardHeader><CardTitle>每日维护</CardTitle><CardDescription>按北京时间封存和清理快照</CardDescription></CardHeader>
        <CardContent className="settings-card-content">
          <SettingRow description="按计划封存和清理历史资源快照" title="启用维护任务"><Switch aria-label="启用每日维护" checked={settings.maintenance.enabled} onCheckedChange={(enabled) => patch("maintenance", { enabled })} /></SettingRow>
          <SettingRow title="小时"><NumberInput id="maintenance-hour" max={23} min={0} onChange={(hour) => patch("maintenance", { hour })} value={settings.maintenance.hour} /></SettingRow>
          <SettingRow title="分钟"><NumberInput id="maintenance-minute" max={59} min={0} onChange={(minute) => patch("maintenance", { minute })} value={settings.maintenance.minute} /></SettingRow>
        </CardContent>
      </Card>
    </div>}
    <div className="settings-page-bottom-actions mt-5">
      <Button disabled={saving} onClick={() => void save()}><Save />{saving ? "保存中" : "保存并应用"}</Button>
    </div>
    {message ? <p aria-live="polite" className="mt-3 text-sm text-[#2f9e6e]">{message}</p> : null}
  </SettingsPageLayout>
}
