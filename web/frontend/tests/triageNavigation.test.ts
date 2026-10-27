import assert from "node:assert/strict"
import test from "node:test"

import { alertTaskDraft, draftFromNavigationState, taskHref } from "../src/lib/triageNavigation.ts"
import type { AlertDetailResponse } from "../src/types.ts"


const alert = {
  alertname: "NameNode Heap Usage",
  platform: "alpha",
  host: "node1",
  payload: {
    available: true,
    source_of: "active",
    data: {
      command: "rm -rf /not-allowed",
      credential: "secret",
    },
  },
  current: {
    triage: {
      playbook: "playbooks/hidden.sh",
      arguments: {"raw": true},
    },
  },
} as AlertDetailResponse


test("builds a manual draft without commands or payload data", () => {
  assert.deepEqual(alertTaskDraft(alert), {
    message: "排查告警：NameNode Heap Usage",
    platform: "alpha",
    node: "node1",
  })
})


test("drops unsupported platform values", () => {
  assert.deepEqual(alertTaskDraft({...alert, platform: "unknown"}), {
    message: "排查告警：NameNode Heap Usage",
    node: "node1",
  })
})


test("passes safe normalized alert context when opened from an alert", () => {
  const draft = alertTaskDraft({
    ...alert,
    source: "prometheus",
    key: "prometheus:alpha:NameNode Heap Usage:node1",
    description: "heap is above the warning threshold",
    current: {
      status: "active",
      severity: "warning",
      muted: false,
      service: "hdfs",
      first_seen: null,
      last_seen: null,
      occurrences: 1,
      class: "watch",
      effective_class: "watch",
      triage: null,
      source_active_at: "2026-09-03T00:00:00Z",
    },
  })

  assert.equal(draft.alert_context?.source, "prometheus")
  assert.equal(draft.alert_context?.alertname, "NameNode Heap Usage")
  assert.equal(draft.alert_context?.service, "hdfs")
  assert.equal(draft.alert_context?.raw_ref, "prometheus:alpha:NameNode Heap Usage:node1")
  assert.equal("credential" in (draft.alert_context?.labels || {}), false)
})


test("encodes an existing task link", () => {
  assert.equal(taskHref("tr-a1b2"), "/triage?task=tr-a1b2")
  assert.equal(taskHref("tr/a b"), "/triage?task=tr%2Fa+b")
})


test("keeps the work item a task is started from", () => {
  const draft = draftFromNavigationState({triageDraft: {
    message: "排查 node1 内存", work_item: {id: "wi-20261023-abcdef", title: "node1 内存治理"},
  }})
  assert.deepEqual(draft?.work_item, {id: "wi-20261023-abcdef", title: "node1 内存治理"})
  assert.equal(draftFromNavigationState({triageDraft: {message: "x", work_item: {id: 1}}})?.work_item, undefined)
})
