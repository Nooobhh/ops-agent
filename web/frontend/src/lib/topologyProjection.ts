import type { KnowledgeTopology, KnowledgeTopologyEdge, KnowledgeTopologyNode } from "../types"

export type TopologyProjectionCandidate = {
  source: string
  target: string
  relation: string
  hiddenNodeIds: string[]
  hiddenKinds: string[]
  pathRelations: string[]
  viaTaskIds?: string[]
}

export type TopologyProjectionIndex = {
  nodeById: Map<string, KnowledgeTopologyNode>
  incidentEdgesByNode: Map<string, KnowledgeTopologyEdge[]>
  projectionCandidates: TopologyProjectionCandidate[]
}

const DATA_RELATIONS = new Set(["reads_from", "writes_to", "uses_database"])
const PROJECTED_EXECUTION_RELATIONS = new Set(["handled_by", "calls", "submits_to"])
const LOCAL_JOIN_KINDS = new Set(["process", "handler", "platform_service", "runtime", "database", "table"])

function edgeKey(source: string, relation: string, target: string): string {
  return `${source}|${relation}|${target}`
}

function addIncident(
  index: Map<string, KnowledgeTopologyEdge[]>,
  edge: KnowledgeTopologyEdge,
) {
  const sourceEdges = index.get(edge.source) ?? []
  sourceEdges.push(edge)
  index.set(edge.source, sourceEdges)
  const targetEdges = index.get(edge.target) ?? []
  targetEdges.push(edge)
  index.set(edge.target, targetEdges)
}

function nodeKind(nodeById: Map<string, KnowledgeTopologyNode>, id: string): string {
  return nodeById.get(id)?.kind ?? "unknown"
}

function canonicalPair(left: string, right: string): [string, string] {
  return left.localeCompare(right) <= 0 ? [left, right] : [right, left]
}

export function buildTopologyProjectionIndex(graph: KnowledgeTopology): TopologyProjectionIndex {
  const nodeById = new Map(graph.nodes.map((node) => [node.id, node]))
  const incidentEdgesByNode = new Map<string, KnowledgeTopologyEdge[]>()
  const outgoingEdgesByNode = new Map<string, KnowledgeTopologyEdge[]>()
  for (const edge of graph.edges) {
    addIncident(incidentEdgesByNode, edge)
    const outgoing = outgoingEdgesByNode.get(edge.source) ?? []
    outgoing.push(edge)
    outgoingEdgesByNode.set(edge.source, outgoing)
  }

  const candidates: TopologyProjectionCandidate[] = []
  const candidateKeys = new Set<string>()
  const addCandidate = (
    source: string,
    target: string,
    relation: string,
    hiddenNodeIds: string[],
    pathRelations: string[],
    viaTaskIds: string[] = [],
  ) => {
    if (source === target || !nodeById.has(source) || !nodeById.has(target)) return
    const hiddenIds = [...new Set(hiddenNodeIds)].filter((id) => id !== source && id !== target)
    const key = edgeKey(source, relation, target)
    const candidateKey = `${key}|${hiddenIds.join(",")}`
    if (candidateKeys.has(candidateKey)) return
    candidateKeys.add(candidateKey)
    candidates.push({
      source,
      target,
      relation,
      hiddenNodeIds: hiddenIds,
      hiddenKinds: [...new Set(hiddenIds.map((id) => nodeKind(nodeById, id)))],
      pathRelations,
      ...(viaTaskIds.length ? { viaTaskIds: [...new Set(viaTaskIds)] } : {}),
    })
  }

  // Semantic projections rooted at Process. These are deliberately built from
  // canonical ownership/data edges instead of doing an unrestricted graph BFS:
  // shared Handler/Runtime nodes must not connect unrelated processes.
  for (const contains of graph.edges) {
    if (contains.relation !== "contains") continue
    if (nodeKind(nodeById, contains.source) !== "process") continue
    if (nodeKind(nodeById, contains.target) !== "task") continue
    const processId = contains.source
    const taskId = contains.target
    for (const taskEdge of outgoingEdgesByNode.get(taskId) ?? []) {
      const targetKind = nodeKind(nodeById, taskEdge.target)
      if (DATA_RELATIONS.has(taskEdge.relation) && ["table", "database"].includes(targetKind)) {
        addCandidate(
          processId,
          taskEdge.target,
          taskEdge.relation,
          [taskId],
          ["contains", taskEdge.relation],
          [taskId],
        )
      }
      if (taskEdge.relation !== "handled_by" || targetKind !== "handler") continue
      const handlerId = taskEdge.target
      addCandidate(processId, handlerId, "handled_by", [taskId], ["contains", "handled_by"], [taskId])
      for (const handlerEdge of outgoingEdgesByNode.get(handlerId) ?? []) {
        if (handlerEdge.relation !== "calls" || nodeKind(nodeById, handlerEdge.target) !== "platform_service") continue
        const serviceId = handlerEdge.target
        addCandidate(processId, serviceId, "calls", [taskId, handlerId], ["contains", "handled_by", "calls"], [taskId])
        for (const serviceEdge of outgoingEdgesByNode.get(serviceId) ?? []) {
          if (serviceEdge.relation !== "submits_to" || nodeKind(nodeById, serviceEdge.target) !== "runtime") continue
          addCandidate(
            processId,
            serviceEdge.target,
            "executes_via",
            [taskId, handlerId, serviceId],
            ["contains", "handled_by", "calls", "submits_to"],
            [taskId],
          )
        }
      }
    }
  }

  // A local task join supports arbitrary visible endpoint kinds such as
  // Handler + Table while the Task node is hidden. It only joins neighbours of
  // the same Task, never arbitrary paths through shared global hubs.
  for (const task of graph.nodes) {
    if (task.kind !== "task") continue
    const neighbours: Array<{ id: string; relation: string }> = []
    for (const edge of incidentEdgesByNode.get(task.id) ?? []) {
      const other = edge.source === task.id ? edge.target : edge.source
      const otherKind = nodeKind(nodeById, other)
      if (!LOCAL_JOIN_KINDS.has(otherKind)) continue
      if (edge.relation === "contains" && edge.target === task.id) {
        neighbours.push({ id: other, relation: edge.relation })
      } else if (edge.source === task.id && (DATA_RELATIONS.has(edge.relation) || PROJECTED_EXECUTION_RELATIONS.has(edge.relation))) {
        neighbours.push({ id: other, relation: edge.relation })
      }
    }
    for (let leftIndex = 0; leftIndex < neighbours.length; leftIndex += 1) {
      for (let rightIndex = leftIndex + 1; rightIndex < neighbours.length; rightIndex += 1) {
        const left = neighbours[leftIndex]
        const right = neighbours[rightIndex]
        const [source, target] = canonicalPair(left.id, right.id)
        // Process/data paths have a semantic relation and are already indexed.
        // Do not add a weaker duplicate for them.
        const semantic = DATA_RELATIONS.has(left.relation) || DATA_RELATIONS.has(right.relation)
        if (semantic && (nodeKind(nodeById, source) === "process" || nodeKind(nodeById, target) === "process")) continue
        addCandidate(
          source,
          target,
          "connected_via_task",
          [task.id],
          [left.relation, right.relation],
          [task.id],
        )
      }
    }
  }

  return { nodeById, incidentEdgesByNode, projectionCandidates: candidates }
}

function mergeProjectedEdge(
  target: Map<string, KnowledgeTopologyEdge>,
  candidate: TopologyProjectionCandidate,
) {
  const key = edgeKey(candidate.source, candidate.relation, candidate.target)
  const current = target.get(key)
  const candidateProperties = {
    projected: true,
    projection_type: "hidden_path",
    hidden_node_ids: candidate.hiddenNodeIds,
    hidden_kinds: candidate.hiddenKinds,
    path_relations: candidate.pathRelations,
    path_count: 1,
    ...(candidate.viaTaskIds?.length ? { via_task_ids: candidate.viaTaskIds } : {}),
  }
  if (!current) {
    target.set(key, {
      id: `projected:${key}`,
      source: candidate.source,
      target: candidate.target,
      relation: candidate.relation,
      properties: candidateProperties,
      provenance: [{ kind: "derived", path: "hidden topology path" }],
    })
    return
  }
  const properties = current.properties
  const hiddenNodeIds = new Set<string>([
    ...(Array.isArray(properties.hidden_node_ids) ? properties.hidden_node_ids.map(String) : []),
    ...candidate.hiddenNodeIds,
  ])
  const viaTaskIds = new Set<string>([
    ...(Array.isArray(properties.via_task_ids) ? properties.via_task_ids.map(String) : []),
    ...(candidate.viaTaskIds ?? []),
  ])
  properties.hidden_node_ids = [...hiddenNodeIds]
  properties.hidden_kinds = [...new Set([
    ...(Array.isArray(properties.hidden_kinds) ? properties.hidden_kinds.map(String) : []),
    ...candidate.hiddenKinds,
  ])]
  properties.path_count = Number(properties.path_count ?? 0) + 1
  if (viaTaskIds.size) properties.via_task_ids = [...viaTaskIds]
}

export function projectVisibleTopologyEdges(
  index: TopologyProjectionIndex,
  visibleIds: Set<string>,
  projectionVisibleIds: Set<string> = visibleIds,
): KnowledgeTopologyEdge[] {
  const direct: KnowledgeTopologyEdge[] = []
  const directKeys = new Set<string>()
  const seenEdges = new Set<string>()
  for (const nodeId of visibleIds) {
    for (const edge of index.incidentEdgesByNode.get(nodeId) ?? []) {
      if (!visibleIds.has(edge.source) || !visibleIds.has(edge.target)) continue
      if (seenEdges.has(edge.id)) continue
      seenEdges.add(edge.id)
      direct.push(edge)
      directKeys.add(edgeKey(edge.source, edge.relation, edge.target))
    }
  }

  const projected = new Map<string, KnowledgeTopologyEdge>()
  for (const candidate of index.projectionCandidates) {
    if (!visibleIds.has(candidate.source) || !visibleIds.has(candidate.target)) continue
    // This is the central visibility rule: a projection exists only while
    // every internal node of its path remains hidden from the current view.
    if (candidate.hiddenNodeIds.some((id) => projectionVisibleIds.has(id))) continue
    const key = edgeKey(candidate.source, candidate.relation, candidate.target)
    if (directKeys.has(key)) continue
    mergeProjectedEdge(projected, candidate)
  }
  return [...direct, ...projected.values()]
}
