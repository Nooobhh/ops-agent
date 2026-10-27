import assert from "node:assert/strict"
import test from "node:test"
import {
  barPosition,
  dateRangeLabel,
  groupBuckets,
  historyLabel,
  isOverdue,
  markerPosition,
  planSpan,
} from "../src/lib/workItemsView.ts"
import type { WeeklyBucketEntry } from "../src/lib/workItemsTypes.ts"

test("overdue only for open items past their due date", () => {
  assert.equal(isOverdue({ status: "todo", due_date: "2026-10-20" }, "2026-10-23"), true)
  assert.equal(isOverdue({ status: "done", due_date: "2026-10-20" }, "2026-10-23"), false)
  assert.equal(isOverdue({ status: "todo", due_date: "2026-10-23" }, "2026-10-23"), false)
  assert.equal(isOverdue({ status: "todo", due_date: null }, "2026-10-23"), false)
})

test("date range label covers both, one or no dates", () => {
  assert.equal(dateRangeLabel("2026-10-19", "2026-10-23"), "10/19 – 10/23")
  assert.equal(dateRangeLabel("2026-10-23", "2026-10-23"), "10/23")
  assert.equal(dateRangeLabel(null, "2026-10-28"), "截止 10/28")
  assert.equal(dateRangeLabel("2026-10-28", null), "10/28 开始")
  assert.equal(dateRangeLabel(null, null), "")
})

test("gantt bars are positioned in whole days across the plan span", () => {
  const items = [
    { start_date: "2026-10-01", due_date: "2026-10-05" },
    { start_date: null, due_date: "2026-10-10" },
    { start_date: null, due_date: null },
  ]
  const span = planSpan(items)
  assert.deepEqual(span, { start: "2026-10-01", end: "2026-10-10" })
  assert.deepEqual(barPosition(items[0], span!), { left: 0, width: 50 })
  assert.deepEqual(barPosition(items[1], span!), { left: 90, width: 10 })
  assert.equal(barPosition(items[2], span!), null)
  assert.equal(markerPosition("2026-10-01", span!), 5)
  assert.equal(markerPosition("2026-10-11", span!), null)
  assert.equal(planSpan([{ start_date: null, due_date: null }]), null)
})

test("weekly entries group by bucket", () => {
  const entry = (bucket: WeeklyBucketEntry["bucket"], id: string) => ({
    bucket, in_next_period: false, overdue: false, worklog_refs: [], item: { id } as WeeklyBucketEntry["item"],
  })
  const groups = groupBuckets([entry("completed", "a"), entry("planned", "b"), entry("completed", "c")])
  assert.deepEqual(groups.completed.map((item) => item.item.id), ["a", "c"])
  assert.deepEqual(groups.in_progress, [])
  assert.deepEqual(groups.planned.map((item) => item.item.id), ["b"])
})

test("history labels describe field changes", () => {
  assert.equal(historyLabel({ ts: "", by: null, action: "status", changes: { status: ["todo", "done"] } }), "状态 待办 → 完成")
  assert.equal(historyLabel({ ts: "", by: null, action: "linked", changes: { link: [null, "task:tr-1"] } }), "挂载 task:tr-1")
  assert.equal(historyLabel({ ts: "", by: null, action: "created", changes: {} }), "创建")
  assert.equal(
    historyLabel({ ts: "", by: null, action: "updated", changes: { due_date: [null, "2026-10-30"], description: ["a", "b"] } }),
    "截止 空 → 2026-10-30；修改描述",
  )
})
