import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react"
import type ForceGraph from "force-graph"
import type { LinkObject, NodeObject } from "force-graph"
import {
  BookOpen, ChevronDown, ChevronRight, Eye, EyeOff, Filter,
  RefreshCw, RotateCcw, Search, X, ZoomIn, ZoomOut,
} from "lucide-react"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { fetchJson, formatTime, postJson } from "@/lib/api"
import { componentLabel, isVerified, platformKeys, platformLabel, serviceLabel } from "@/lib/knowledgeView"
import {
  buildTopologyProjectionIndex,
  projectVisibleTopologyEdges,
  type TopologyProjectionIndex,
} from "@/lib/topologyProjection"
import type {
  KnowledgeDoc, KnowledgeTopology, KnowledgeTopologyEdge, KnowledgeTopologyNode,
} from "@/types"

const VIEWBOX_WIDTH = 1200
const VIEWBOX_HEIGHT = 720
const CORE_KINDS = ["platform", "cluster", "node", "service", "component", "endpoint"]
const DEFAULT_VISIBLE_KINDS = ["platform", "cluster", "node", "service", "endpoint"]
const OPTIONAL_KINDS = ["index", "document", "alert", "playbook"]
const KIND_LABELS: Record<string, string> = {
  platform: "平台", cluster: "集群", node: "节点", service: "服务", component: "组件",
  endpoint: "Endpoint", index: "索引", document: "文档", alert: "告警", playbook: "Playbook",
  process: "Process", task: "Task", control: "流程控制", handler: "Handler",
  platform_service: "平台服务", runtime: "执行引擎", database: "数据源", table: "数据表",
  dependency_target: "依赖条件", execution_node: "执行节点",
}
const NODE_COLORS: Record<string, { fill: string; stroke: string }> = {
  platform: { fill: "#f59e0b", stroke: "#fde68a" },
  cluster: { fill: "#38bdf8", stroke: "#bae6fd" },
  service: { fill: "#8b5cf6", stroke: "#ddd6fe" },
  component: { fill: "#64748b", stroke: "#cbd5e1" },
  endpoint: { fill: "#14b8a6", stroke: "#99f6e4" },
  alert: { fill: "#ef4444", stroke: "#fecaca" },
  document: { fill: "#22c55e", stroke: "#bbf7d0" },
  index: { fill: "#94a3b8", stroke: "#e2e8f0" },
  process: { fill: "#2563eb", stroke: "#bfdbfe" },
  task: { fill: "#8b5cf6", stroke: "#ddd6fe" },
  control: { fill: "#64748b", stroke: "#cbd5e1" },
  handler: { fill: "#db2777", stroke: "#fbcfe8" },
  platform_service: { fill: "#0891b2", stroke: "#a5f3fc" },
  runtime: { fill: "#16a34a", stroke: "#bbf7d0" },
  database: { fill: "#ea580c", stroke: "#fed7aa" },
  table: { fill: "#ca8a04", stroke: "#fef08a" },
  dependency_target: { fill: "#dc2626", stroke: "#fecaca" },
  execution_node: { fill: "#475569", stroke: "#cbd5e1" },
}

type Point = { x: number; y: number }
type FilterKind = "all" | string
type RelationFilter = "all" | "depends_on"
type CanvasTheme = "light" | "dark"
type TopologyForceNode = NodeObject & {
  id: string
  name: string
  hoverLabel: string
  kind: string
  platform?: string
  degree: number
}
type TopologyForceLink = LinkObject<TopologyForceNode> & {
  id: string
  relation: string
  projected?: boolean
}
type TopologyForceGraph = ForceGraph<TopologyForceNode, TopologyForceLink>
type TopologyForceData = { nodes: TopologyForceNode[]; links: TopologyForceLink[] }
type TopologyCluster = {
  id: string
  nodes: KnowledgeTopologyNode[]
  edgeCount: number
  kindCounts: Record<string, number>
  relationCounts: Record<string, number>
}
const MAX_TOPOLOGY_VIEW_CACHE = 3
const SEARCH_DEBOUNCE_MS = 180

function stringValue(value: unknown): string {
  if (value === null || value === undefined) return ""
  return String(value)
}

function propertyText(value: unknown): string {
  if (Array.isArray(value)) return value.map(propertyText).join("、")
  if (value && typeof value === "object") return JSON.stringify(value, null, 2)
  return stringValue(value)
}

function nodeLabel(node: KnowledgeTopologyNode): string {
  if (node.kind === "service") return serviceLabel(node.name)
  if (node.kind === "component") return componentLabel(node.name)
  if (node.platform && node.kind === "platform") return platformLabel(node.name)
  return node.name
}

function nodeIdentity(node: KnowledgeTopologyNode): string | null {
  if (node.kind === "process") {
    const pid = stringValue(node.properties.pid) || node.id.split(":").pop() || ""
    return pid ? `PID: ${pid}` : null
  }
  if (node.kind === "task") {
    const tid = stringValue(node.properties.tid || node.properties.taskId) || node.id.split(":").pop() || ""
    return tid ? `TID: ${tid}` : null
  }
  return null
}

function nodeHoverLabel(node: KnowledgeTopologyNode): string {
  const identity = nodeIdentity(node)
  return identity ? `${identity} · ${nodeLabel(node)}` : nodeLabel(node)
}

function nodeSearchText(node: KnowledgeTopologyNode): string {
  return [node.name, node.kind, node.platform, JSON.stringify(node.properties)].join(" ").toLowerCase()
}

function nodeRadiusForKind(kind: string, degree: number, emphasized: boolean): number {
  const base = kind === "platform" ? 8.5 : kind === "cluster" ? 7.5 : kind === "service" ? 6.2 : kind === "node" ? 8 : kind === "process" ? 8.2 : kind === "task" ? 5.3 : kind === "handler" ? 6.2 : kind === "platform_service" ? 6.4 : kind === "runtime" ? 5.8 : kind === "database" ? 5.8 : kind === "table" ? 3.2 : kind === "control" ? 3.8 : kind === "component" ? 3.6 : 4.8
  const size = degree > 0 ? Math.min(10, Math.sqrt(degree) * 0.85) : 2.5
  return (emphasized ? 1.45 : 1) * (base + size)
}

type CompactForce = ((alpha: number) => void) & {
  initialize?: (nodes: TopologyForceNode[]) => void
}

function linkEndId(value: string | number | TopologyForceNode | undefined): string {
  return value && typeof value === "object" ? value.id : value === undefined ? "" : String(value)
}

function createCompactComponentForce(
  dataRef: { current: { nodes: TopologyForceNode[]; links: TopologyForceLink[] } },
): CompactForce {
  let nodes: TopologyForceNode[] = []
  let targets = new Map<string, Point>()
  const force = ((alpha: number) => {
    const strength = dataRef.current.links.length ? 0.2 : 0.3
    for (const node of nodes) {
      const target = targets.get(node.id) ?? { x: 0, y: 0 }
      node.vx = (node.vx ?? 0) + (target.x - (node.x ?? 0)) * strength * alpha
      node.vy = (node.vy ?? 0) + (target.y - (node.y ?? 0)) * strength * alpha
    }
  }) as CompactForce
  force.initialize = (nextNodes) => {
    nodes = nextNodes
    const adjacency = new Map<string, Set<string>>()
    for (const node of nodes) adjacency.set(node.id, new Set())
    for (const link of dataRef.current.links) {
      const source = linkEndId(link.source); const target = linkEndId(link.target)
      if (!adjacency.has(source) || !adjacency.has(target)) continue
      adjacency.get(source)?.add(target); adjacency.get(target)?.add(source)
    }

    const components: string[][] = []
    const visited = new Set<string>()
    for (const node of nodes) {
      if (visited.has(node.id)) continue
      const component: string[] = []; const queue = [node.id]; visited.add(node.id)
      while (queue.length) {
        const current = queue.shift()
        if (!current) continue
        component.push(current)
        for (const neighbour of adjacency.get(current) ?? []) {
          if (!visited.has(neighbour)) { visited.add(neighbour); queue.push(neighbour) }
        }
      }
      components.push(component)
    }

    // Keep disconnected clusters close enough to compare. For a kind-only
    // filter with no links, use a compact grid rather than a huge force cloud.
    const columns = Math.max(1, Math.ceil(Math.sqrt(components.length)))
    const spacing = components.length <= 4 ? 96 : dataRef.current.links.length ? 64 : 54
    const rows = Math.ceil(components.length / columns)
    targets = new Map()
    components.forEach((component, index) => {
      const column = index % columns; const row = Math.floor(index / columns)
      const center = { x: (column - (columns - 1) / 2) * spacing, y: (row - (rows - 1) / 2) * spacing }
      for (const id of component) targets.set(id, center)
    })
  }
  return force
}

function relationLabel(relation: string): string {
  return {
    contains_cluster: "包含集群", contains_node: "包含节点", contains_service: "包含服务",
    contains_endpoint: "包含 endpoint", hosts_component: "承载组件", component_of: "组件属于",
    hosts_service: "节点承载服务",
    depends_on: "依赖", exposes_endpoint: "暴露 endpoint", served_by: "由节点提供",
    documents: "知识文档", handles_alert: "处理告警", related_to: "相关知识", uses_playbook: "使用 playbook",
    uses_service: "配置引用", routes_to: "路由到", observes: "监控采集", audits_to: "审计到",
    contains: "包含", next: "下一步", handled_by: "由 Handler 执行", calls: "调用",
    submits_to: "提交到", reads_from: "读取", writes_to: "写入", connected_via_task: "经 Task 关联", waits_for: "等待",
    runs_on: "运行于", uses_database: "使用数据源",
  }[relation] ?? relation
}

function relatedDocumentPaths(node: KnowledgeTopologyNode, graph: KnowledgeTopology): string[] {
  const nodes = new Map(graph.nodes.map((item) => [item.id, item]))
  const paths = new Set<string>()
  const addDocumentNode = (candidate: KnowledgeTopologyNode | undefined) => {
    if (!candidate || !["document", "index"].includes(candidate.kind)) return
    const path = stringValue(candidate.properties.path)
    if (path.endsWith(".md")) paths.add(path)
  }
  const addProvenance = (candidate: KnowledgeTopologyNode | undefined) => {
    for (const item of candidate?.provenance ?? []) {
      if (item.kind === "knowledge" && item.path.endsWith(".md")) paths.add(item.path)
    }
  }
  const addServiceDocs = (serviceId: string) => {
    for (const edge of graph.edges) {
      if (edge.relation === "documents" && edge.target === serviceId) addDocumentNode(nodes.get(edge.source))
    }
  }

  addDocumentNode(node); addProvenance(node)
  if (node.kind === "service") addServiceDocs(node.id)
  if (node.kind === "component") {
    for (const edge of graph.edges) {
      if (edge.relation === "component_of" && edge.source === node.id) addServiceDocs(edge.target)
    }
  }
  if (node.kind === "node") {
    for (const edge of graph.edges) {
      if (edge.relation !== "hosts_component" || edge.source !== node.id) continue
      for (const serviceEdge of graph.edges) {
        if (serviceEdge.relation === "component_of" && serviceEdge.source === edge.target) addServiceDocs(serviceEdge.target)
      }
    }
  }
  if (node.kind === "alert") {
    for (const edge of graph.edges) if (edge.relation === "handles_alert" && edge.target === node.id) addDocumentNode(nodes.get(edge.source))
  }
  if (node.kind === "endpoint") {
    for (const edge of graph.edges) if (edge.relation === "exposes_endpoint" && edge.target === node.id) addServiceDocs(edge.source)
  }
  if (node.kind === "playbook") {
    for (const edge of graph.edges) if (edge.relation === "uses_playbook" && edge.target === node.id) addDocumentNode(nodes.get(edge.source))
  }
  return [...paths].sort()
}

function SummaryCard({ label, value, active, onClick }: {
  label: string
  value: number
  active: boolean
  onClick: () => void
}) {
  return (
    <button
      aria-pressed={active}
      className={`topology-summary-card ${active ? "is-active" : ""}`}
      onClick={onClick}
      type="button"
    >
      <span>{label}</span><strong>{value}</strong>
    </button>
  )
}

function buildTopologyClusters(
  nodes: KnowledgeTopologyNode[],
  edges: KnowledgeTopologyEdge[],
): TopologyCluster[] {
  if (!nodes.length || !edges.length) return []
  const nodeById = new Map(nodes.map((node) => [node.id, node]))
  const adjacency = new Map<string, Set<string>>()
  for (const node of nodes) adjacency.set(node.id, new Set())
  for (const edge of edges) {
    if (!nodeById.has(edge.source) || !nodeById.has(edge.target)) continue
    adjacency.get(edge.source)?.add(edge.target)
    adjacency.get(edge.target)?.add(edge.source)
  }

  const visited = new Set<string>()
  const clusters: TopologyCluster[] = []
  for (const node of nodes) {
    if (visited.has(node.id)) continue
    const ids: string[] = []
    const queue = [node.id]
    visited.add(node.id)
    for (let index = 0; index < queue.length; index += 1) {
      const current = queue[index]
      ids.push(current)
      for (const neighbour of adjacency.get(current) ?? []) {
        if (visited.has(neighbour)) continue
        visited.add(neighbour)
        queue.push(neighbour)
      }
    }
    clusters.push({
      id: `cluster:${[...ids].sort()[0]}`,
      nodes: ids.map((id) => nodeById.get(id)).filter((item): item is KnowledgeTopologyNode => Boolean(item)),
      edgeCount: 0,
      kindCounts: {},
      relationCounts: {},
    })
  }

  const componentByNode = new Map<string, TopologyCluster>()
  for (const cluster of clusters) {
    for (const item of cluster.nodes) {
      componentByNode.set(item.id, cluster)
      cluster.kindCounts[item.kind] = (cluster.kindCounts[item.kind] ?? 0) + 1
    }
  }
  for (const edge of edges) {
    const sourceCluster = componentByNode.get(edge.source)
    if (!sourceCluster || componentByNode.get(edge.target) !== sourceCluster) continue
    sourceCluster.edgeCount += 1
    sourceCluster.relationCounts[edge.relation] = (sourceCluster.relationCounts[edge.relation] ?? 0) + 1
  }
  return clusters
    .filter((cluster) => cluster.nodes.length >= 3 || cluster.edgeCount >= 2)
    .sort((left, right) => (
      right.nodes.length - left.nodes.length || right.edgeCount - left.edgeCount
    ))
}

function visualEdges(
  graph: KnowledgeTopology,
  edges: KnowledgeTopologyEdge[],
  visibleIds: Set<string>,
  componentsShown: boolean,
): KnowledgeTopologyEdge[] {
  if (componentsShown) return edges

  // Keep node↔service context visible while component dots are hidden by
  // default. The canonical graph remains unchanged; this is only a view edge.
  const componentHosts = graph.edges.filter((edge) => edge.relation === "hosts_component")
  const componentServices = graph.edges.filter((edge) => edge.relation === "component_of")
  const bridges: KnowledgeTopologyEdge[] = []
  for (const host of componentHosts) {
    for (const component of componentServices) {
      if (host.target !== component.source || !visibleIds.has(host.source) || !visibleIds.has(component.target)) continue
      bridges.push({
        id: `bridge-${host.source}-${component.target}`,
        source: host.source,
        target: component.target,
        relation: "hosts_service",
        properties: { via: component.source },
        provenance: [],
      })
    }
  }
  const existing = new Set(edges.map((edge) => `${edge.source}|${edge.target}|${edge.relation}`))
  return [...edges, ...bridges.filter((edge) => !existing.has(`${edge.source}|${edge.target}|${edge.relation}`))]
}

function ToggleRow({ label, checked, onChange }: { label: string; checked: boolean; onChange: () => void }) {
  return (
    <button aria-pressed={checked} className="topology-toggle-row" onClick={onChange} type="button">
      {checked ? <Eye className="size-3.5" /> : <EyeOff className="size-3.5" />}
      <span>{label}</span>
    </button>
  )
}

function TopologyClusterPanel({
  clusters, onHoverNode, onSelectNode,
}: {
  clusters: TopologyCluster[]
  onHoverNode: (node: KnowledgeTopologyNode | null) => void
  onSelectNode: (node: KnowledgeTopologyNode) => void
}) {
  const processClusters = clusters.filter((cluster) => cluster.nodes.some((node) => node.kind === "process"))
  const visibleClusters = processClusters.slice(0, 10)
  if (!visibleClusters.length) return null

  return (
    <aside className="topology-cluster-panel" aria-label="当前拓扑 Process 集群摘要">
      <div className="topology-cluster-header">
        <div><p className="topology-eyebrow">Clusters</p><strong>Process 集群</strong></div>
        <span>Top {visibleClusters.length} / {processClusters.length}</span>
      </div>
      <div className="topology-cluster-scroll">
        {visibleClusters.map((cluster, index) => {
          const processNodes = cluster.nodes
            .filter((node) => node.kind === "process")
            .sort((left, right) => nodeLabel(left).localeCompare(nodeLabel(right), "zh-CN"))
          return (
            <details className="topology-cluster-card" key={cluster.id} open={index === 0}>
              <summary>
                <span>集群 {index + 1}</span>
                <span>{processNodes.length} 个 Process</span>
              </summary>
              <div className="topology-cluster-body">
                <div className="topology-cluster-members">
                  {processNodes.map((process) => (
                    <button
                      className="topology-cluster-member topology-cluster-process-button"
                      key={process.id}
                      onClick={() => onSelectNode(process)}
                      onMouseEnter={() => onHoverNode(process)}
                      onMouseLeave={() => onHoverNode(null)}
                      type="button"
                    >
                      <span className="topology-cluster-member-kind">Process</span>
                      <span className="truncate">{nodeLabel(process)}</span>
                    </button>
                  ))}
                </div>
              </div>
            </details>
          )
        })}
      </div>
    </aside>
  )
}

function NodeDetails({
  node, graph, viewEdges, side, onClose, onOpenDocument,
}: {
  node: KnowledgeTopologyNode
  graph: KnowledgeTopology
  viewEdges: KnowledgeTopologyEdge[]
  side: "left" | "right" | "left-adjacent"
  onClose: () => void
  onOpenDocument: (path: string) => void
}) {
  const paths = useMemo(() => relatedDocumentPaths(node, graph), [graph, node])
  const nodeById = useMemo(() => new Map(graph.nodes.map((candidate) => [candidate.id, candidate])), [graph.nodes])
  const [previewPath, setPreviewPath] = useState<string | null>(paths[0] ?? null)
  const [preview, setPreview] = useState<KnowledgeDoc | null>(null)
  const [previewError, setPreviewError] = useState<string | null>(null)

  useEffect(() => setPreviewPath(paths[0] ?? null), [node.id, paths])
  useEffect(() => {
    if (!previewPath) { setPreview(null); return undefined }
    const controller = new AbortController()
    setPreviewError(null)
    fetchJson<KnowledgeDoc>(`/api/knowledge/doc?path=${encodeURIComponent(previewPath)}`, controller.signal)
      .then(setPreview)
      .catch((error: Error) => { if (error.name !== "AbortError") setPreviewError(error.message) })
    return () => controller.abort()
  }, [previewPath])

  const properties = Object.entries(node.properties).filter(([key, value]) => {
    if (value === null || value === undefined || value === "") return false
    if (Array.isArray(value) && value.length === 0) return false
    return !["observed_state", "baseline_configs"].includes(key)
  })
  const incoming = viewEdges.filter((edge) => edge.target === node.id)
  const outgoing = viewEdges.filter((edge) => edge.source === node.id)
  const neighbours = (edges: KnowledgeTopologyEdge[]) => edges.flatMap((edge) => {
    const other = nodeById.get(edge.source === node.id ? edge.target : edge.source)
    if (node.kind === "process" && other?.kind === "control") {
      const controlType = stringValue(other.properties.control_type).toUpperCase()
      if (controlType === "START" || controlType === "END") return []
    }
    return [other ? `${relationLabel(edge.relation)} · ${nodeHoverLabel(other)}` : relationLabel(edge.relation)]
  })
  const baselineConfigs = Array.isArray(node.properties.baseline_configs)
    ? node.properties.baseline_configs as Array<Record<string, unknown>> : []

  return (
    <aside className={`topology-details-panel ${side === "right" ? "topology-details-panel-right" : side === "left-adjacent" ? "topology-details-panel-left-adjacent" : ""}`}>
      <div className="topology-panel-header">
        <div className="min-w-0">
          <p className="topology-eyebrow">{KIND_LABELS[node.kind] ?? node.kind}</p>
          <h3 className="truncate">{nodeLabel(node)}</h3>
          {nodeIdentity(node) ? <p className="topology-panel-identity">{nodeIdentity(node)}</p> : null}
          <p className="topology-panel-subtitle">{node.platform ? platformLabel(node.platform) : "跨平台"}</p>
        </div>
        <Button aria-label="关闭节点详情" className="size-7 shrink-0" onClick={onClose} size="icon" variant="ghost">
          <X className="size-4" />
        </Button>
      </div>
      <div className="topology-details-scroll">
        <div className="topology-node-badges">
          <Badge variant="secondary">{KIND_LABELS[node.kind] ?? node.kind}</Badge>
          {node.platform ? <Badge variant="outline">{node.platform}</Badge> : null}
        </div>
        {properties.length ? (
          <dl className="topology-property-list">
            {properties.slice(0, 12).map(([key, value]) => (
              <div key={key}><dt>{key}</dt><dd>{propertyText(value)}</dd></div>
            ))}
          </dl>
        ) : null}
        {baselineConfigs.length ? (
          <section className="topology-detail-section">
            <h4>关键配置</h4>
            <ul>{baselineConfigs.slice(0, 12).map((item, index) => (
              <li key={`${stringValue(item.key)}-${index}`}><code>{stringValue(item.file)} / {stringValue(item.key)}</code><span>{stringValue(item.value)}</span></li>
            ))}</ul>
          </section>
        ) : null}
        {(incoming.length || outgoing.length) ? (
          <section className="topology-detail-section">
            <h4>关系</h4>
            <ul>{[...neighbours(outgoing), ...neighbours(incoming)].slice(0, 16).map((item, index) => <li key={`${item}-${index}`}>{item}</li>)}</ul>
          </section>
        ) : null}
        <section className="topology-detail-section">
          <h4><BookOpen className="mr-1 inline size-3.5" />知识库条目 {paths.length ? `(${paths.length})` : ""}</h4>
          {paths.length ? (
            <div className="topology-document-list">
              {paths.slice(0, 12).map((path) => (
                <button className={`topology-document-link ${path === previewPath ? "is-selected" : ""}`} key={path} onClick={() => setPreviewPath(path)} type="button">
                  <span className="truncate">{path.split("/").pop()}</span>
                  <span className="topology-document-open" onClick={(event) => { event.stopPropagation(); onOpenDocument(path) }}>打开</span>
                </button>
              ))}
            </div>
          ) : <p className="topology-muted">暂无关联的知识库条目</p>}
        </section>
        {previewPath ? (
          <section className="topology-preview-section">
            {previewError ? <p className="text-xs text-destructive">{previewError}</p> : preview ? (
              <>
                <div className="flex items-center justify-between gap-2">
                  <h4 className="truncate">{preview.meta.display_title}</h4>
                  {preview.meta.status ? <Badge variant={isVerified(preview.meta.status) ? "default" : "outline"}>{isVerified(preview.meta.status) ? "已验证" : "草稿"}</Badge> : null}
                </div>
                <p className="mt-2 whitespace-pre-wrap break-words text-xs leading-5 text-foreground/80">{preview.meta.summary || preview.markdown.replace(/^#+\s+/gm, "").slice(0, 620)}</p>
                <Button className="mt-3 w-full" onClick={() => onOpenDocument(previewPath)} size="sm" variant="outline">打开完整知识条目</Button>
              </>
            ) : <Skeleton className="h-20 w-full" />}
          </section>
        ) : null}
      </div>
    </aside>
  )
}

// graph:环境包图谱(lib/graphs),节点种类、默认可见与摘要卡由图的 view 段给
type TopologyVariant = "knowledge" | "graph"

type TopologyRefreshStatus = {
  running: boolean
  state?: string
  mode?: string | null
  started_at?: string | null
  completed_at?: string | null
  last_error?: string | null
  graph?: { generated_at?: string | null; change_summary?: Record<string, number> | null } | null
}

function TopologyLoading({ title }: { title: string }) {
  const messages = [`正在读取${title}…`, "正在准备节点和关系…"]
  const [index, setIndex] = useState(0)

  useEffect(() => {
    const timer = window.setInterval(() => setIndex((value) => (value + 1) % messages.length), 1600)
    return () => window.clearInterval(timer)
  }, [messages.length])

  return (
    <section className="knowledge-topology-loading" aria-live="polite" aria-label={`${title}加载中`}>
      <div className="flex items-center gap-3 rounded-md border bg-background p-4">
        <RefreshCw className="size-5 animate-spin text-[var(--link)]" />
        <div>
          <p className="font-medium">{title}加载中</p>
          <p className="mt-1 text-xs text-muted-foreground">{messages[index]} 大型拓扑首次加载可能需要几秒。</p>
        </div>
      </div>
      <Skeleton className="h-[520px] w-full" />
    </section>
  )
}

export function KnowledgeTopology({
  onOpenDocument,
  endpoint = "/api/knowledge/topology",
  variant = "knowledge",
  title = "知识库拓扑",
}: {
  onOpenDocument: (path: string) => void
  endpoint?: string
  variant?: TopologyVariant
  title?: string
}) {
  const isGraph = variant === "graph"
  const [graph, setGraph] = useState<KnowledgeTopology | null>(null)
  const view = isGraph ? graph?.view : undefined
  const coreKinds = isGraph ? view?.core_kinds ?? [] : CORE_KINDS
  const optionalKinds = isGraph ? view?.optional_kinds ?? [] : OPTIONAL_KINDS
  const defaultVisibleKinds = isGraph ? view?.default_visible_kinds ?? [] : DEFAULT_VISIBLE_KINDS
  const primaryKind = isGraph ? view?.count_kind ?? "process" : "service"
  const [error, setError] = useState<string | null>(null)
  const [platform, setPlatform] = useState<string>("all")
  const [queryInput, setQueryInput] = useState("")
  const [query, setQuery] = useState("")
  const [kindFilter, setKindFilter] = useState<FilterKind>("all")
  const [relationFilter, setRelationFilter] = useState<RelationFilter>("all")
  const [shownKinds, setShownKinds] = useState<Set<string>>(() => new Set(defaultVisibleKinds))
  const [showLabels, setShowLabels] = useState(false)
  const [showFilterPanel, setShowFilterPanel] = useState(true)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [hoveredId, setHoveredId] = useState<string | null>(null)
  const [zoom, setZoom] = useState(0.86)
  const [theme, setTheme] = useState<CanvasTheme>(() => (
    typeof document !== "undefined" && document.documentElement.dataset.theme === "dark" ? "dark" : "light"
  ))
  const graphHostRef = useRef<HTMLDivElement>(null)
  const forceGraphRef = useRef<TopologyForceGraph | null>(null)
  const forceDataRef = useRef<TopologyForceData>({ nodes: [], links: [] })
  const forceNodeCacheRef = useRef<Map<string, TopologyForceNode>>(new Map())
  const viewCacheRef = useRef<Map<string, TopologyForceData>>(new Map())
  const appliedForceDataRef = useRef<TopologyForceData | null>(null)
  const adjacencyRef = useRef<Map<string, Set<string>>>(new Map())
  const hoveredIdRef = useRef<string | null>(null)
  const clusterHoveredIdRef = useRef<string | null>(null)
  const selectedIdRef = useRef<string | null>(null)
  const selectedFocusEnabledRef = useRef(false)
  const showLabelsRef = useRef(false)
  const themeRef = useRef<CanvasTheme>(theme)
  const refreshGraphRef = useRef<() => void>(() => undefined)
  const zoomRef = useRef(0.86)
  const zoomPaintRef = useRef<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [refreshStatus, setRefreshStatus] = useState<TopologyRefreshStatus | null>(null)

  hoveredIdRef.current = hoveredId
  selectedIdRef.current = selectedId
  const graphNodeById = useMemo(
    () => new Map((graph?.nodes ?? []).map((node) => [node.id, node])),
    [graph],
  )
  selectedFocusEnabledRef.current = Boolean(
    isGraph && selectedId && graphNodeById.has(selectedId),
  )
  showLabelsRef.current = showLabels
  themeRef.current = theme

  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(queryInput.trim().toLowerCase()), SEARCH_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [queryInput])

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    fetchJson<KnowledgeTopology>(endpoint, controller.signal)
      .then((payload) => {
        setGraph(payload); setError(null)
      })
      .catch((reason: Error) => { if (reason.name !== "AbortError") setError(reason.message) })
      .finally(() => setLoading(false))
    return () => controller.abort()
  }, [endpoint])

  useEffect(() => {
    const root = document.documentElement
    const updateTheme = () => setTheme(root.dataset.theme === "dark" ? "dark" : "light")
    const observer = new MutationObserver(updateTheme)
    observer.observe(root, { attributes: true, attributeFilter: ["data-theme"] })
    updateTheme()
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    // Keep the page stationary while force-graph consumes wheel events for
    // zooming. The listener is non-passive for embedded browser hosts.
    const preventGraphPageScroll = (event: WheelEvent) => {
      const target = event.target
      if (!(target instanceof Element) || !target.closest(".topology-canvas")) return
      // Detail/filter content owns its own scroll area. Preventing the wheel
      // event here would make the hidden-scrollbar panels impossible to use.
      if (target.closest(".topology-details-scroll, .topology-filter-scroll, .topology-cluster-scroll")) return
      event.preventDefault()
    }
    document.addEventListener("wheel", preventGraphPageScroll, { capture: true, passive: false })
    return () => document.removeEventListener("wheel", preventGraphPageScroll, { capture: true })
  }, [])

  const topologyIndex = useMemo<TopologyProjectionIndex | null>(
    () => graph ? buildTopologyProjectionIndex(graph) : null,
    [graph],
  )
  const searchTextById = useMemo(
    () => new Map((graph?.nodes ?? []).map((node) => [node.id, nodeSearchText(node)])),
    [graph],
  )

  const filteredBaseNodes = useMemo(() => {
    if (!graph) return []
    return graph.nodes.filter((node) => {
      if (!shownKinds.has(node.kind)) return false
      if (kindFilter !== "all" && node.kind !== kindFilter) return false
      if (platform !== "all" && node.platform !== platform) return false
      return !query || searchTextById.get(node.id)?.includes(query)
    })
  }, [graph, kindFilter, platform, query, searchTextById, shownKinds])

  const baseVisibleIds = useMemo(
    () => new Set(filteredBaseNodes.map((node) => node.id)),
    [filteredBaseNodes],
  )
  const candidateViewEdges = useMemo(
    () => topologyIndex ? projectVisibleTopologyEdges(topologyIndex, baseVisibleIds) : [],
    [baseVisibleIds, topologyIndex],
  )

  const relationFilteredNodes = useMemo(() => {
    if (!graph) return []
    if (relationFilter === "all") return filteredBaseNodes
    const connectedIds = new Set<string>()
    for (const edge of candidateViewEdges) {
      if (edge.relation === relationFilter && baseVisibleIds.has(edge.source) && baseVisibleIds.has(edge.target)) {
        connectedIds.add(edge.source); connectedIds.add(edge.target)
      }
    }
    return filteredBaseNodes.filter((node) => connectedIds.has(node.id))
  }, [baseVisibleIds, candidateViewEdges, filteredBaseNodes, graph, relationFilter])

  const renderNodes = relationFilteredNodes

  const visibleIds = useMemo(() => new Set(renderNodes.map((node) => node.id)), [renderNodes])
  const viewEdges = useMemo(() => {
    if (!topologyIndex) return []
    const edges = projectVisibleTopologyEdges(topologyIndex, visibleIds, baseVisibleIds)
    return edges.filter((edge) => relationFilter === "all" || edge.relation === relationFilter)
  }, [baseVisibleIds, relationFilter, topologyIndex, visibleIds])
  const renderEdges = viewEdges
  const visibleEdges = useMemo(() => {
    if (!graph) return []
    return visualEdges(graph, renderEdges, visibleIds, shownKinds.has("component"))
  }, [graph, renderEdges, shownKinds, visibleIds])
  const clusters = useMemo(
    () => buildTopologyClusters(renderNodes, visibleEdges),
    [renderNodes, visibleEdges],
  )
  const degreeByNode = useMemo(() => {
    const degrees = new Map<string, number>()
    for (const edge of visibleEdges) {
      degrees.set(edge.source, (degrees.get(edge.source) ?? 0) + 1)
      degrees.set(edge.target, (degrees.get(edge.target) ?? 0) + 1)
    }
    return degrees
  }, [visibleEdges])
  const adjacencyByNode = useMemo(() => {
    const adjacency = new Map<string, Set<string>>()
    for (const edge of visibleEdges) {
      if (!adjacency.has(edge.source)) adjacency.set(edge.source, new Set())
      if (!adjacency.has(edge.target)) adjacency.set(edge.target, new Set())
      adjacency.get(edge.source)?.add(edge.target)
      adjacency.get(edge.target)?.add(edge.source)
    }
    return adjacency
  }, [visibleEdges])
  adjacencyRef.current = adjacencyByNode

  const rawForceGraphData = useMemo<TopologyForceData>(() => {
    const nodes = renderNodes.map((node) => {
      const cached = forceNodeCacheRef.current.get(node.id) ?? {
        id: node.id,
        name: nodeLabel(node),
        hoverLabel: nodeHoverLabel(node),
        kind: node.kind,
        degree: 0,
      }
      cached.degree = degreeByNode.get(node.id) ?? 0
      cached.kind = node.kind
      cached.name = nodeLabel(node)
      cached.hoverLabel = nodeHoverLabel(node)
      cached.platform = node.platform ?? undefined
      forceNodeCacheRef.current.set(node.id, cached)
      return cached
    })
    const links = visibleEdges.map((edge) => ({
      id: edge.id,
      relation: edge.relation,
      projected: edge.properties.projected === true,
      source: edge.source,
      target: edge.target,
    }))
    return { nodes, links }
  }, [degreeByNode, renderNodes, visibleEdges])
  const forceViewKey = useMemo(() => JSON.stringify({
    fingerprint: graph?.source_fingerprint,
    endpoint,
    platform,
    query: query.trim().toLowerCase(),
    kindFilter,
    relationFilter,
    shownKinds: [...shownKinds].sort(),
  }), [endpoint, graph?.source_fingerprint, kindFilter, platform, query, relationFilter, shownKinds])
  const forceGraphData = useMemo(() => {
    const cache = viewCacheRef.current
    const cached = cache.get(forceViewKey)
    if (cached) {
      // Refresh insertion order so the small cache behaves as an LRU.
      cache.delete(forceViewKey)
      cache.set(forceViewKey, cached)
      return cached
    }
    cache.set(forceViewKey, rawForceGraphData)
    while (cache.size > MAX_TOPOLOGY_VIEW_CACHE) {
      const oldest = cache.keys().next().value
      if (oldest === undefined) break
      cache.delete(oldest)
    }
    return rawForceGraphData
  }, [forceViewKey, rawForceGraphData])
  forceDataRef.current = forceGraphData

  useEffect(() => {
    viewCacheRef.current.clear()
    appliedForceDataRef.current = null
  }, [graph?.source_fingerprint])

  const hasProcessClusters = useMemo(
    () => clusters.some((cluster) => cluster.nodes.some((node) => node.kind === "process")),
    [clusters],
  )
  const centerGraphNode = (
    nodeId: string,
    detailOpen = Boolean(selectedId),
  ) => {
    const instance = forceGraphRef.current
    const host = graphHostRef.current
    const node = forceDataRef.current.nodes.find((item) => item.id === nodeId)
    if (!instance || !host || !node || node.x === undefined || node.y === undefined) return
    const zoomLevel = Math.max(0.42, zoomRef.current)
    const leftInset = (hasProcessClusters ? 316 : 0) + (detailOpen ? 356 : 0)
    const rightInset = showFilterPanel ? 316 : 0
    const visibleLeft = leftInset
    const visibleRight = Math.max(visibleLeft + 80, host.clientWidth - rightInset)
    const targetScreenX = (visibleLeft + visibleRight) / 2
    const targetWorldX = node.x + (host.clientWidth / 2 - targetScreenX) / zoomLevel
    instance.centerAt(targetWorldX, node.y, 360)
  }

  useEffect(() => {
    if (!selectedId) return undefined
    const frame = window.requestAnimationFrame(() => centerGraphNode(selectedId, true))
    return () => window.cancelAnimationFrame(frame)
  }, [hasProcessClusters, selectedId, showFilterPanel])

  useEffect(() => {
    if (selectedId && !visibleIds.has(selectedId)) setSelectedId(null)
  }, [selectedId, visibleIds])

  useEffect(() => {
    const host = graphHostRef.current
    if (!host) return undefined
    let disposed = false
    let instance: TopologyForceGraph | null = null
    let resizeObserver: ResizeObserver | null = null

    const setup = async () => {
      const [{ default: ForceGraphConstructor }, { forceCollide }] = await Promise.all([
        import("force-graph"),
        import("d3-force"),
      ])
      if (disposed) return

      instance = new ForceGraphConstructor(host) as TopologyForceGraph
      forceGraphRef.current = instance

      const getNodeId = (value: string | number | TopologyForceNode | undefined) => (
        value && typeof value === "object" ? value.id : value === undefined ? "" : String(value)
      )
      const isConnectedToHovered = (nodeId: string, currentHoveredId: string | null) => (
        Boolean(currentHoveredId && (nodeId === currentHoveredId || adjacencyRef.current.get(currentHoveredId)?.has(nodeId)))
      )
      const isConnectedToSelected = (nodeId: string) => {
        const currentSelectedId = selectedIdRef.current
        return Boolean(
          selectedFocusEnabledRef.current && currentSelectedId
          && (nodeId === currentSelectedId || adjacencyRef.current.get(currentSelectedId)?.has(nodeId)),
        )
      }
      const selectedLinkIsActive = (link: TopologyForceLink) => {
        const currentSelectedId = selectedIdRef.current
        return Boolean(
          selectedFocusEnabledRef.current && currentSelectedId
          && (getNodeId(link.source) === currentSelectedId || getNodeId(link.target) === currentSelectedId),
        )
      }
      const linkIsActive = (link: TopologyForceLink) => {
        const currentHoveredId = hoveredIdRef.current
        if (currentHoveredId) return Boolean(getNodeId(link.source) === currentHoveredId || getNodeId(link.target) === currentHoveredId)
        return selectedLinkIsActive(link)
      }
      const linkColor = (link: TopologyForceLink) => {
        if (hoveredIdRef.current) {
          return linkIsActive(link)
            ? (themeRef.current === "dark" ? "#e9d5ff" : "#5b21b6")
            : (themeRef.current === "dark" ? "rgba(170,181,196,.18)" : "rgba(203,213,225,.86)")
        }
        if (selectedFocusEnabledRef.current && selectedIdRef.current && !selectedLinkIsActive(link)) return themeRef.current === "dark" ? "rgba(170,181,196,.18)" : "rgba(203,213,225,.86)"
        if (linkIsActive(link)) return selectedIdRef.current ? (themeRef.current === "dark" ? "#60a5fa" : "#2563eb") : (themeRef.current === "dark" ? "#e9d5ff" : "#5b21b6")
        if (link.relation === "depends_on") return themeRef.current === "dark" ? "#9b87f5" : "#7654d6"
        if (link.relation === "routes_to") return themeRef.current === "dark" ? "#f4b860" : "#c37a19"
        if (link.relation === "observes") return themeRef.current === "dark" ? "#55d6c2" : "#138f82"
        if (link.relation === "audits_to") return themeRef.current === "dark" ? "#b9a7d9" : "#9a86b8"
        if (link.relation === "uses_service") return themeRef.current === "dark" ? "#7ec8a4" : "#3f9565"
        if (link.relation === "related_to") return "#6ea3ed"
        if (link.relation === "documents" || link.relation === "handles_alert") return "#66c6a1"
        return themeRef.current === "dark" ? "rgba(100,116,139,.24)" : "rgba(100,116,139,.25)"
      }
      const linkWidth = (link: TopologyForceLink) => linkIsActive(link) ? (selectedIdRef.current ? 3.4 : 3) : link.relation === "depends_on" ? 1.6 : 1
      const linkDash = (link: TopologyForceLink) => link.projected ? [6, 4] : link.relation === "related_to" ? [3, 4] : link.relation === "documents" || link.relation === "handles_alert" || link.relation === "observes" || link.relation === "audits_to" ? [2, 4] : link.relation === "uses_service" ? [5, 3] : null
      const paintNode = (node: TopologyForceNode, context: CanvasRenderingContext2D, globalScale: number) => {
        const currentHoveredId = hoveredIdRef.current
        const hovered = node.id === currentHoveredId
        const selected = node.id === selectedIdRef.current
        const dimmed = currentHoveredId
          ? !isConnectedToHovered(node.id, currentHoveredId)
          : Boolean(selectedIdRef.current && selectedFocusEnabledRef.current && !isConnectedToSelected(node.id))
        const radius = nodeRadiusForKind(node.kind, node.degree, hovered || selected)
        const colors = NODE_COLORS[node.kind] ?? NODE_COLORS.component
        context.save()
        context.globalAlpha = dimmed ? 0.12 : hovered || selected ? 1 : 0.82
        context.fillStyle = hovered ? (themeRef.current === "dark" ? "#f8fafc" : "#ffffff") : selected ? (themeRef.current === "dark" ? "#60a5fa" : "#2563eb") : colors.fill
        context.strokeStyle = hovered ? "#ffffff" : selected ? "#ede9fe" : colors.stroke
        context.lineWidth = (hovered || selected ? 2 : 0.7) / globalScale
        context.beginPath(); context.arc(node.x ?? 0, node.y ?? 0, radius, 0, Math.PI * 2); context.fill(); context.stroke()
        if ((showLabelsRef.current && ["platform", "cluster", "service"].includes(node.kind)) || hovered || selected) {
          context.globalAlpha = dimmed ? 0.12 : 1
          context.font = `${hovered || selected ? "600 " : ""}${12 / globalScale}px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif`
          context.textAlign = "center"; context.textBaseline = "bottom"
          context.fillStyle = themeRef.current === "dark" ? "#dbeafe" : "#334155"
          context.strokeStyle = themeRef.current === "dark" ? "#101722" : "#f5f7fb"
          context.lineWidth = 3 / globalScale
          const label = hovered || selected ? node.hoverLabel : node.name
          context.strokeText(label, node.x ?? 0, (node.y ?? 0) - radius - 5 / globalScale)
          context.fillText(label, node.x ?? 0, (node.y ?? 0) - radius - 5 / globalScale)
        }
        context.restore()
      }
      const paintNodePointerArea = (node: TopologyForceNode, color: string, context: CanvasRenderingContext2D, globalScale: number) => {
        context.fillStyle = color
        context.beginPath(); context.arc(node.x ?? 0, node.y ?? 0, nodeRadiusForKind(node.kind, node.degree, false) + 6 / globalScale, 0, Math.PI * 2); context.fill()
      }
      const refresh = () => {
        // These setters notify force-graph that its paused Canvas frame is
        // dirty, without restarting the force simulation.
        instance?.nodeCanvasObject(paintNode)
        instance?.linkColor(linkColor)
        instance?.linkWidth(linkWidth)
      }
      refreshGraphRef.current = refresh

      instance
        .width(host.clientWidth || 1200)
        .height(host.clientHeight || 720)
        .backgroundColor("transparent")
        .nodeId("id")
        .nodeVal((node) => Math.max(1, node.degree))
        .nodeRelSize(1)
        .nodeLabel((node) => node.hoverLabel)
        .nodeCanvasObjectMode(() => "replace")
        .nodeCanvasObject(paintNode)
        .nodePointerAreaPaint(paintNodePointerArea)
        .linkColor(linkColor)
        .linkWidth(linkWidth)
        .linkLineDash(linkDash)
        .linkDirectionalArrowLength((link) => link.relation === "depends_on" || link.relation === "routes_to" ? 6 : 0)
        .linkDirectionalArrowColor(linkColor)
        .linkDirectionalArrowRelPos(0.9)
        .d3AlphaDecay(0.08)
        .d3VelocityDecay(0.6)
        .warmupTicks(20)
        .cooldownTime(900)
        .autoPauseRedraw(true)
        .onNodeHover((node) => {
          if (clusterHoveredIdRef.current) return
          hoveredIdRef.current = node?.id ?? null
          setHoveredId(node?.id ?? null)
          refresh()
        })
        .onNodeClick((node) => {
          setSelectedId(node.id)
          centerGraphNode(node.id, true)
        })
        .onZoom((transform) => {
          zoomRef.current = transform.k
          if (zoomPaintRef.current !== null) return
          zoomPaintRef.current = window.requestAnimationFrame(() => {
            zoomPaintRef.current = null
            setZoom(zoomRef.current)
          })
        })

      instance.d3Force("collide", forceCollide<TopologyForceNode>()
        .radius((node) => nodeRadiusForKind(node.kind, node.degree, false) + 4)
        .iterations(1))
      instance.d3Force("compact", createCompactComponentForce(forceDataRef))
      instance.d3Force("charge")?.strength(-10)
      instance.d3Force("link")?.distance((link: TopologyForceLink) => link.relation === "depends_on" || link.relation === "routes_to" ? 84 : 44)
      // graphData starts the simulation asynchronously. Fit-to-view here
      // would read undefined node coordinates and produce a NaN transform;
      // center first, then fit after the warmup ticks have produced positions.
      instance.graphData(forceDataRef.current).zoom(0.86).centerAt(0, 0)
      appliedForceDataRef.current = forceDataRef.current
      window.setTimeout(() => {
        if (disposed || selectedIdRef.current || hoveredIdRef.current || clusterHoveredIdRef.current) return
        instance?.zoomToFit(300, 36)
      }, 1000)

      if (typeof ResizeObserver !== "undefined") {
        resizeObserver = new ResizeObserver(() => {
          const current = forceGraphRef.current
          if (current) current.width(host.clientWidth).height(host.clientHeight)
        })
        resizeObserver.observe(host)
      }
    }
    void setup()
    return () => {
      disposed = true
      resizeObserver?.disconnect()
      refreshGraphRef.current = () => undefined
      instance?._destructor()
      if (forceGraphRef.current === instance) forceGraphRef.current = null
    }
  }, [Boolean(graph), isGraph])

  useEffect(() => {
    const instance = forceGraphRef.current
    if (!instance) return undefined
    const isInitialView = appliedForceDataRef.current === null
    for (const node of forceGraphData.nodes) {
      if (node.x !== undefined && node.y !== undefined) {
        forceNodeCacheRef.current.set(node.id, node)
      }
    }
    instance.d3Force("charge")?.strength(forceGraphData.links.length ? -10 : -4)
    instance.d3Force("link")?.strength(1)
    instance.d3Force("compact", createCompactComponentForce(forceDataRef))
    instance
      .d3AlphaDecay(0.08)
      .d3VelocityDecay(0.6)
      .warmupTicks(20)
      .cooldownTime(900)
    instance.graphData(forceGraphData)
    if (!isInitialView) instance.d3ReheatSimulation()
    appliedForceDataRef.current = forceGraphData
    const fitTimer = isInitialView ? window.setTimeout(() => {
      if (forceGraphRef.current === instance && !selectedIdRef.current && !hoveredIdRef.current && !clusterHoveredIdRef.current) instance.zoomToFit(220, 36)
    }, 1000) : undefined
    refreshGraphRef.current()
    return () => { if (fitTimer !== undefined) window.clearTimeout(fitTimer) }
  }, [forceGraphData])

  useEffect(() => {
    refreshGraphRef.current()
  }, [hoveredId, selectedId, showLabels, theme])

  const selectedNode = selectedId ? graphNodeById.get(selectedId) ?? null : null
  const stats = graph?.stats
  const count = (kind: string) => {
    if (isGraph && kind === view?.count_kind && typeof view?.count === "number") return view.count
    return stats?.node_kinds?.[kind] ?? 0
  }
  const cardFilter = (nextKind: FilterKind, nextRelation: RelationFilter = "all") => {
    setKindFilter(nextKind); setRelationFilter(nextRelation)
    if (nextKind !== "all") setShownKinds((prior) => new Set(prior).add(nextKind))
    if (nextRelation === "depends_on") setShownKinds((prior) => new Set(prior).add(primaryKind))
  }
  const reset = () => {
    setPlatform("all"); setQueryInput(""); setQuery(""); setKindFilter("all"); setRelationFilter("all")
    setShownKinds(new Set(defaultVisibleKinds)); setShowLabels(false); setSelectedId(null); setHoveredId(null)
    forceGraphRef.current?.centerAt(0, 0, 250).zoom(0.86, 250)
    zoomRef.current = 0.86; setZoom(0.86)
  }
  const toggleKind = (kind: string) => setShownKinds((prior) => {
    const next = new Set(prior); if (next.has(kind)) next.delete(kind); else next.add(kind); return next
  })
  const updateGraphZoom = (nextZoom: number) => {
    const bounded = Math.min(2.6, Math.max(0.42, nextZoom))
    forceGraphRef.current?.zoom(bounded, 180)
    zoomRef.current = bounded; setZoom(bounded)
  }
  useEffect(() => {
    if (!isGraph) return undefined
    const controller = new AbortController()
    fetchJson<TopologyRefreshStatus>(
      `${endpoint}/status`, controller.signal,
    ).then(setRefreshStatus).catch((reason: Error) => {
      if (reason.name !== "AbortError") setRefreshStatus(null)
    })
    return () => controller.abort()
  }, [isGraph, endpoint])

  // 图谱的种类随图下发:图到手后按 view 的默认可见种类重置
  useEffect(() => {
    if (isGraph && graph?.view) setShownKinds(new Set(graph.view.default_visible_kinds))
  }, [isGraph, graph?.view])

  const refreshGraph = async () => {
    if (!isGraph || refreshing) return
    setRefreshing(true)
    try {
      const accepted = await postJson<TopologyRefreshStatus & { accepted?: boolean }>(
        `${endpoint}/refresh`, {},
      )
      setRefreshStatus(accepted)
      if (accepted.accepted === false) return
      const deadline = Date.now() + 15 * 60 * 1000
      while (Date.now() < deadline) {
        await new Promise((resolve) => window.setTimeout(resolve, 1000))
        const status = await fetchJson<TopologyRefreshStatus>(
          `${endpoint}/status`,
        )
        setRefreshStatus(status)
        if (!status.running) {
          if (status.last_error) throw new Error(status.last_error)
          const payload = await fetchJson<KnowledgeTopology>(endpoint)
          setGraph(payload)
          setError(null)
          break
        }
      }
    } catch (reason) {
      setError((reason as Error).message)
    } finally {
      setRefreshing(false)
    }
  }

  function handleCanvasKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") { setSelectedId(null); setHoveredId(null); return }
    if ((event.key === "Enter" || event.key === " ") && hoveredId) {
      event.preventDefault(); setSelectedId(hoveredId)
    }
  }

  if (error && graph) return <Alert variant="destructive"><AlertDescription>{error}</AlertDescription></Alert>
  if (!graph) {
    if (loading) return <TopologyLoading title={title} />
    if (isGraph) return (
      <section className="knowledge-topology-loading" aria-label={`${title}图谱`}>
        {error ? <Alert variant="destructive"><AlertDescription>{error}</AlertDescription></Alert> : null}
        <div className="flex items-center justify-between gap-3 rounded-md border bg-background p-4">
          <div><p className="font-medium">{title} 图谱尚未生成</p><p className="mt-1 text-xs text-muted-foreground">首次更新会执行一次全量校准，后续按钮使用增量扫描。</p></div>
          <Button disabled={refreshing} onClick={() => void refreshGraph()} size="sm" variant="outline"><RefreshCw className={refreshing ? "size-3.5 animate-spin" : "size-3.5"} />{refreshing ? "更新中" : "立即更新"}</Button>
        </div>
        <Skeleton className="h-[520px] w-full" />
      </section>
    )
    return <TopologyLoading title={title} />
  }

  return (
    <section className="knowledge-topology-shell" aria-label={isGraph ? `${title}图谱` : "知识库拓扑图"}>
      <div className="topology-summary-row">
        <div className="topology-summary-cards">
          {isGraph ? (
            <>
              {(view?.summary_kinds ?? []).map((kind) => <SummaryCard active={kindFilter === kind} key={kind} label={KIND_LABELS[kind] ?? kind} value={count(kind)} onClick={() => cardFilter(kind)} />)}
            </>
          ) : <SummaryCard active={kindFilter === "node"} label="节点" value={count("node")} onClick={() => cardFilter("node")} />}
          <SummaryCard active={kindFilter === "all" && relationFilter === "all"} label="边" value={graph.stats.edge_count} onClick={() => cardFilter("all")} />
          {isGraph ? null : (
            <>
              <SummaryCard active={kindFilter === "platform"} label="平台" value={count("platform")} onClick={() => cardFilter("platform")} />
              <SummaryCard active={kindFilter === "cluster"} label="集群" value={count("cluster")} onClick={() => cardFilter("cluster")} />
              <SummaryCard active={kindFilter === "service"} label="服务" value={count("service")} onClick={() => cardFilter("service")} />
              <SummaryCard active={kindFilter === "alert"} label="告警" value={count("alert")} onClick={() => cardFilter("alert")} />
            </>
          )}
          <SummaryCard active={relationFilter === "depends_on"} label="依赖边" value={graph.stats.edge_relations.depends_on ?? 0} onClick={() => cardFilter("all", "depends_on")} />
        </div>
        <div className="topology-summary-actions">
          {isGraph ? (
            <Button aria-label={`更新${title}图谱`} disabled={refreshing} onClick={() => void refreshGraph()} size="sm" variant="outline">
              <RefreshCw className={refreshing ? "size-3.5 animate-spin" : "size-3.5"} />{refreshing ? "更新中" : "更新拓扑"}
            </Button>
          ) : null}
          {isGraph && refreshStatus?.last_error ? <span className="text-xs text-destructive">上次更新失败</span> : null}
        </div>
      </div>
      {graph.warnings.length ? (
        <Alert className="rounded-none border-x-0 py-2" role="status">
          <AlertDescription className="text-xs">
            拓扑数据提示：{graph.warnings.slice(0, 3).join("；")}
            {graph.warnings.length > 3 ? ` 等 ${graph.warnings.length} 项` : ""}
          </AlertDescription>
        </Alert>
      ) : null}
      <div className={`topology-canvas ${selectedNode ? "has-details" : ""}`}>
        <div
          aria-label="可拖动和缩放的运维拓扑图"
          className="topology-canvas-renderer"
          onKeyDown={handleCanvasKeyDown}
          ref={graphHostRef}
          role="img"
          tabIndex={0}
        />
        <div className="topology-canvas-hint">拖动画布 / 拖动节点 · 滚轮缩放 · {isGraph ? "点击节点查看详情" : "点击节点查看知识库条目"}</div>
        <div className="topology-zoom-controls">
          <Button aria-label="放大拓扑" onClick={() => updateGraphZoom(zoomRef.current * 1.15)} size="icon" variant="outline"><ZoomIn className="size-4" /></Button>
          <span>{Math.round(zoom * 100)}%</span>
          <Button aria-label="缩小拓扑" onClick={() => updateGraphZoom(zoomRef.current * 0.87)} size="icon" variant="outline"><ZoomOut className="size-4" /></Button>
          <Button aria-label="重置视图位置" onClick={() => { forceGraphRef.current?.centerAt(0, 0, 250); updateGraphZoom(0.86) }} size="icon" variant="outline"><FocusIcon /></Button>
        </div>
        {showFilterPanel ? (
          <aside className="topology-filter-panel">
            <div className="topology-panel-header">
              <div><p className="topology-eyebrow">Graph view</p><h3><Filter className="mr-1.5 inline size-4" />筛选</h3></div>
              <div className="topology-panel-header-actions">
                <Button aria-label="重置拓扑筛选" onClick={reset} size="sm" variant="ghost"><RotateCcw className="size-3.5" />重置</Button>
                <Button aria-label="收起筛选面板" className="size-7" onClick={() => setShowFilterPanel(false)} size="icon" variant="ghost"><X className="size-4" /></Button>
              </div>
            </div>
            <div className="topology-filter-scroll">
              <div className="topology-filter-controls">
                <div className="topology-search-wrap"><Search className="size-4 shrink-0" /><Input aria-label="搜索拓扑" onChange={(event) => setQueryInput(event.target.value)} placeholder={isGraph ? "搜索节点名或编号…" : "搜索服务、节点、组件、endpoint…"} value={queryInput} /></div>
                <select aria-label="拓扑平台筛选" className="topology-select w-full" onChange={(event) => setPlatform(event.target.value)} value={platform}>
                  {["all", ...platformKeys()].map((value) => <option key={value} value={value}>{value === "all" ? "全部平台" : platformLabel(value)}</option>)}
                </select>
                <button aria-pressed={relationFilter === "depends_on"} className="topology-relation-filter" onClick={() => { setKindFilter("all"); if (relationFilter === "all") setShownKinds((prior) => new Set(prior).add(primaryKind)); setRelationFilter((value) => value === "all" ? "depends_on" : "all") }} type="button">
                  <span>{relationFilter === "depends_on" ? "仅显示依赖关系" : "显示全部关系"}</span><span className="text-xs text-muted-foreground">{relationFilter === "depends_on" ? "已启用" : ""}</span>
                </button>
              </div>
              <details open><summary><ChevronDown className="size-3.5" />Groups</summary><div className="topology-filter-options">{[...coreKinds, ...optionalKinds].map((kind) => <ToggleRow checked={shownKinds.has(kind)} key={kind} label={KIND_LABELS[kind] ?? kind} onChange={() => toggleKind(kind)} />)}</div></details>
              <details open><summary><ChevronDown className="size-3.5" />Display</summary><div className="topology-filter-options"><ToggleRow checked={showLabels} label="显示服务标签" onChange={() => setShowLabels((value) => !value)} /></div></details>
              <details><summary><ChevronRight className="size-3.5" />关系说明</summary><div className="topology-relation-legend"><span><i className="topology-legend-dot service" />服务</span><span><i className="topology-legend-dot node" />节点</span><span><i className="topology-legend-dot component" />组件</span><span><i className="topology-legend-line" />依赖箭头</span></div></details>
            </div>
          </aside>
        ) : <Button aria-label="展开筛选面板" className="topology-filter-collapsed" onClick={() => setShowFilterPanel(true)} size="sm" variant="outline"><Filter className="size-3.5" />筛选</Button>}
        <TopologyClusterPanel
          clusters={clusters}
          onHoverNode={(node) => {
            clusterHoveredIdRef.current = node?.id ?? null
            setHoveredId(node?.id ?? null)
            if (node) centerGraphNode(node.id)
          }}
          onSelectNode={(node) => {
            clusterHoveredIdRef.current = null
            setSelectedId(node.id)
            setHoveredId(null)
            centerGraphNode(node.id, true)
          }}
        />
        {selectedNode ? <NodeDetails
          graph={graph}
          node={selectedNode}
          viewEdges={visibleEdges}
          onClose={() => { setSelectedId(null); setHoveredId(null) }}
          onOpenDocument={onOpenDocument}
          side={isGraph && (view?.core_kinds ?? []).slice(0, 2).includes(selectedNode.kind) ? "left-adjacent" : "left"}
        /> : null}
      </div>
      <div className="topology-statusbar"><span>当前显示 {renderNodes.length} 个节点 / {visibleEdges.length} 条关系</span><span>数据指纹 <code>{graph.source_fingerprint.slice(0, 12)}…</code></span><span>{isGraph ? `图谱更新于 ${formatTime(graph.completed_at || graph.generated_at)}` : `服务依赖证据 ${graph.coverage.service_dependencies?.documented_edge_count ?? 0} 条`}</span></div>
    </section>
  )
}

function FocusIcon() {
  return <span className="topology-focus-icon" aria-hidden="true">⌖</span>
}
