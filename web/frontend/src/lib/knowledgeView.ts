import type {
  KnowledgeBaseNode,
  KnowledgeCategory,
  KnowledgeIndexState,
  KnowledgePlatformNode,
  KnowledgeSearchHit,
  KnowledgeTree,
  KnowledgeTreeDoc,
} from "@/types"

// 平台显示名随环境包,由 /api/knowledge/tree 的 platform_labels 下发;未下发前只认环境级
let platformLabels: Record<string, string> = { shared: "跨平台" }

export function setPlatformLabels(labels: Record<string, string> | undefined): void {
  if (labels && Object.keys(labels).length) platformLabels = { ...labels }
}

/** 知识库平台键,按环境包声明顺序 */
export function platformKeys(): string[] {
  return Object.keys(platformLabels)
}

export const DOC_TYPE_LABELS: Record<string, string> = {
  troubleshoot: "排查",
  knowledge: "知识",
  procedure: "流程",
  index: "概览",
}

// 服务显示名随环境包,由 /api/knowledge/tree 的 service_labels 下发;未登记的原样显示
let serviceLabels: Record<string, string> = {}

export function setServiceLabels(labels: Record<string, string> | undefined): void {
  if (labels && Object.keys(labels).length) serviceLabels = { ...labels }
}

/** URL 段 → 文档相对路径。路由用 `knowledge/*`，通配段即路径本身。 */
export function pathFromRoute(splat: string | undefined): string | null {
  const raw = (splat ?? "").trim()
  if (!raw) return null
  const decoded = raw.split("/").map((segment) => {
    try { return decodeURIComponent(segment) } catch { return segment }
  }).join("/")
  // 越界形态在后端也会被拦，这里先挡一道免得白跑一次请求
  if (decoded.startsWith("/") || decoded.split("/").includes("..")) return null
  return decoded.endsWith(".md") ? decoded : null
}

export function routeFromPath(path: string): string {
  return `/knowledge/${path}`
}

/**
 * 任务记录里的 kb_doc 既有 `knowledge/` 前缀的存量格式，也有相对路径格式。
 * 知识库路由和 API 都要求相对于 knowledge/ 的文档路径，统一在前端入口处归一化。
 */
export function normalizeKnowledgePath(path: string): string {
  const normalized = path.trim().replace(/^\/+/, "")
  return normalized.startsWith("knowledge/")
    ? normalized.slice("knowledge/".length)
    : normalized
}

export function platformLabel(platform: string): string {
  return platformLabels[platform] ?? platform
}

export const CATEGORY_ORDER: KnowledgeCategory[] = [
  "troubleshoot", "procedure", "cluster_info", "reference", "oplog",
]

export const CATEGORY_LABELS: Record<KnowledgeCategory, string> = {
  troubleshoot: "故障处理",
  procedure: "操作手册",
  cluster_info: "集群信息",
  reference: "参考知识",
  oplog: "操作记录",
}

const SOP_KIND_LABELS: Record<string, string> = { routine: "日常", change: "变更调优", deploy: "部署" }

/** 类别标签;操作手册带上细分(日常/变更调优/部署) */
export function categoryLabel(category: string | null | undefined, sopKind?: string | null): string {
  const base = CATEGORY_LABELS[category as KnowledgeCategory] ?? CATEGORY_LABELS.reference
  return sopKind ? `${base}·${SOP_KIND_LABELS[sopKind] ?? sopKind}` : base
}

export function sopKindLabel(sopKind: string | null | undefined): string | null {
  return sopKind ? SOP_KIND_LABELS[sopKind] ?? sopKind : null
}

export function docTypeLabel(docType: string): string {
  return DOC_TYPE_LABELS[docType] ?? docType
}

export function serviceLabel(service: string): string {
  return serviceLabels[service.toLowerCase()] ?? service
}

export function componentLabel(component: string): string {
  const branded = serviceLabel(component)
  if (branded !== component) return branded
  return component.split(/[-_]/).filter(Boolean).map((part) => (
    part.length <= 3 ? part.toUpperCase() : `${part[0].toUpperCase()}${part.slice(1)}`
  )).join(" ")
}

/** 列表项主标题:正文 H1 比 frontmatter title 更接近人话,缺了才退回。 */
export function docLabel(doc: Pick<KnowledgeTreeDoc, "title" | "doc_title" | "display_title">): string {
  return doc.display_title?.trim() || doc.doc_title?.trim() || doc.title
}

/** 左侧目录只显示问题部分，平台/服务上下文已由父级目录提供。 */
export function docProblemLabel(doc: {
  title: string
  doc_title?: string | null
  display_title: string
}): string {
  const label = doc.display_title?.trim() || doc.doc_title?.trim() || doc.title
  const parts = label.split(/\s+-\s+/)
  return parts.length >= 3 ? parts.slice(2).join(" - ") : label
}

/** Hide only a leading H1 that the detail header already renders. */
export function withoutRepeatedTitle(markdown: string, docTitle: string | null): string {
  if (!docTitle) return markdown
  const escaped = docTitle.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
  return markdown.replace(new RegExp(`^(\\s*)#\\s+${escaped}\\s*(?:\\r?\\n|$)`), "$1")
}

const GENERATED_INDEX_MARKER = /^\s*<!-- GENERATED:KB-INDEX:(?:START|END) -->\s*$/gm

/** Managed source markers are an editing boundary, not document content. */
export function withoutGeneratedIndexMarkers(markdown: string): string {
  return markdown.replace(GENERATED_INDEX_MARKER, "")
}

export function isVerified(status: string | null | undefined): boolean {
  return status === "verified"
}

/** 当前文档所在的 platform/service，用于首屏自动展开左栏到位。 */
export function expandedKeyFor(path: string | null): string | null {
  if (!path) return null
  const parts = path.split("/")
  return parts.length >= 2 ? `${parts[0]}/${parts[1]}` : null
}

export function serviceKey(platform: string, service: string): string {
  return `${platform}/${service}`
}

export function breadcrumb(path: string, bases: readonly Pick<KnowledgeBaseNode, "id" | "name">[] = []): string[] {
  const parts = path.split("/")
  const base = bases.find((item) => item.id && item.id === parts[0])
  const inner = base ? parts.slice(1) : parts
  // 库根索引(只剩文件名)不显示文件名,只留库名
  const trail = inner.length < 2 ? [] : [platformLabel(inner[0]), ...inner.slice(1, -1)]
  return base && bases.length > 1 ? [base.name, ...trail] : trail
}

export type ServiceGroup = {
  key: string
  platform: string
  service: string
  hits: KnowledgeSearchHit[]
}

/** 搜索结果按 platform/service 折叠,顺序沿用后端给的相关度排序。 */
export function groupHits(hits: KnowledgeSearchHit[]): ServiceGroup[] {
  const groups = new Map<string, ServiceGroup>()
  for (const hit of hits) {
    const key = serviceKey(hit.platform, hit.service)
    const existing = groups.get(key)
    if (existing) existing.hits.push(hit)
    else groups.set(key, { key, platform: hit.platform, service: hit.service, hits: [hit] })
  }
  return [...groups.values()]
}

/** 文档在树中的位置:所属库与服务分组键;用树定位比从路径猜可靠(flat 库没有平台目录) */
export type DocLocation = { baseId: string; key: string }

export function locateDoc(tree: KnowledgeTree, path: string | null): DocLocation | null {
  if (!path) return null
  for (const base of tree.bases) {
    for (const platform of base.platforms) {
      for (const service of platform.services) {
        if (service.index_doc === path
          || service.docs.some((doc) => doc.path === path)
          || service.component_indexes.some((item) => item.index_doc === path)) {
          return { baseId: base.id, key: serviceKey(platform.platform, service.service) }
        }
      }
    }
  }
  const head = path.split("/")[0]
  const base = tree.bases.find((item) => item.id && item.id === head)
  return base ? { baseId: base.id, key: "" } : null
}

/** 库首页:库根 _index.md;旧单库布局即知识库根 _index.md */
export function baseHomeDoc(baseId: string | null | undefined): string {
  return baseId ? `${baseId}/_index.md` : "_index.md"
}

export function countDocs(platforms: KnowledgePlatformNode[]): number {
  return platforms.reduce(
    (total, platform) => total + platform.services.reduce(
      (subtotal, service) => subtotal + service.docs.length, 0), 0)
}

export const RELATION_LABELS: Record<string, string> = {
  produced: "产出本文",
  matched: "路由命中",
  consulted: "已查阅",
}

/** 关系标签按信息量排序:产出 > 路由命中 > 已查阅。 */
const RELATION_ORDER = ["produced", "matched", "consulted"]

export function relationLabels(relations: string[]): string[] {
  return [...relations]
    .sort((a, b) => RELATION_ORDER.indexOf(a) - RELATION_ORDER.indexOf(b))
    .map((relation) => RELATION_LABELS[relation] ?? relation)
}

/**
 * 路由命中的文档与已查阅的文档必须分开呈现。
 *
 * 两者含义不同 —— 前者是告警路由表指到的，后者是排查过程中真读过的 ——
 * 同一篇可能两者都是。此前它们用同样的图标和字号逐行铺开，只靠出现顺序
 * 区分，扫一眼看不出哪行是哪种。
 */
export type ConsultedSplit = {
  matchedOnly: string[]
  consultedOnly: string[]
  both: string[]
}

export function splitKnowledgeDocs(
  matchedDoc: string | null, consultedDocs: string[],
): ConsultedSplit {
  const consulted = [...new Set(consultedDocs)]
  const isMatched = (doc: string) => doc === matchedDoc
  return {
    both: matchedDoc && consulted.some(isMatched) ? [matchedDoc] : [],
    matchedOnly: matchedDoc && !consulted.some(isMatched) ? [matchedDoc] : [],
    consultedOnly: consulted.filter((doc) => doc !== matchedDoc),
  }
}

export type Heading = { depth: 2 | 3; text: string; id: string }

/** 标题锚点 id。中文不做转写,只清掉空白与 markdown 标记,冲突时后缀去重。 */
export function headingId(text: string, seen: Set<string>): string {
  const base = text.trim().toLowerCase()
    .replace(/[`*_~[\]()]/g, "")
    .replace(/\s+/g, "-") || "section"
  let id = base
  for (let n = 2; seen.has(id); n += 1) id = `${base}-${n}`
  seen.add(id)
  return id
}

/** 右栏目录。与后端 extract_h1 同样跳过 fenced code，免得把注释当标题。 */
export function extractHeadings(markdown: string): Heading[] {
  const seen = new Set<string>()
  const headings: Heading[] = []
  let inFence = false
  for (const line of markdown.split("\n")) {
    if (/^\s{0,3}(?:```|~~~)/.test(line)) { inFence = !inFence; continue }
    if (inFence) continue
    const matched = /^(#{2,3})\s+(.+?)\s*$/.exec(line)
    if (!matched) continue
    const text = matched[2].trim()
    headings.push({ depth: matched[1].length as 2 | 3, text, id: headingId(text, seen) })
  }
  return headings
}

export type IndexNotice = { level: "error" | "warn"; message: string } | null

/**
 * 索引状态提示。树与正文直读源文件所以始终最新，只有搜索走索引 —— 说清楚
 * 「搜不到刚写的文档」是索引旧了，不是文档没了。
 */
export function indexNotice(state: KnowledgeIndexState | null): IndexNotice {
  if (!state) return null
  if (!state.available) {
    return { level: "error", message: `搜索不可用：索引尚未生成。${state.hint ?? ""}`.trim() }
  }
  if (state.stale) {
    return { level: "warn", message: `搜索索引早于最新文档，新写入的内容可能搜不到。${state.hint ?? ""}`.trim() }
  }
  return null
}
