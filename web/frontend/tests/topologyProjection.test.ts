import assert from "node:assert/strict"
import test from "node:test"

import {
  buildTopologyProjectionIndex,
  projectVisibleTopologyEdges,
} from "../src/lib/topologyProjection.ts"
import type { KnowledgeTopology, KnowledgeTopologyEdge, KnowledgeTopologyNode } from "../src/types.ts"

function node(id: string, kind: string, name = id): KnowledgeTopologyNode {
  return { id, kind, name, platform: "alpha", properties: {}, provenance: [] }
}

function edge(source: string, target: string, relation: string): KnowledgeTopologyEdge {
  return { id: `${source}|${relation}|${target}`, source, target, relation, properties: {}, provenance: [] }
}

function graph(nodes: KnowledgeTopologyNode[], edges: KnowledgeTopologyEdge[]): KnowledgeTopology {
  return {
    schema_version: 1,
    topology_kind: "jobs",
    platform: "alpha",
    source_fingerprint: "test",
    nodes,
    edges,
    warnings: [],
    coverage: {},
    stats: {
      node_count: nodes.length,
      edge_count: edges.length,
      node_kinds: {},
      edge_relations: {},
      platforms: ["alpha"],
      clusters: 0,
      services: 0,
      warning_count: 0,
    },
  }
}

test("projects Process to Table only while Task is hidden", () => {
  const processId = "process:alpha:1"
  const taskId = "task:alpha:1:10"
  const tableId = "table:alpha:source"
  const topology = graph(
    [node(processId, "process"), node(taskId, "task"), node(tableId, "table")],
    [edge(processId, taskId, "contains"), edge(taskId, tableId, "reads_from")],
  )
  const index = buildTopologyProjectionIndex(topology)

  const projected = projectVisibleTopologyEdges(index, new Set([processId, tableId]))
  assert.deepEqual(projected.map((item) => [item.source, item.relation, item.target]), [
    [processId, "reads_from", tableId],
  ])
  assert.equal(projected[0].properties.projected, true)
  assert.deepEqual(projected[0].properties.hidden_node_ids, [taskId])
})

test("suppresses Process to Table projection when Task is visible", () => {
  const processId = "process:alpha:1"
  const taskId = "task:alpha:1:10"
  const tableId = "table:alpha:source"
  const topology = graph(
    [node(processId, "process"), node(taskId, "task"), node(tableId, "table")],
    [edge(processId, taskId, "contains"), edge(taskId, tableId, "reads_from")],
  )
  const view = projectVisibleTopologyEdges(buildTopologyProjectionIndex(topology), new Set([processId, taskId, tableId]))
  assert.deepEqual(view.map((item) => [item.source, item.relation, item.target]), [
    [processId, "contains", taskId],
    [taskId, "reads_from", tableId],
  ])
})

test("aggregates multiple hidden Task paths into one projection edge", () => {
  const processId = "process:alpha:1"
  const taskA = "task:alpha:1:10"
  const taskB = "task:alpha:1:11"
  const tableId = "table:alpha:source"
  const topology = graph(
    [node(processId, "process"), node(taskA, "task"), node(taskB, "task"), node(tableId, "table")],
    [
      edge(processId, taskA, "contains"), edge(processId, taskB, "contains"),
      edge(taskA, tableId, "reads_from"), edge(taskB, tableId, "reads_from"),
    ],
  )
  const view = projectVisibleTopologyEdges(buildTopologyProjectionIndex(topology), new Set([processId, tableId]))
  assert.equal(view.length, 1)
  assert.equal(view[0].properties.path_count, 2)
  assert.deepEqual(view[0].properties.via_task_ids, [taskA, taskB])
})

test("keeps direct edges and avoids a duplicate projection", () => {
  const processId = "process:alpha:1"
  const taskId = "task:alpha:1:10"
  const tableId = "table:alpha:source"
  const topology = graph(
    [node(processId, "process"), node(taskId, "task"), node(tableId, "table")],
    [
      edge(processId, taskId, "contains"), edge(taskId, tableId, "reads_from"),
      edge(processId, tableId, "reads_from"),
    ],
  )
  const view = projectVisibleTopologyEdges(buildTopologyProjectionIndex(topology), new Set([processId, tableId]))
  assert.equal(view.length, 1)
  assert.equal(view[0].properties.projected, undefined)
})

test("projects a local Handler to Table relationship without crossing shared handlers", () => {
  const processA = "process:alpha:a"
  const processB = "process:alpha:b"
  const taskA = "task:alpha:a:1"
  const taskB = "task:alpha:b:2"
  const handler = "handler:jobs:sparksql"
  const tableB = "table:alpha:b"
  const topology = graph(
    [
      node(processA, "process"), node(processB, "process"), node(taskA, "task"), node(taskB, "task"),
      node(handler, "handler"), node(tableB, "table"),
    ],
    [
      edge(processA, taskA, "contains"), edge(processB, taskB, "contains"),
      edge(taskA, handler, "handled_by"), edge(taskB, handler, "handled_by"),
      edge(taskB, tableB, "reads_from"),
    ],
  )
  const view = projectVisibleTopologyEdges(buildTopologyProjectionIndex(topology), new Set([handler, tableB]))
  assert.deepEqual(view.map((item) => [item.source, item.relation, item.target]), [[
    handler < tableB ? handler : tableB,
    "connected_via_task",
    handler < tableB ? tableB : handler,
  ]])
})
