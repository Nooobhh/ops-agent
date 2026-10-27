import assert from "node:assert/strict"
import test from "node:test"

import {
  BUILD_WIKI_COMMAND,
  coverageRows,
  healthFindings,
  verifiedRatioLabel,
} from "../src/lib/knowledgeHealth.ts"
import { setPlatformLabels } from "../src/lib/knowledgeView.ts"
import type { KnowledgeHealth } from "../src/types.ts"


function health(overrides: Partial<KnowledgeHealth> = {}): KnowledgeHealth {
  return {
    index: { available: true, built_at: "x", source_latest_at: "x", stale: false, hint: null },
    documents_total: 137,
    status_counts: { draft: 135, verified: 2, other: 0 },
    type_counts: { troubleshoot: 58, knowledge: 67, procedure: 12 },
    platform_coverage: [
      { platform: "alpha", documents: 82, troubleshoot: 42, with_alerts: 17, draft: 81, verified: 1 },
      { platform: "beta", documents: 37, troubleshoot: 12, with_alerts: 4, draft: 36, verified: 1 },
    ],
    recall_orphans: { count: 0, troubleshoot_total: 58, documents: [] },
    promotion_candidates: [],
    promotion_reviews: [],
    ...overrides,
  }
}

function keys(input: KnowledgeHealth) {
  return healthFindings(input).map((finding) => finding.key)
}


test("a healthy knowledge base reports no findings", () => {
  assert.deepEqual(keys(health()), [])
  assert.deepEqual(healthFindings(null), [])
})


test("missing index is an error, stale index only a warning", () => {
  const missing = healthFindings(health({
    index: { available: false, built_at: null, source_latest_at: null, stale: true, hint: "x" },
  }))
  assert.equal(missing[0].key, "index-missing")
  assert.equal(missing[0].level, "error")
  assert.equal(missing[0].command, BUILD_WIKI_COMMAND)

  const stale = healthFindings(health({
    index: { available: true, built_at: "a", source_latest_at: "b", stale: true, hint: "x" },
  }))
  assert.equal(stale[0].key, "index-stale")
  assert.equal(stale[0].level, "warn")
})


test("index findings are mutually exclusive", () => {
  // 索引不存在时不该再抱怨它「过期」
  const findings = keys(health({
    index: { available: false, built_at: null, source_latest_at: null, stale: true, hint: "x" },
  }))
  assert.equal(findings.filter((key) => key.startsWith("index-")).length, 1)
})


test("recall orphans report their share of troubleshoot docs", () => {
  const findings = healthFindings(health({
    recall_orphans: {
      count: 29, troubleshoot_total: 58,
      documents: [{ path: "alpha/hdfs/x.md", title: "x", platform: "alpha", service: "hdfs" }],
    },
  }))
  const orphan = findings.find((item) => item.key === "recall-orphans")
  assert.equal(orphan?.title, "29 篇排查文档召回不到")
  assert.match(orphan?.detail ?? "", /50%/)
  assert.equal(orphan?.command, BUILD_WIKI_COMMAND)
})


test("promotion candidates suggest the CLI promote command, never a button", () => {
  const findings = healthFindings(health({
    promotion_candidates: [
      {
        path: "alpha/yarn/a.md", title: "YARN 资源压力", platform: "alpha", service: "yarn",
        eligible: true, run_id: "ts-1", attempts: [],
      },
    ],
  }))
  const candidate = findings.find((item) => item.key === "promotion-candidates")
  assert.match(candidate?.command ?? "", /lib\.kb promote/)
})


test("coverage rows translate platforms and compute verified share", () => {
  setPlatformLabels({ alpha: "Alpha 平台", beta: "Beta 平台", shared: "跨平台" })
  const rows = coverageRows(health())
  assert.deepEqual(rows.map((row) => row.label), ["Alpha 平台", "Beta 平台"])
  assert.equal(rows[0].verifiedRatio, "1%")
  assert.equal(rows[1].verifiedRatio, "3%")
})


test("verified ratio label counts only draft and verified", () => {
  assert.equal(verifiedRatioLabel(health()), "2 / 137")
  assert.equal(verifiedRatioLabel(null), "-")
})
