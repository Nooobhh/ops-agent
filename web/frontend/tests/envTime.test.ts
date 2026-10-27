import assert from "node:assert/strict"
import test from "node:test"

import { getEnvTimeZone, setEnvTimeZone, zonedDate, zonedMidnightIso, zoneOffsetMs } from "../src/lib/envTime.ts"

test("env time zone is set once the API answers and ignores empty values", () => {
  setEnvTimeZone("Etc/GMT-8")
  setEnvTimeZone("")
  assert.equal(getEnvTimeZone(), "Etc/GMT-8")
})

test("dates and midnights follow the env zone, not the browser", () => {
  const at = Date.parse("2026-08-08T20:30:00Z")
  assert.equal(zonedDate(at, "Etc/GMT-8"), "2026-08-09")
  assert.equal(zonedDate(at, "UTC"), "2026-08-08")
  assert.equal(zonedMidnightIso("2026-08-09", "Etc/GMT-8"), "2026-08-08T16:00:00.000Z")
})

test("offsets track daylight saving per instant", () => {
  const hour = 60 * 60 * 1000
  assert.equal(zoneOffsetMs(Date.parse("2026-01-15T12:00:00Z"), "America/New_York"), -5 * hour)
  assert.equal(zoneOffsetMs(Date.parse("2026-07-15T12:00:00Z"), "America/New_York"), -4 * hour)
  assert.equal(zonedMidnightIso("2026-07-15", "America/New_York"), "2026-07-15T04:00:00.000Z")
})
