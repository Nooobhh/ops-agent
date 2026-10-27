import assert from "node:assert/strict"
import test from "node:test"

import {
  hourlyFrequency,
  occurrenceStartTime,
  occurrenceTrend,
} from "../src/lib/alertCharts.ts"
import { setEnvTimeZone } from "../src/lib/envTime.ts"
import type { TimelineEvent } from "../src/types.ts"

setEnvTimeZone("Etc/GMT-8") // 固定 UTC+8(Etc 时区符号相反),下文注释的「北京」即此时区

const NOW = Date.parse("2026-08-09T06:30:00Z") // 北京 2026-08-09 14:30

function event(
  ts: string,
  type = "new",
  detail: Record<string, unknown> | null = null,
): TimelineEvent {
  return {
    event_id: `${type}-${ts}`, ts, type, key: "prom:HighLoad:node1",
    source: "prometheus", platform: "alpha", alertname: "HighLoad", host: "node1",
    alert_class: null, detail, suppressed_by: null, severity: "warning", service: null,
  }
}

test("occurrence start prefers the source-side trigger time", () => {
  assert.equal(
    occurrenceStartTime(event("2026-08-09T06:00:00Z", "new", {
      source_active_at: "2026-08-09T05:00:00Z",
    })),
    "2026-08-09T05:00:00Z",
  )
  assert.equal(occurrenceStartTime(event("2026-08-09T06:00:00Z")), "2026-08-09T06:00:00Z")
})

test("hourly frequency folds occurrences onto Beijing clock hours", () => {
  const events = [
    event("2026-08-09T06:00:00Z"), // 北京 14:00
    event("2026-08-08T22:00:00Z"), // 北京 06:00
    event("2026-08-05T02:00:00Z"), // 北京 10:00，超出 1d 窗口
    event("2026-08-09T05:00:00Z", "resolved"), // 非出现事件
  ]

  const day = hourlyFrequency(events, "1d", NOW)
  assert.equal(day.length, 24)
  assert.equal(day[14].count, 1)
  assert.equal(day[6].count, 1)
  assert.equal(day[10].count, 0)
  assert.equal(day.reduce((sum, bar) => sum + bar.count, 0), 2)

  const week = hourlyFrequency(events, "7d", NOW)
  assert.equal(week[10].count, 1)
  assert.equal(week.reduce((sum, bar) => sum + bar.count, 0), 3)
  assert.deepEqual([week[0].label, week[23].label], ["00:00", "23:00"])
})

test("hourly frequency uses the source trigger time, not the poll timestamp", () => {
  const events = [event("2026-08-09T06:00:00Z", "new", {
    source_active_at: "2026-08-09T03:00:00Z", // 北京 11:00
  })]
  const bars = hourlyFrequency(events, "1d", NOW)
  assert.equal(bars[11].count, 1)
  assert.equal(bars[14].count, 0)
})

test("occurrence trend buckets 1d by hour ending at the current hour", () => {
  const bars = occurrenceTrend([
    event("2026-08-09T06:00:00Z"),
    event("2026-08-08T22:00:00Z"),
    event("2026-08-05T02:00:00Z"), // 窗口外
  ], "1d", NOW)

  assert.equal(bars.length, 24)
  assert.equal(bars[0].label, "15:00") // 北京 8-8 15:00
  assert.equal(bars[23].label, "14:00")
  assert.equal(bars[23].count, 1)
  assert.equal(bars[15].count, 1)
  assert.equal(bars.reduce((sum, bar) => sum + bar.count, 0), 2)
})

test("occurrence trend buckets 7d/30d by Beijing calendar day", () => {
  const events = [
    event("2026-08-09T06:00:00Z"),
    event("2026-08-08T22:00:00Z"), // 北京 8-9，与上一条同一天
    event("2026-08-05T02:00:00Z"), // 北京 8-5
    event("2026-07-01T02:00:00Z"), // 超出 30d 窗口（起点为北京 07-11）
  ]

  const week = occurrenceTrend(events, "7d", NOW)
  assert.equal(week.length, 7)
  assert.deepEqual([week[0].label, week[6].label], ["08-03", "08-09"])
  assert.equal(week[6].count, 2)
  assert.equal(week[2].count, 1)

  const month = occurrenceTrend(events, "30d", NOW)
  assert.equal(month.length, 30)
  assert.equal(month[29].label, "08-09")
  assert.equal(month.reduce((sum, bar) => sum + bar.count, 0), 3)
})
