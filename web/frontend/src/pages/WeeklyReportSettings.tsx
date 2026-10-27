import { useEffect, useState } from "react"
import { Save } from "lucide-react"
import { SettingsPageLayout } from "@/components/SettingsLayout"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { loadWeeklyReportSettings, updateWeeklyReportSettings } from "@/lib/worklogApi"

const WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

export default function WeeklyReportSettings() {
  const [startWeekday, setStartWeekday] = useState(3)
  const [endWeekday, setEndWeekday] = useState(2)
  const [savedStart, setSavedStart] = useState(3)
  const [savedEnd, setSavedEnd] = useState(2)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState("")
  const [message, setMessage] = useState("")

  useEffect(() => {
    const controller = new AbortController()
    loadWeeklyReportSettings(controller.signal)
      .then((settings) => {
        setStartWeekday(settings.start_weekday)
        setEndWeekday(settings.end_weekday)
        setSavedStart(settings.start_weekday)
        setSavedEnd(settings.end_weekday)
      })
      .catch((reason) => {
        if ((reason as Error).name !== "AbortError") setError((reason as Error).message)
      })
      .finally(() => setLoading(false))
    return () => controller.abort()
  }, [])

  async function save() {
    setSaving(true)
    setError("")
    setMessage("")
    try {
      const settings = await updateWeeklyReportSettings(startWeekday, endWeekday)
      setSavedStart(settings.start_weekday)
      setSavedEnd(settings.end_weekday)
      setMessage(`周报周期已保存：${settings.start_label}至${settings.end_label}`)
    } catch (reason) {
      setError((reason as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const dirty = startWeekday !== savedStart || endWeekday !== savedEnd

  return (
    <SettingsPageLayout
      group="worklog"
    >
      {error ? <Alert className="mb-5" variant="destructive"><AlertDescription>{error}</AlertDescription></Alert> : null}
      <Card className="settings-card max-w-2xl">
        <CardHeader>
          <CardTitle>周报周期</CardTitle>
          <CardDescription>按北京时间计算；起止日期均计入周期，工作日志页将按此周期前后翻页。</CardDescription>
        </CardHeader>
        <CardContent className="settings-card-content">
          <div className="settings-setting-row weekly-setting-row">
            <div className="min-w-0">
              <p className="text-sm font-medium">周报周期</p>
              <p className="mt-1 text-xs text-muted-foreground">按北京时间计算，起止日期均计入周期。</p>
            </div>
            <div className="settings-setting-control flex items-center gap-3">
              <div className="min-w-0 flex-1">
                <label className="sr-only" htmlFor="weekly-start">开始日</label>
                <Select disabled={loading || saving} onValueChange={(value) => setStartWeekday(Number(value))} value={String(startWeekday)}>
                  <SelectTrigger id="weekly-start"><SelectValue /></SelectTrigger>
                  <SelectContent>{WEEKDAYS.map((day, index) => <SelectItem key={day} value={String(index)}>{day}</SelectItem>)}</SelectContent>
                </Select>
              </div>
              <span className="shrink-0 text-sm text-muted-foreground">至</span>
              <div className="min-w-0 flex-1">
                <label className="sr-only" htmlFor="weekly-end">结束日</label>
                <Select disabled={loading || saving} onValueChange={(value) => setEndWeekday(Number(value))} value={String(endWeekday)}>
                  <SelectTrigger id="weekly-end"><SelectValue /></SelectTrigger>
                  <SelectContent>{WEEKDAYS.map((day, index) => <SelectItem key={day} value={String(index)}>{day}</SelectItem>)}</SelectContent>
                </Select>
              </div>
            </div>
          </div>
          <div className="settings-action-row">
            <Button disabled={loading || saving || !dirty} onClick={() => void save()}><Save data-icon="inline-start" />{saving ? "保存中" : "保存配置"}</Button>
            {dirty ? <span className="text-xs font-medium text-[#b7791f]">未保存</span> : null}
            {message ? <span className="text-sm text-[#2f9e6e]">{message}</span> : null}
          </div>
        </CardContent>
      </Card>
    </SettingsPageLayout>
  )
}
