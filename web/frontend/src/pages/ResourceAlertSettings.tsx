import { useEffect, useMemo, useState } from "react"
import { RotateCcw, Save } from "lucide-react"
import { SettingsPageLayout } from "@/components/SettingsLayout"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { loadResourceAlertSettings, saveResourceAlertSettings } from "@/lib/acquisitionApi"
import type { ResourceAlertSettingsResponse } from "@/types"

type Draft = Record<string, string>

function pathKey(path: string[]) {
  return path.join(".")
}

function draftFromSettings(data: ResourceAlertSettingsResponse): Draft {
  return Object.fromEntries(data.groups.flatMap((group) => group.fields.map((field) => [
    pathKey(field.path), field.override == null ? "" : String(field.override),
  ])))
}

function setPath(target: Record<string, unknown>, path: string[], value: number) {
  let current = target
  for (const key of path.slice(0, -1)) {
    const next = current[key]
    if (!next || typeof next !== "object" || Array.isArray(next)) current[key] = {}
    current = current[key] as Record<string, unknown>
  }
  current[path[path.length - 1]] = value
}

function displayValue(value: number | null, unit: string) {
  if (value == null) return "未设置"
  return unit === "%" ? `${value}%` : `${value} ${unit}`.trim()
}

export default function ResourceAlertSettings() {
  const [data, setData] = useState<ResourceAlertSettingsResponse | null>(null)
  const [draft, setDraft] = useState<Draft>({})
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState("")
  const [message, setMessage] = useState("")

  useEffect(() => {
    const controller = new AbortController()
    loadResourceAlertSettings(controller.signal).then((value) => {
      setData(value)
      setDraft(draftFromSettings(value))
    }).catch((reason) => {
      if ((reason as Error).name !== "AbortError") setError((reason as Error).message)
    }).finally(() => setLoading(false))
    return () => controller.abort()
  }, [])

  const manualCount = useMemo(
    () => Object.values(draft).filter((value) => value.trim() !== "").length,
    [draft],
  )

  function resetAll() {
    setDraft((current) => Object.fromEntries(Object.keys(current).map((key) => [key, ""])))
    setMessage("")
  }

  async function save() {
    if (!data || saving) return
    const overrides: Record<string, unknown> = {}
    for (const group of data.groups) {
      for (const field of group.fields) {
        const raw = draft[pathKey(field.path)]?.trim() || ""
        if (!raw) continue
        const value = Number(raw)
        if (!Number.isFinite(value)) {
          setError(`${group.label} · ${field.label} 必须是数字`)
          return
        }
        setPath(overrides, field.path, value)
      }
    }
    setSaving(true); setError(""); setMessage("")
    try {
      const value = await saveResourceAlertSettings(overrides)
      setData(value)
      setDraft(draftFromSettings(value))
      setMessage("资源告警阈值已保存；下一轮资源分析生效")
    } catch (reason) {
      setError((reason as Error).message)
    } finally {
      setSaving(false)
    }
  }

  return <SettingsPageLayout
    group="resources"
  >
    {error ? <Alert className="mb-5" variant="destructive"><AlertDescription>{error}</AlertDescription></Alert> : null}
    {loading ? <p className="text-sm text-muted-foreground">正在读取资源告警阈值</p> : data ? <>
      <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
        <p className="text-sm text-muted-foreground">内置配置作为默认值；只填写需要调整的项目，留空即跟随默认。</p>
        <Badge variant="outline">手动覆盖 {manualCount} 项</Badge>
      </div>
      <div className="space-y-5">
        {data.groups.map((group) => <Card className="settings-card" key={group.key}>
          <CardHeader><CardTitle className="text-base">{group.label}</CardTitle><CardDescription>{group.description}</CardDescription></CardHeader>
          <CardContent className="settings-card-content">
            {group.fields.map((field) => {
              const key = pathKey(field.path)
              const value = draft[key] || ""
              return <div className="settings-setting-row resource-alert-row" key={key}>
                <div className="min-w-0"><p className="text-sm font-medium">{field.label}</p><p className="mt-1 text-xs text-muted-foreground">默认 {displayValue(field.default, field.unit)} · 当前生效 {displayValue(field.effective, field.unit)}</p><p className="mt-1 text-xs text-muted-foreground">{field.description}</p></div>
                <div className="settings-setting-control"><div className="relative"><Input aria-label={`${group.label} ${field.label}`} inputMode="decimal" min={0} onChange={(event) => setDraft((current) => ({ ...current, [key]: event.target.value }))} placeholder={field.default == null ? "未设置" : String(field.default)} step="any" type="number" value={value} /><span className="pointer-events-none absolute right-3 top-2 text-xs text-muted-foreground">{field.unit}</span></div></div>
                <Button disabled={!value} onClick={() => setDraft((current) => ({ ...current, [key]: "" }))} size="sm" type="button" variant="ghost">跟随默认</Button>
              </div>
            })}
          </CardContent>
        </Card>)}
      </div>
      <div className="settings-page-bottom-actions mt-5"><Button disabled={saving} onClick={() => void save()}><Save />{saving ? "保存中" : "保存设置"}</Button><Button disabled={saving || manualCount === 0} onClick={resetAll} variant="outline"><RotateCcw />全部恢复默认</Button>{message ? <span className="text-sm text-emerald-600 dark:text-emerald-400">{message}</span> : null}</div>
    </> : null}
  </SettingsPageLayout>
}
