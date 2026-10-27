import { useEffect, useMemo, useState } from "react"
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { loadResourceHistory } from "@/lib/acquisitionApi"
import { buildResourceCharts } from "@/lib/resourceCharts"
import type { ResourceChartTarget, ResourceHistoryResponse } from "@/types"

const RANGES = [
  { hours: 24, label: "24 小时" },
  { hours: 24 * 7, label: "7 天" },
  { hours: 24 * 30, label: "30 天" },
]

export default function ResourceMetricDialog({ target, onClose }: {
  target: ResourceChartTarget
  onClose: () => void
}) {
  const [hours, setHours] = useState(24)
  const [history, setHistory] = useState<ResourceHistoryResponse | null>(null)
  const [error, setError] = useState("")
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError("")
    loadResourceHistory(target.platform, target.node, hours, controller.signal)
      .then(setHistory)
      .catch((reason) => {
        if ((reason as Error).name !== "AbortError") setError((reason as Error).message)
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [hours, target.node, target.platform])

  const charts = useMemo(
    () => buildResourceCharts(history, target),
    [history, target],
  )

  return <Dialog onOpenChange={(open) => { if (!open) onClose() }} open>
    <DialogContent className="flex max-h-[92vh] max-w-[min(1400px,calc(100vw-2rem))] flex-col overflow-hidden p-0">
      <DialogHeader className="border-b px-6 py-5">
        <div className="flex flex-wrap items-start justify-between gap-3 pr-8">
          <div><DialogTitle>{target.title} · 历史资源</DialogTitle><DialogDescription className="mt-1">{target.platform.toUpperCase()} / {target.node} · 每张图对应快照中的一个数值指标</DialogDescription></div>
          <div className="flex gap-1">{RANGES.map((range) => <Button key={range.hours} onClick={() => setHours(range.hours)} size="sm" variant={hours === range.hours ? "default" : "outline"}>{range.label}</Button>)}</div>
        </div>
      </DialogHeader>
      <div className="overflow-y-auto p-5">
        {loading ? <p className="py-16 text-center text-sm text-muted-foreground">正在加载历史快照…</p> : null}
        {error ? <p className="py-16 text-center text-sm text-destructive">{error}</p> : null}
        {!loading && !error && charts.length === 0 ? <p className="py-16 text-center text-sm text-muted-foreground">该时间范围内没有可绘制的数值指标。</p> : null}
        {!loading && !error && charts.length ? <div className="grid gap-4 lg:grid-cols-2 2xl:grid-cols-3">{charts.map((chart) => <section className="rounded-md border bg-card p-4" key={chart.key}>
          <h3 className="mb-3 font-semibold">{chart.title}</h3>
          <div className="h-[300px] w-full">
            <ResponsiveContainer height="100%" width="100%">
              <LineChart data={chart.data} margin={{ bottom: 8, left: 8, right: 16, top: 8 }}>
                <CartesianGrid strokeDasharray="3 3" vertical={false} />
                <XAxis dataKey="time" minTickGap={28} tick={{ fontSize: 11 }} />
                <YAxis domain={chart.unit === "percent" ? [0, 100] : ["auto", "auto"]} tick={{ fontSize: 11 }} tickFormatter={(value) => shortValue(Number(value), chart.unit)} width={72} />
                <Tooltip formatter={(value) => formatValue(Number(value), chart.unit)} />
                <Legend />
                {chart.series.map((series) => <Line connectNulls={false} dataKey={series.key} dot={false} isAnimationActive={false} key={series.key} name={series.label} stroke={series.color} strokeWidth={2} type="monotone" />)}
              </LineChart>
            </ResponsiveContainer>
          </div>
        </section>)}</div> : null}
      </div>
    </DialogContent>
  </Dialog>
}

function formatValue(value: number, unit: string) {
  if (!Number.isFinite(value)) return "—"
  if (unit === "percent") return `${value.toFixed(2)}%`
  if (unit === "bytes") return formatBytes(value)
  if (unit === "seconds") return `${value.toFixed(2)} s`
  if (unit === "per_second") return `${value.toFixed(2)} /s`
  return value.toLocaleString("zh-CN", { maximumFractionDigits: 2 })
}

function shortValue(value: number, unit: string) {
  if (unit === "percent") return `${value.toFixed(0)}%`
  if (unit === "bytes") return formatBytes(value)
  if (Math.abs(value) >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`
  if (Math.abs(value) >= 1_000) return `${(value / 1_000).toFixed(1)}K`
  return Number.isInteger(value) ? String(value) : value.toFixed(1)
}

function formatBytes(value: number) {
  if (Math.abs(value) >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(1)} GiB`
  if (Math.abs(value) >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(1)} MiB`
  if (Math.abs(value) >= 1024) return `${(value / 1024).toFixed(1)} KiB`
  return `${value.toFixed(0)} B`
}
