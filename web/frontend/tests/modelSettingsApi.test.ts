import assert from "node:assert/strict"
import test from "node:test"

import {
  createModel,
  deleteModel,
  loadModelSettings,
  updateDefaults,
  updateModel,
} from "../src/lib/modelSettingsApi.ts"
import {
  deleteBlocker,
  isSettingsPath,
  manualDefaultSelection,
  selectionForModel,
} from "../src/lib/modelSettingsView.ts"
import type { ModelSettingsResponse } from "../src/types.ts"


function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: {"Content-Type": "application/json"},
  })
}


const settingsFixture: ModelSettingsResponse = {
  schema_version: 1,
  models: [
    {
      id: "model-01",
      title: "Primary",
      base_url: "https://primary.test/v1",
      model: "primary-model",
      api_key_configured: true,
      default_reasoning_effort: "low",
    },
    {
      id: "model-02",
      title: "Secondary",
      base_url: "https://secondary.test/v1",
      model: "secondary-model",
      api_key_configured: true,
      default_reasoning_effort: "xhigh",
    },
  ],
  defaults: {
    auto: {model_id: "model-01", reasoning_effort: "high", timeout_seconds: 120},
    manual: {model_id: "model-01", reasoning_effort: "medium", timeout_seconds: 120},
    weekly: {model_id: "model-01", reasoning_effort: "low", timeout_seconds: 300},
  },
  error: null,
}


test("serializes model update without an empty api key", async (context) => {
  const originalFetch = globalThis.fetch
  context.after(() => { globalThis.fetch = originalFetch })
  let body: unknown
  globalThis.fetch = async (_input, init) => {
    body = JSON.parse(String(init?.body))
    return jsonResponse(settingsFixture)
  }

  await updateModel("model-01", {
    title: "Primary",
    base_url: "https://relay.test/v1",
    api_key: "",
    model: "gpt-test",
    default_reasoning_effort: "high",
  })

  assert.deepEqual(body, {
    title: "Primary",
    base_url: "https://relay.test/v1",
    model: "gpt-test",
    default_reasoning_effort: "high",
  })
})


test("uses the expected HTTP methods for model settings mutations", async (context) => {
  const originalFetch = globalThis.fetch
  context.after(() => { globalThis.fetch = originalFetch })
  const requests: Array<[string, string]> = []
  globalThis.fetch = async (input, init) => {
    requests.push([String(input), String(init?.method || "GET")])
    return jsonResponse(settingsFixture)
  }

  await loadModelSettings()
  await createModel({
    title: "Primary",
    base_url: "https://relay.test/v1",
    api_key: "secret",
    model: "gpt-test",
    default_reasoning_effort: "medium",
  })
  await updateDefaults(settingsFixture.defaults!)
  await deleteModel("model-01")

  assert.deepEqual(requests, [
    ["/api/model-settings", "GET"],
    ["/api/model-settings/models", "POST"],
    ["/api/model-settings/defaults", "PUT"],
    ["/api/model-settings/models/model-01", "DELETE"],
  ])
})


test("manual task starts from the workflow default", () => {
  assert.deepEqual(manualDefaultSelection(settingsFixture), {
    model_id: "model-01",
    reasoning_effort: "medium",
    timeout_seconds: 120,
  })
})


test("changing model selects that profile default effort", () => {
  assert.deepEqual(selectionForModel(settingsFixture, "model-02"), {
    model_id: "model-02",
    reasoning_effort: "xhigh",
    timeout_seconds: 120,
  })
})


test("workflow defaults expose their delete blocker", () => {
  assert.equal(
    deleteBlocker(settingsFixture, "model-01"),
    "该模型正在被默认模型配置使用",
  )
  assert.equal(deleteBlocker(settingsFixture, "model-02"), null)
})


test("settings navigation treats both routes as settings", () => {
  assert.equal(isSettingsPath("/settings/notify"), true)
  assert.equal(isSettingsPath("/settings/alerts"), true)
  assert.equal(isSettingsPath("/settings/models"), true)
  assert.equal(isSettingsPath("/settings/user"), true)
  assert.equal(isSettingsPath("/alerts"), false)
})


test("model upsert carries the harness tier, null clears it", async (context) => {
  const originalFetch = globalThis.fetch
  context.after(() => { globalThis.fetch = originalFetch })
  const bodies: unknown[] = []
  globalThis.fetch = async (_input, init) => {
    bodies.push(JSON.parse(String(init?.body)))
    return jsonResponse(settingsFixture)
  }
  const base = {title: "P", base_url: "https://relay.test/v1", model: "m", default_reasoning_effort: "none" as const}

  await updateModel("model-01", {...base, harness_tier: "guided"})
  await updateModel("model-01", {...base, harness_tier: null})

  assert.deepEqual(bodies.map((body) => (body as {harness_tier?: unknown}).harness_tier), ["guided", null])
})
