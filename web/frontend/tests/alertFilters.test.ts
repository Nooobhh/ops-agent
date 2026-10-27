import assert from "node:assert/strict"
import test from "node:test"

import {
  ALERT_CLASS_FILTER_OPTIONS,
  alertClassSelectionSummary,
  normalizeAlertClassFilter,
} from "../src/lib/alertFilters.ts"


test("class multi-select normalizes, deduplicates, and orders URL values", () => {
  assert.deepEqual(normalizeAlertClassFilter(null), [])
  assert.deepEqual(normalizeAlertClassFilter("flapping"), ["flapping"])
  assert.deepEqual(
    normalizeAlertClassFilter("flapping,watch,broken,watch"),
    ["watch", "flapping"],
  )
})


test("class multi-select exposes only the four Chinese choices", () => {
  assert.deepEqual(ALERT_CLASS_FILTER_OPTIONS, [
    { value: "watch", label: "观察" },
    { value: "chronic", label: "常驻" },
    { value: "ignore", label: "忽略" },
    { value: "flapping", label: "波动" },
  ])
})


test("class multi-select summarizes the current selection in Chinese", () => {
  assert.equal(alertClassSelectionSummary([]), "全部")
  assert.equal(
    alertClassSelectionSummary(["watch", "flapping"]),
    "观察、波动",
  )
})
