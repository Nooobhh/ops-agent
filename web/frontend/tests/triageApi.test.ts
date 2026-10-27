import assert from "node:assert/strict"
import test from "node:test"

import {
  createTriageTask,
  loadTriageEvents,
  loadTriageHealth,
  retryTriageTask,
} from "../src/lib/triageApi.ts"


function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: {"Content-Type": "application/json"},
  })
}


test("validates health schema before returning it", async (context) => {
  const originalFetch = globalThis.fetch
  context.after(() => { globalThis.fetch = originalFetch })
  globalThis.fetch = async () => jsonResponse({
    schema_version: 2,
    model: "configured",
    can_create: true,
    worker: {state: "running", pid: 12},
    queue: {queued: 0, running: 0, waiting_input: 0, waiting_approval: 0},
  })

  await assert.rejects(loadTriageHealth(), /不支持的数据版本/)
})


test("encodes the incremental event cursor", async (context) => {
  const originalFetch = globalThis.fetch
  context.after(() => { globalThis.fetch = originalFetch })
  let requested = ""
  globalThis.fetch = async (input) => {
    requested = String(input)
    return jsonResponse({schema_version: 1, items: []})
  }

  await loadTriageEvents("tr-a/b", "ev 1")

  assert.equal(
    requested,
    "/api/triage-tasks/tr-a%2Fb/events?after=ev+1",
  )
})


test("creates a task with only explicitly provided optional fields", async (context) => {
  const originalFetch = globalThis.fetch
  context.after(() => { globalThis.fetch = originalFetch })
  let request: RequestInit | undefined
  globalThis.fetch = async (_input, init) => {
    request = init
    return jsonResponse({
      schema_version: 1,
      task: {task_id: "tr-1"},
      events: [],
    }, 201)
  }

  await createTriageTask({message: "检查内存压力"})

  assert.equal(request?.method, "POST")
  assert.deepEqual(JSON.parse(String(request?.body)), {
    message: "检查内存压力",
  })
})


test("creates a task with an explicit model selection", async (context) => {
  const originalFetch = globalThis.fetch
  context.after(() => { globalThis.fetch = originalFetch })
  let request: RequestInit | undefined
  globalThis.fetch = async (_input, init) => {
    request = init
    return jsonResponse({
      schema_version: 1,
      task: {task_id: "tr-1"},
      events: [],
    }, 201)
  }

  await createTriageTask({
    message: "检查",
    model_id: "model-02",
    reasoning_effort: "high",
  })

  assert.deepEqual(JSON.parse(String(request?.body)), {
    message: "检查",
    model_id: "model-02",
    reasoning_effort: "high",
  })
})


test("retries a failed task through the dedicated endpoint", async (context) => {
  const originalFetch = globalThis.fetch
  context.after(() => { globalThis.fetch = originalFetch })
  let requested = ""
  let request: RequestInit | undefined
  globalThis.fetch = async (input, init) => {
    requested = String(input)
    request = init
    return jsonResponse({
      schema_version: 1,
      task: {task_id: "tr-1"},
      events: [],
    })
  }

  await retryTriageTask("tr-a/b")

  assert.equal(requested, "/api/triage-tasks/tr-a%2Fb/retry")
  assert.equal(request?.method, "POST")
  assert.deepEqual(JSON.parse(String(request?.body)), {})
})
