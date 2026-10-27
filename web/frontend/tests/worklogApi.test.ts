import assert from "node:assert/strict"
import test from "node:test"
import { filterWeeklyReports } from "../src/lib/worklogView.ts"
import type { WeeklyReportItem } from "../src/types.ts"

const reports: WeeklyReportItem[] = [
  { filename: "aug.docx", start_date: "2026-08-06", end_date: "2026-08-12", updated_at: "" },
  { filename: "cross.docx", start_date: "2026-06-25", end_date: "2026-07-01", updated_at: "" },
  { filename: "june.docx", start_date: "2026-06-18", end_date: "2026-06-24", updated_at: "" },
]

test("weekly report date filter includes periods overlapping the selected range", () => {
  assert.deepEqual(
    filterWeeklyReports(reports, "2026-07-01", "2026-07-31").map((item) => item.filename),
    ["cross.docx"],
  )
})

test("weekly report date filter supports either boundary and rejects an invalid range", () => {
  assert.deepEqual(
    filterWeeklyReports(reports, "2026-07-01", "").map((item) => item.filename),
    ["aug.docx", "cross.docx"],
  )
  assert.deepEqual(
    filterWeeklyReports(reports, "", "2026-06-30").map((item) => item.filename),
    ["cross.docx", "june.docx"],
  )
  assert.deepEqual(filterWeeklyReports(reports, "2026-08-01", "2026-07-01"), [])
})
