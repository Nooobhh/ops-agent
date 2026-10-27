import assert from "node:assert/strict"
import test from "node:test"

import { buildAlertMetaTokens } from "../src/lib/alertMeta.ts"


test("renders every tag before the chronic duration note", () => {
  const tokens = buildAlertMetaTokens({
    chronic: true,
    effective_class: "flapping",
    muted: true,
    first_seen: "2026-07-24T00:00:00Z",
  }, Date.parse("2026-07-25T01:00:00Z"))

  assert.deepEqual(
    tokens.map((token) => token.kind),
    ["tag", "tag", "tag", "duration"],
  )
  assert.deepEqual(
    tokens
      .filter((token) => token.kind === "tag")
      .map((token) => token.label),
    ["常驻", "波动", "已静音"],
  )
})


test("flapping has a tag without a duration note", () => {
  const tokens = buildAlertMetaTokens({
    chronic: false,
    class: "flapping",
    effective_class: "flapping",
    muted: false,
    first_seen: "2026-07-24T00:00:00Z",
  })

  assert.deepEqual(tokens, [
    { kind: "tag", status: "flapping", label: "波动" },
  ])
})


test("muted flapping keeps both classification and muted tags", () => {
  const tokens = buildAlertMetaTokens({
    chronic: false,
    class: "flapping",
    effective_class: "ignore",
    muted: true,
    first_seen: "2026-07-24T00:00:00Z",
  })

  assert.deepEqual(tokens, [
    { kind: "tag", status: "flapping", label: "波动" },
    { kind: "tag", status: "muted", label: "已静音" },
  ])
})


test("watch has no visible classification tag", () => {
  const tokens = buildAlertMetaTokens({
    chronic: false,
    class: "watch",
    effective_class: "watch",
    muted: false,
    first_seen: "2026-07-24T00:00:00Z",
  })

  assert.deepEqual(tokens, [])
})
