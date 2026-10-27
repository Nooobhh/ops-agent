import assert from "node:assert/strict"
import test from "node:test"

import { CREDENTIAL_FIELDS, enabledPluginHealth } from "../src/lib/pluginsApi.ts"


test("top bar plugin health counts only enabled plugins", () => {
  const health = (state: string, enabled: boolean) => ({state, enabled, detail: null, version: null, checked_at: ""})
  assert.deepEqual(enabledPluginHealth(null), {healthy: 0, total: 0})
  assert.deepEqual(enabledPluginHealth({schema_version: 1, plugins: {
    a: health("ok", true), b: health("unreachable", true), c: health("ok", false),
  }} as never), {healthy: 1, total: 2})
})


test("credential fields follow the auth scheme", () => {
  assert.deepEqual(CREDENTIAL_FIELDS.basic.map((item) => item.key), ["user", "password"])
  assert.deepEqual(CREDENTIAL_FIELDS.none, [])
  assert.ok(CREDENTIAL_FIELDS.bearer.every((item) => item.secret))
})
