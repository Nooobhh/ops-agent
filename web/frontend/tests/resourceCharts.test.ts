import assert from "node:assert/strict"
import test from "node:test"

import { buildResourceCharts } from "../src/lib/resourceCharts.ts"
import type { ResourceHistoryResponse } from "../src/types.ts"

const history = {
  platform: "mysql", node: "198.51.100.101", hours: 24,
  points: ["2026-08-31T00:00:00+00:00", "2026-08-31T00:05:00+00:00"].map((collected_at, index) => ({
    collected_at, status: "ok",
    nodes: [{
      node: "198.51.100.101", platform: "mysql", instance: "198.51.100.101",
      sample_at: collected_at, status: "ok",
      metrics: {
        cpu_used_pct: 10 + index, memory_used_pct: 70 + index,
        load1: 1 + index, load5: 2 + index, load15: 3 + index,
      },
      filesystems: [{
        device: "/dev/sda", mountpoint: "/", fstype: "xfs", kind: "local" as const,
        used_pct: 30 + index, inode_used_pct: null,
        available_bytes: 1000 - index, size_bytes: null,
      }],
    }],
    services: [{
      node: "198.51.100.101", platform: "mysql", service: "svc", component: "SVC",
      job: "svc_exporter", role: null, profile: "standard", status: "ok",
      sample_at: collected_at,
      metrics: { heap_used_pct: 50 + index, heap_max_bytes: 1000 },
      gc: { young: { gc_count_total: 10 + index, gc_time_seconds_total: 2 + index } },
      old_gen_after_gc_bytes: { old: 500 + index },
    }],
    business_metrics: [
      { platform: "mysql", node: "198.51.100.101", service: "MySQL", job: "mysql_exporter1", metric: "qps", label: "QPS", unit: "per_second", value: 20 + index, status: "ok", severity: "healthy", sample_at: collected_at, details: {} },
      { platform: "mysql", node: "198.51.100.101", service: "MySQL", job: "mysql_exporter1", metric: "lag", label: "主从延迟", unit: "seconds", value: index, status: "ok", severity: "healthy", sample_at: collected_at, details: {} },
    ],
  })),
} satisfies ResourceHistoryResponse

test("node history creates one chart for every populated numeric field", () => {
  const charts = buildResourceCharts(history, {
    kind: "node", platform: "mysql", node: "198.51.100.101", title: "node",
  })
  assert.deepEqual(charts.map((chart) => chart.key), [
    "node:cpu_used_pct", "node:memory_used_pct",
    "node:load",
    "filesystem:used_pct", "filesystem:available_bytes",
  ])
  assert.equal(charts[0].data.length, 2)
  assert.deepEqual(charts[2].series.map((series) => series.label), [
    "Load 1 分钟", "Load 5 分钟", "Load 15 分钟",
  ])
})

test("service and business history expand all their metric columns", () => {
  const service = buildResourceCharts(history, {
    kind: "service", platform: "mysql", node: "198.51.100.101", title: "svc",
    job: "svc_exporter", role: null,
  })
  const business = buildResourceCharts(history, {
    kind: "business", platform: "mysql", node: "198.51.100.101", title: "mysql",
    job: "mysql_exporter1", service: "MySQL",
  })
  assert.equal(service.length, 5)
  assert.equal(business.length, 2)
})
