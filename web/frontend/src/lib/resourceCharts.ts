import type { ResourceChartTarget, ResourceHistoryResponse } from "../types.ts"

export type ResourceChartSpec = {
  key: string
  title: string
  unit: string
  series: Array<{ key: string; label: string; color: string }>
  data: Array<Record<string, number | string | null>>
}

type MutableChart = {
  key: string
  title: string
  unit: string
  series: Map<string, Map<number, number>>
}

const COLORS = ["#65b96e", "#f2cc0c", "#7aa2f7", "#f97316", "#a78bfa", "#22c1c3", "#ef4444"]

const LABELS: Record<string, string> = {
  cpu_used_pct: "CPU 使用率",
  cpu_cores: "CPU 核数",
  load1: "Load 1 分钟",
  load5: "Load 5 分钟",
  load15: "Load 15 分钟",
  memory_total_bytes: "内存总量",
  memory_available_bytes: "可用内存",
  memory_used_pct: "内存使用率",
  swap_total_bytes: "Swap 总量",
  swap_free_bytes: "Swap 空闲量",
  swap_used_pct: "Swap 使用率",
  swap_used_bytes: "Swap 使用量",
  major_page_faults_total: "Major Page Faults",
  heap_used_bytes: "Heap 使用量",
  heap_committed_bytes: "Heap Committed",
  heap_max_bytes: "Heap Max",
  heap_used_pct: "Heap 使用率",
  process_resident_bytes: "进程 RSS",
  used_pct: "磁盘使用率",
  inode_used_pct: "Inode 使用率",
  available_bytes: "磁盘可用空间",
  size_bytes: "磁盘总空间",
  files: "Inode 总数",
  files_free: "Inode 空闲数",
  gc_count_total: "GC 次数",
  gc_time_seconds_total: "GC 累计耗时",
  gc_time_pct: "GC 耗时占比",
}

export function buildResourceCharts(
  response: ResourceHistoryResponse | null,
  target: ResourceChartTarget,
): ResourceChartSpec[] {
  if (!response) return []
  const charts = new Map<string, MutableChart>()
  for (const point of response.points) {
    const timestamp = Date.parse(point.collected_at)
    if (!Number.isFinite(timestamp)) continue
    if (target.kind === "node") {
      const node = point.nodes.find((item) => item.node === target.node)
      if (!node) continue
      for (const [metric, value] of Object.entries(node.metrics || {})) {
        if (["load1", "load5", "load15"].includes(metric)) {
          add(
            charts, "node:load", "Load 1/5/15 分钟", "number",
            label(metric), timestamp, value,
          )
        } else {
          add(charts, `node:${metric}`, label(metric), unit(metric), target.node, timestamp, value)
        }
      }
      for (const filesystem of node.filesystems || []) {
        const series = filesystem.mountpoint || filesystem.device || "filesystem"
        for (const metric of ["used_pct", "inode_used_pct", "available_bytes", "size_bytes", "files", "files_free"] as const) {
          add(charts, `filesystem:${metric}`, label(metric), unit(metric), series, timestamp, filesystem[metric])
        }
      }
    } else if (target.kind === "service") {
      const service = point.services.find((item) => (
        item.node === target.node && item.job === target.job
        && (item.role || null) === (target.role || null)
      ))
      if (!service) continue
      for (const [metric, value] of Object.entries(service.metrics || {})) {
        add(charts, `service:${metric}`, label(metric), unit(metric), target.job || target.title, timestamp, value)
      }
      for (const [collector, values] of Object.entries(service.gc || {})) {
        for (const [metric, value] of Object.entries(values)) {
          add(charts, `gc:${metric}`, label(metric), unit(metric), collector, timestamp, value)
        }
      }
      for (const [pool, value] of Object.entries(service.old_gen_after_gc_bytes || {})) {
        add(charts, "after-gc:old-gen", "GC 后 Old Gen", "bytes", pool, timestamp, value)
      }
    } else {
      for (const metric of point.business_metrics || []) {
        if (
          metric.node !== target.node || metric.job !== target.job
          || (target.service && metric.service !== target.service)
        ) continue
        add(
          charts, `business:${metric.metric}`, metric.label || label(metric.metric),
          metric.unit, metric.label || metric.metric, timestamp, metric.value,
        )
      }
    }
  }
  return [...charts.values()].map(finalize).filter((chart) => chart.data.length > 0)
}

function add(
  charts: Map<string, MutableChart>, key: string, title: string, metricUnit: string,
  series: string, timestamp: number, raw: number | null | undefined,
) {
  const value = Number(raw)
  if (raw == null || !Number.isFinite(value)) return
  const chart = charts.get(key) || {
    key, title, unit: metricUnit, series: new Map<string, Map<number, number>>(),
  }
  const points = chart.series.get(series) || new Map<number, number>()
  points.set(timestamp, value)
  chart.series.set(series, points)
  charts.set(key, chart)
}

function finalize(chart: MutableChart): ResourceChartSpec {
  const labels = [...chart.series.keys()]
  const timestamps = [...new Set(
    [...chart.series.values()].flatMap((points) => [...points.keys()]),
  )].sort((left, right) => left - right)
  const series = labels.map((seriesLabel, index) => ({
    key: `series_${index}`, label: seriesLabel, color: COLORS[index % COLORS.length],
  }))
  const data = timestamps.map((timestamp) => {
    const row: Record<string, number | string | null> = {
      timestamp,
      time: new Date(timestamp).toLocaleString("zh-CN", {
        month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
      }),
    }
    series.forEach((item, index) => {
      row[item.key] = chart.series.get(labels[index])?.get(timestamp) ?? null
    })
    return row
  })
  return { key: chart.key, title: chart.title, unit: chart.unit, series, data }
}

function label(metric: string) {
  return LABELS[metric] || metric.replaceAll("_", " ")
}

function unit(metric: string) {
  if (metric.endsWith("_pct")) return "percent"
  if (metric.endsWith("_bytes")) return "bytes"
  if (metric.includes("seconds")) return "seconds"
  if (metric.endsWith("_total") || metric === "files" || metric === "files_free") return "count"
  return "number"
}
