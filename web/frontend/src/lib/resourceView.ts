import type { ResourceRisksResponse } from "../types.ts"

export type MetricKind = "cpu" | "load" | "memory" | "disk" | "heap"
export type SortDirection = "asc" | "desc"
export type ResourceFinding = ResourceRisksResponse["findings"][number]

const NATURAL = new Intl.Collator("zh-CN", { numeric: true, sensitivity: "base" })

const THRESHOLDS: Record<MetricKind, [number, number, number]> = {
  cpu: [50, 70, 85],
  load: [0.5, 0.75, 1],
  memory: [60, 75, 90],
  disk: [60, 70, 80],
  heap: [65, 75, 85],
}

export function metricTone(value: number | null | undefined, kind: MetricKind) {
  if (value == null || !Number.isFinite(value)) return "text-muted-foreground"
  const [caution, warning, critical] = THRESHOLDS[kind]
  if (value >= critical) return "text-red-600 dark:text-red-400"
  if (value >= warning) return "text-orange-600 dark:text-orange-400"
  if (value >= caution) return "text-amber-600 dark:text-amber-400"
  return "text-emerald-600 dark:text-emerald-400"
}

export function compareValues(
  left: string | number | null | undefined,
  right: string | number | null | undefined,
  direction: SortDirection,
) {
  const factor = direction === "asc" ? 1 : -1
  const leftMissing = left == null || (typeof left === "number" && !Number.isFinite(left))
  const rightMissing = right == null || (typeof right === "number" && !Number.isFinite(right))
  if (leftMissing && rightMissing) return 0
  if (leftMissing) return 1
  if (rightMissing) return -1
  if (typeof left === "number" && typeof right === "number") return (left - right) * factor
  return NATURAL.compare(String(left), String(right)) * factor
}

export function nextDirection(currentKey: string, nextKey: string, direction: SortDirection) {
  return currentKey === nextKey && direction === "asc" ? "desc" : "asc"
}

export function riskTitle(finding: ResourceFinding) {
  const scope = `${finding.platform.toUpperCase()} / ${finding.node}`
  const evidence = finding.evidence || {}
  if (finding.resource === "business") {
    return `${scope}-${evidence.service || "业务服务"}-${evidence.label || evidence.metric || "业务指标"}`
  }
  if (finding.resource === "jvm") {
    const service = String(evidence.job || "JVM").replace(/_exporter$/, "")
    if (evidence.metric === "heap_used_pct") return `${scope}-${service}-Heap 使用率`
    const metric = finite(evidence.eta_hours) ? "GC 后 Old Gen 增长预测" : "GC 后 Old Gen 使用率"
    return `${scope}-${service}-${metric}`
  }
  if (finding.resource === "filesystem") {
    return `${scope}-${evidence.mountpoint || "文件系统"}-磁盘容量`
  }
  if (finding.resource === "cpu") return `${scope}-节点-CPU 使用率`
  if (finding.resource === "memory") return `${scope}-节点-可用内存`
  return `${scope}-${finding.resource}-资源风险`
}

export function riskDescription(finding: ResourceFinding) {
  if (finding.description) return finding.description
  const evidence = finding.evidence || {}
  if (finding.resource === "jvm") {
    const job = String(evidence.job || "JVM 服务").replace(/_exporter$/, "")
    if (evidence.metric === "heap_used_pct") {
      const threshold = finding.severity === "critical"
        ? evidence.danger_pct ?? evidence.warning_pct
        : evidence.warning_pct
      const label = finding.severity === "critical" ? "严重" : "告警"
      return `${job} 最近一次 Heap 采样为 ${number(evidence.heap_used_pct, "%")}，最近 3 个采集点中位数为 ${number(evidence.recent_median_pct, "%")}，已达到 ${number(threshold, "%")} 现状${label}阈值。`
    }
    if (finite(evidence.eta_hours)) {
      return `${job} 的 GC 后 Old Gen 小时中位数从 ${number(evidence.start_pct, "%")} 增至 ${number(evidence.trend_current_pct ?? evidence.after_gc_pct, "%")}，窗口内增长 ${signed(evidence.increase_pct_points, " 个百分点")}；稳健趋势为每小时 ${signed(evidence.slope_pct_points_per_hour, " 个百分点")}，预计约 ${number(evidence.eta_hours, " 小时")} 后达到 ${number(evidence.danger_pct, "%")} 危险线。`
    }
    return `${job} 的 GC 后 Old Gen 使用率为 ${number(evidence.after_gc_pct, "%")}，已连续取得 ${evidence.samples || "多"} 个有效样本。高位长期不回落可能表示堆压力或内存泄漏，需要结合 Old/Full GC 次数和耗时继续观察。`
  }
  if (finding.resource === "filesystem") {
    return `${evidence.mountpoint || "文件系统"} 当前按 ${number(evidence.slope_bytes_per_hour, " B/h")} 的速度增长，预计约 ${number(evidence.eta_hours, " 小时")} 后达到 ${number(evidence.target_pct, "%")} 阈值。`
  }
  if (finding.resource === "cpu") {
    return `最近窗口 CPU P95 为 ${number(evidence.p95, "%")}，基于 ${evidence.samples || "多"} 个采集点判定为持续负载风险。`
  }
  if (finding.resource === "memory") {
    return `最近窗口最低可用内存为 ${number(evidence.available_pct, "%")}；该指标使用 MemAvailable 口径，已排除大部分 page cache 误判。`
  }
  return "该风险由本地资源快照趋势分析产生，请结合节点与服务指标进一步确认。"
}

export function riskCardTone(findings: ResourceFinding[]) {
  if (findings.some((item) => item.severity === "critical")) {
    return "border-red-500/50 bg-red-500/10 hover:bg-red-500/15"
  }
  if (findings.some((item) => item.severity === "warning")) {
    return "border-orange-500/50 bg-orange-500/10 hover:bg-orange-500/15"
  }
  if (findings.length) {
    return "border-amber-500/50 bg-amber-500/10 hover:bg-amber-500/15"
  }
  return "border-emerald-500/40 bg-emerald-500/10 hover:bg-emerald-500/15"
}

function number(value: unknown, suffix: string) {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? `${parsed.toFixed(1)}${suffix}` : "未知"
}

function signed(value: unknown, suffix: string) {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? `${parsed >= 0 ? "+" : ""}${parsed.toFixed(2)}${suffix}` : "未知"
}

function finite(value: unknown) {
  return Number.isFinite(Number(value)) && value !== null
}
