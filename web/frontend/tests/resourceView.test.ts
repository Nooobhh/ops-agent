import assert from "node:assert/strict"
import test from "node:test"

import {
  compareValues,
  metricTone,
  riskDescription,
  riskTitle,
} from "../src/lib/resourceView.ts"

test("metric colors progress from green to red", () => {
  assert.match(metricTone(20, "cpu"), /emerald/)
  assert.match(metricTone(60, "cpu"), /amber/)
  assert.match(metricTone(75, "cpu"), /orange/)
  assert.match(metricTone(90, "cpu"), /red/)
})

test("sorting is natural and always keeps missing values last", () => {
  assert.ok(compareValues("node2", "node10", "asc") < 0)
  assert.ok(compareValues(90, 20, "desc") < 0)
  assert.ok(compareValues(null, 20, "desc") > 0)
})

test("JVM findings render an operator-readable explanation", () => {
  const finding = {
    key: "risk",
    severity: "warning" as const,
    platform: "beta",
    node: "node14",
    resource: "jvm",
    evidence: { after_gc_pct: 80.349, job: "hbase_regionserver_exporter", samples: 6 },
  }
  assert.equal(riskTitle(finding), "BETA / node14-hbase_regionserver-GC 后 Old Gen 使用率")
  assert.match(riskDescription(finding), /80\.3%/)
  assert.match(riskDescription(finding), /HBase|hbase/i)
})

test("JVM growth findings explain increase, slope, danger line, and ETA", () => {
  const finding = {
    key: "growth-risk",
    severity: "warning" as const,
    platform: "beta",
    node: "node14",
    resource: "jvm",
    evidence: {
      job: "hbase_regionserver_exporter",
      start_pct: 60,
      after_gc_pct: 62.2,
      trend_current_pct: 62.2,
      increase_pct_points: 2.2,
      slope_pct_points_per_hour: 0.2,
      eta_hours: 114,
      danger_pct: 85,
    },
  }
  assert.equal(riskTitle(finding), "BETA / node14-hbase_regionserver-GC 后 Old Gen 增长预测")
  assert.match(riskDescription(finding), /60\.0%.*62\.2%/)
  assert.match(riskDescription(finding), /\+2\.20 个百分点/)
  assert.match(riskDescription(finding), /114\.0 小时/)
  assert.match(riskDescription(finding), /85\.0% 危险线/)
})

test("JVM current heap findings use the current metric wording", () => {
  const finding = {
    key: "heap-current",
    alert_type: "current" as const,
    severity: "critical" as const,
    platform: "beta",
    node: "node14",
    resource: "jvm",
    evidence: { job: "hbase_regionserver_exporter", metric: "heap_used_pct", heap_used_pct: 91, recent_median_pct: 91, warning_pct: 80 },
  }
  assert.equal(riskTitle(finding), "BETA / node14-hbase_regionserver-Heap 使用率")
  assert.match(riskDescription(finding), /最近一次 Heap 采样为 91\.0%/)
  assert.match(riskDescription(finding), /现状严重阈值/)
})

test("business risk title includes platform, node, service, and metric", () => {
  const finding = {
    key: "business-risk", severity: "critical" as const,
    platform: "beta", node: "node1", resource: "business",
    evidence: { service: "MySQL", label: "当前行锁等待" },
  }
  assert.equal(riskTitle(finding), "BETA / node1-MySQL-当前行锁等待")
})
