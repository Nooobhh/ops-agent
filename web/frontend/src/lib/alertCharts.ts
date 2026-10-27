import type { TimelineEvent } from "@/types"

import { zoneOffsetMs } from "./envTime.ts"

const HOUR_MS = 60 * 60 * 1000
const DAY_MS = 24 * HOUR_MS

export const ALERT_CHART_RANGES = [
  { value: "1d", label: "1 天" },
  { value: "7d", label: "7 天" },
  { value: "30d", label: "30 天" },
] as const

export type AlertChartRange = (typeof ALERT_CHART_RANGES)[number]["value"]

export type FrequencyBar = { hour: number; label: string; count: number }
export type TrendBar = { bucket: string; label: string; count: number }

/** 一次出现的起点：优先取监控源记录的触发时间，回退轮询首见。 */
export function occurrenceStartTime(event: TimelineEvent): string {
  const detail = event.detail as Record<string, unknown> | null
  const value = detail && typeof detail === "object" ? detail.source_active_at : null
  return typeof value === "string" && value ? value : event.ts
}

function occurrenceTimestamps(events: TimelineEvent[]): number[] {
  return events
    .filter((event) => event.type === "new")
    .map((event) => new Date(occurrenceStartTime(event)).getTime())
    .filter((value) => !Number.isNaN(value))
}

function bucketCount(range: AlertChartRange): number {
  return range === "1d" ? 24 : range === "7d" ? 7 : 30
}

/** 窗口起点：1d 对齐到整点，7d/30d 对齐到环境时区 00:00。 */
function windowStart(range: AlertChartRange, now: number): number {
  if (range === "1d") return Math.floor(now / HOUR_MS) * HOUR_MS - 23 * HOUR_MS
  const offset = zoneOffsetMs(now)
  const midnight = Math.floor((now + offset) / DAY_MS) * DAY_MS - offset
  return midnight - (bucketCount(range) - 1) * DAY_MS
}

function pad(value: number): string {
  return String(value).padStart(2, "0")
}

function bucketLabel(bucketStart: number, range: AlertChartRange): string {
  const shifted = new Date(bucketStart + zoneOffsetMs(bucketStart))
  if (range === "1d") return `${pad(shifted.getUTCHours())}:00`
  return `${pad(shifted.getUTCMonth() + 1)}-${pad(shifted.getUTCDate())}`
}

/** 频率图：把窗口内每次出现按「环境时区几点」折叠累加，横轴固定 0~23 时。 */
export function hourlyFrequency(
  events: TimelineEvent[],
  range: AlertChartRange,
  now = Date.now(),
): FrequencyBar[] {
  const start = windowStart(range, now)
  const bars: FrequencyBar[] = Array.from({ length: 24 }, (_, hour) => ({
    hour, label: `${pad(hour)}:00`, count: 0,
  }))
  for (const timestamp of occurrenceTimestamps(events)) {
    if (timestamp < start || timestamp > now) continue
    bars[Math.floor(((timestamp + zoneOffsetMs(timestamp)) % DAY_MS) / HOUR_MS)].count += 1
  }
  return bars
}

/** 出现次数图：横轴真实时间，1d 按小时桶、7d/30d 按天桶，口径与首页时间分布一致。 */
export function occurrenceTrend(
  events: TimelineEvent[],
  range: AlertChartRange,
  now = Date.now(),
): TrendBar[] {
  const start = windowStart(range, now)
  const step = range === "1d" ? HOUR_MS : DAY_MS
  const count = bucketCount(range)
  const bars: TrendBar[] = Array.from({ length: count }, (_, index) => {
    const bucketStart = start + index * step
    return {
      bucket: new Date(bucketStart).toISOString(),
      label: bucketLabel(bucketStart, range),
      count: 0,
    }
  })
  for (const timestamp of occurrenceTimestamps(events)) {
    const index = Math.floor((timestamp - start) / step)
    if (index < 0 || index >= count) continue
    bars[index].count += 1
  }
  return bars
}
