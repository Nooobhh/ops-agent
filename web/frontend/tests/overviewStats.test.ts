import assert from "node:assert/strict"
import test from "node:test"

import { OVERVIEW_STAT_CARDS, overviewStatTone } from "../src/lib/overviewStats.ts"


test("overview cards use duration-based chronic statistics and filters", () => {
  assert.deepEqual(
    OVERVIEW_STAT_CARDS.map(({ key, label, filterUrl }) => ({
      key,
      label,
      filterUrl,
    })),
    [
      {
        key: "active_non_chronic",
        label: "活跃（非常驻）",
        filterUrl: "/alerts?chronic=exclude&status=active,suppressed",
      },
      {
        key: "new_today",
        label: "今日新增",
        filterUrl: "today",
      },
      {
        key: "suppressed_today",
        label: "今日抑制",
        filterUrl: "/alerts?suppressed_today=true",
      },
      {
        key: "chronic",
        label: "常驻",
        filterUrl: "/alerts?chronic=only",
      },
    ],
  )
})

test("overview alert card tones follow the requested thresholds", () => {
  assert.match(overviewStatTone("active_non_chronic", 1), /red/)
  assert.match(overviewStatTone("new_today", 9), /emerald/)
  assert.match(overviewStatTone("new_today", 10), /amber/)
  assert.match(overviewStatTone("new_today", 20), /amber/)
  assert.match(overviewStatTone("new_today", 21), /red/)
  assert.match(overviewStatTone("chronic", 1), /amber/)
  assert.equal(overviewStatTone("suppressed_today", 1), "text-foreground")
})
