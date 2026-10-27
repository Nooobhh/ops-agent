import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { BookOpen, ChevronDown, ChevronRight, FileText, Maximize2, Network, Search, Workflow, X } from "lucide-react"
import { useNavigate, useParams } from "react-router-dom"
import type { Components } from "react-markdown"
import { KnowledgeHealthPanel } from "@/components/KnowledgeHealthPanel"
import { KnowledgeTopology } from "@/components/KnowledgeTopology"
import { MarkdownView } from "@/components/MarkdownView"
import { PageHeader } from "@/components/PageHeader"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { fetchJson, formatTime } from "@/lib/api"
import {
  baseHomeDoc, breadcrumb, CATEGORY_LABELS, CATEGORY_ORDER, categoryLabel, componentLabel, countDocs,
  docProblemLabel, docTypeLabel, extractHeadings, sopKindLabel,
  groupHits, headingId, indexNotice, isVerified, pathFromRoute, platformLabel, setPlatformLabels, setServiceLabels,
  locateDoc, relationLabels, routeFromPath, serviceKey,
  serviceLabel, withoutGeneratedIndexMarkers, withoutRepeatedTitle,
} from "@/lib/knowledgeView"
import { remarkWikiLink } from "@/lib/remarkWikiLink"
import type {
  KnowledgeBaseNode, KnowledgeCategory, KnowledgeDoc, KnowledgeHealth, KnowledgeReferences,
  KnowledgeSearchResult, KnowledgeTree, KnowledgeTreeDoc,
} from "@/types"

function StatusDot({ status, docType }: { status: string | null; docType?: string }) {
  if (docType === "knowledge") {
    return <BookOpen aria-hidden="true" className="size-3.5 shrink-0 text-muted-foreground" />
  }
  if (!status) return null
  return (
    <span
      aria-hidden="true"
      className={`size-[7px] shrink-0 rounded-full ${
        isVerified(status) ? "bg-[#2f9e6e]" : "bg-muted-foreground/40"}`}
      title={isVerified(status) ? "已验证" : "草稿"}
    />
  )
}

function TreeDocumentButton({ item, currentPath, open, indent = "pl-7" }: {
  item: KnowledgeTreeDoc
  currentPath: string | null
  open: (path: string) => void
  indent?: string
}) {
  return (
    <button
      className={`flex w-full items-center gap-2 rounded py-1 pr-2 text-left hover:bg-muted ${indent} ${
        item.path === currentPath ? "bg-muted font-medium" : ""}`}
      onClick={() => open(item.path)}
      title={item.problem ?? undefined}
      type="button"
    >
      <StatusDot docType={item.doc_type} status={item.status} />
      <span className="min-w-0 flex-1 truncate">{docProblemLabel(item)}</span>
      {item.sop_kind && item.sop_kind !== "routine" ? (
        <span className="shrink-0 text-[11px] text-muted-foreground">{sopKindLabel(item.sop_kind)}</span>
      ) : null}
    </button>
  )
}

function DocumentContent({ doc, currentPath, components, plugins, bases }: {
  doc: KnowledgeDoc
  currentPath: string | null
  bases: KnowledgeBaseNode[]
  components: Components
  plugins: ReturnType<typeof remarkWikiLink>[]
}) {
  return (
    <>
      {currentPath ? (
        <p className="text-xs text-muted-foreground">{breadcrumb(doc.path, bases).join(" / ")}</p>
      ) : null}
      <h1 className="mt-1 break-words text-xl font-semibold leading-7">
        {doc.meta.display_title}
      </h1>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        {doc.meta.category ? (
          <Badge variant="secondary">{categoryLabel(doc.meta.category, doc.meta.sop_kind)}</Badge>
        ) : doc.meta.type ? <Badge variant="secondary">{docTypeLabel(doc.meta.type)}</Badge> : null}
        {doc.meta.status ? (
          <Badge variant={isVerified(doc.meta.status) ? "default" : "outline"}>
            {isVerified(doc.meta.status) ? "已验证" : "草稿"}
          </Badge>
        ) : null}
        {doc.meta.alerts.map((alertname) => (
          <Badge key={alertname} variant="outline">{alertname}</Badge>
        ))}
      </div>
      {doc.meta.lint_error ? (
        <Alert className="mt-3" variant="destructive">
          <AlertDescription>frontmatter 有问题：{doc.meta.lint_error}</AlertDescription>
        </Alert>
      ) : null}
      <div className="mt-4">
        <MarkdownView
          components={components}
          content={withoutGeneratedTitle(doc)}
          plugins={plugins}
        />
      </div>
    </>
  )
}

function withoutGeneratedTitle(doc: KnowledgeDoc): string {
  return withoutGeneratedIndexMarkers(
    withoutRepeatedTitle(doc.markdown, doc.meta.doc_title),
  )
}

function DocumentOutline({ headings, onSelect, idPrefix = "" }: {
  headings: ReturnType<typeof extractHeadings>
  onSelect: (id: string) => void
  idPrefix?: string
}) {
  return (
    <div>
      <p className="pb-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        本页目录
      </p>
      {headings.length ? (
        <nav className="flex flex-col gap-1">
          {headings.map((item) => (
            <a
              className={item.depth === 3
                ? "truncate pl-3 text-xs text-muted-foreground hover:text-[var(--link)]"
                : "truncate text-xs text-muted-foreground hover:text-[var(--link)]"}
              href={"#" + idPrefix + item.id}
              key={item.id}
              onClick={(event) => {
                event.preventDefault()
                onSelect(item.id)
              }}
            >{item.text}</a>
          ))}
        </nav>
      ) : <p className="text-xs text-muted-foreground">（无小节）</p>}
    </div>
  )
}

// 根 _index.md 是知识库总索引(25 条服务导航),但按路径深度被排除在服务树之外,
// 左栏点不到它。未选中文档时把它当首页渲染,总索引才有入口。

/** 谁引用过这篇文档。空关系不占版面,四类全空则整块不渲染。 */
function ReferencePanel({ refs, navigate }: {
  refs: KnowledgeReferences | null
  navigate: (to: string) => void
}) {
  if (!refs) return null
  const { tasks, alerts, conclusions, case_lines: caseLines } = refs
  if (!tasks.length && !alerts.length && !conclusions.length && !caseLines.length) {
    return null
  }
  return (
    <section className="mt-8 border-t pt-5">
      <h2 className="text-sm font-semibold">引用与命中</h2>

      {alerts.length ? (
        <div className="mt-3">
          <p className="text-xs text-muted-foreground">路由到本文的告警</p>
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {alerts.map((alertname) => (
              <button
                className="rounded border px-2 py-0.5 font-mono text-xs hover:bg-muted"
                key={alertname}
                onClick={() => navigate(`/alerts?alertname=${encodeURIComponent(alertname)}`)}
                type="button"
              >{alertname}</button>
            ))}
          </div>
        </div>
      ) : null}

      {tasks.length ? (
        <div className="mt-4">
          <p className="text-xs text-muted-foreground">中台排查任务（{tasks.length}）</p>
          <ul className="mt-1.5 flex flex-col gap-1.5">
            {tasks.map((task) => (
              <li className="flex flex-wrap items-center gap-2 text-xs" key={task.task_id}>
                <button
                  className="font-mono text-[var(--link)] hover:underline"
                  onClick={() => navigate(`/triage?task=${encodeURIComponent(task.task_id)}`)}
                  type="button"
                >{task.task_id}</button>
                {relationLabels(task.relations).map((label) => (
                  <Badge className="px-1.5 py-0" key={label} variant="secondary">{label}</Badge>
                ))}
                <span className="text-muted-foreground">{task.status}</span>
                {task.created_at ? (
                  <span className="text-muted-foreground">{formatTime(task.created_at)}</span>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {conclusions.length ? (
        <div className="mt-4">
          <p className="text-xs text-muted-foreground">收口结论（{conclusions.length}）</p>
          <ul className="mt-1.5 flex flex-col gap-1.5">
            {conclusions.map((item) => (
              <li className="flex flex-wrap items-center gap-2 text-xs" key={item.run_id}>
                <span className="font-mono">{item.run_id}</span>
                <Badge className="px-1.5 py-0" variant="outline">{item.verdict}</Badge>
                {item.via === "kb_draft" ? (
                  <Badge className="px-1.5 py-0" variant="secondary">草稿</Badge>
                ) : null}
                {item.kb_doc_effective ? (
                  <Badge className="px-1.5 py-0" variant="secondary">实质更新</Badge>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {caseLines.length ? (
        <div className="mt-4">
          <p className="text-xs text-muted-foreground">文档自述历史案例（{caseLines.length}）</p>
          <ul className="mt-1.5 flex flex-col gap-1 text-xs text-muted-foreground">
            {caseLines.map((line) => (
              <li className="break-words" key={line.text}>{line.text}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {/* CLI 侧 session.json 无 kb_doc 字段,回链天生不全——说清楚，不装完整 */}
      {refs.coverage.sessions_total > refs.coverage.sessions_linkable ? (
        <p className="mt-4 text-[11px] text-muted-foreground">
          {refs.coverage.note}（{refs.coverage.sessions_linkable}/{refs.coverage.sessions_total}）
        </p>
      ) : null}
    </section>
  )
}

export default function Knowledge() {
  const navigate = useNavigate()
  const params = useParams()
  const currentPath = pathFromRoute(params["*"])

  const [tree, setTree] = useState<KnowledgeTree | null>(null)
  const [activeBase, setActiveBase] = useState<string | null>(null)
  const bases = tree?.bases ?? []
  const activeBaseNode = bases.find((base) => base.id === activeBase) ?? bases[0] ?? null
  // 首页是当前库的根索引;树未加载前不知道有哪些库,先不请求首页
  const targetPath = currentPath ?? (tree ? baseHomeDoc(activeBaseNode?.id) : null)
  const [treeError, setTreeError] = useState<string | null>(null)
  const [doc, setDoc] = useState<KnowledgeDoc | null>(null)
  const [docError, setDocError] = useState<string | null>(null)
  const [docLoading, setDocLoading] = useState(false)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [collapsedPlatforms, setCollapsedPlatforms] = useState<Set<string>>(new Set())
  const [collapsedComponents, setCollapsedComponents] = useState<Set<string>>(new Set())
  const [categoryFilter, setCategoryFilter] = useState<"all" | KnowledgeCategory>("all")
  const [query, setQuery] = useState("")
  const [results, setResults] = useState<KnowledgeSearchResult | null>(null)
  const [searchError, setSearchError] = useState<string | null>(null)
  const [refs, setRefs] = useState<KnowledgeReferences | null>(null)
  const [health, setHealth] = useState<KnowledgeHealth | null>(null)
  // 视图:文档 / 知识库拓扑 / 环境包图谱(graph:<id>)
  const [view, setView] = useState<string>(() => "documents")
  const [graphs, setGraphs] = useState<Array<{ id: string; title: string }>>([])
  const articleRef = useRef<HTMLDivElement>(null)
  const fullscreenArticleRef = useRef<HTMLElement>(null)
  const fullscreenCloseRef = useRef<HTMLButtonElement>(null)
  const [isReaderFullscreen, setIsReaderFullscreen] = useState(false)

  useEffect(() => {
    fetchJson<KnowledgeTree>("/api/knowledge/tree")
      .then((payload) => { setPlatformLabels(payload.platform_labels); setServiceLabels(payload.service_labels); setTree(payload); setTreeError(null) })
      .catch((error: Error) => setTreeError(error.message))
    fetchJson<KnowledgeHealth>("/api/knowledge/health")
      .then(setHealth)
      .catch(() => setHealth(null))
    fetchJson<{ graphs: Array<{ id: string; title: string }> }>("/api/knowledge/graphs")
      .then((payload) => setGraphs(payload.graphs))
      .catch(() => setGraphs([]))
  }, [])

  // 深链进来时切到文档所属的库,并把左栏展开到其所在服务
  useEffect(() => {
    if (!tree) return
    const location = locateDoc(tree, currentPath)
    if (!location) return
    setActiveBase(location.baseId)
    const key = location.key
    if (key) setExpanded((prior) => (prior.has(key) ? prior : new Set(prior).add(key)))
  }, [currentPath, tree])

  useEffect(() => {
    if (!targetPath) return
    const controller = new AbortController()
    setDocLoading(true)
    fetchJson<KnowledgeDoc>(
      `/api/knowledge/doc?path=${encodeURIComponent(targetPath)}`, controller.signal)
      .then((payload) => {
        setDoc(payload)
        setDocError(null)
        // 旧文档 ID(迁移前链接)由后端映射到现行 ID;地址栏同步成现行 ID,左栏随之定位
        if (currentPath && payload.path !== currentPath) {
          navigate(routeFromPath(payload.path), { replace: true })
        }
      })
      .catch((error: Error) => {
        if (error.name === "AbortError") return
        setDoc(null)
        // 首页缺 _index.md 不算错,退回空态提示即可
        setDocError(currentPath ? error.message : null)
      })
      .finally(() => setDocLoading(false))
    return () => controller.abort()
  }, [currentPath, navigate, targetPath])

  // 引用关系独立请求:正文先渲染,回链后到,不阻塞阅读
  useEffect(() => {
    if (!targetPath) return
    const controller = new AbortController()
    setRefs(null)
    fetchJson<KnowledgeReferences>(
      `/api/knowledge/references?path=${encodeURIComponent(targetPath)}`,
      controller.signal)
      .then(setRefs)
      .catch(() => setRefs(null))
    return () => controller.abort()
  }, [targetPath])

  useEffect(() => {
    articleRef.current?.scrollTo({ top: 0 })
    fullscreenArticleRef.current?.scrollTo({ top: 0 })
    if (currentPath) articleRef.current?.scrollIntoView({ behavior: "smooth", block: "start" })
  }, [currentPath])

  useEffect(() => {
    if (!isReaderFullscreen) return
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = "hidden"
    fullscreenCloseRef.current?.focus()
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setIsReaderFullscreen(false)
    }
    window.addEventListener("keydown", onKeyDown)
    return () => {
      document.body.style.overflow = previousOverflow
      window.removeEventListener("keydown", onKeyDown)
    }
  }, [isReaderFullscreen])

  const runSearch = useCallback(async (raw: string) => {
    const trimmed = raw.trim()
    if (!trimmed) { setResults(null); setSearchError(null); return }
    try {
      setResults(await fetchJson<KnowledgeSearchResult>(
        `/api/knowledge/search?q=${encodeURIComponent(trimmed)}&limit=30`))
      setSearchError(null)
    } catch (error) {
      setResults(null)
      setSearchError((error as Error).message)
    }
  }, [])

  useEffect(() => {
    const timer = window.setTimeout(() => { void runSearch(query) }, 250)
    return () => window.clearTimeout(timer)
  }, [query, runSearch])

  function toggle(key: string) {
    setExpanded((prior) => {
      const next = new Set(prior)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  function togglePlatform(platform: string) {
    setCollapsedPlatforms((prior) => {
      const next = new Set(prior)
      if (next.has(platform)) next.delete(platform)
      else next.add(platform)
      return next
    })
  }

  function toggleComponent(component: string) {
    setCollapsedComponents((prior) => {
      const next = new Set(prior)
      if (next.has(component)) next.delete(component)
      else next.add(component)
      return next
    })
  }

  function open(path: string) {
    setView("documents")
    navigate(routeFromPath(path))
  }

  function openHealthDocument(path: string) {
    setCategoryFilter("all")
    open(path)
  }

  const headings = useMemo(
    () => (doc ? extractHeadings(doc.markdown) : []), [doc])

  // wikilink 与标题锚点都在这里接上:站内跳转走 router，不整页刷新
  const makeMarkdownComponents = useCallback((idPrefix = ""): Components => {
    const seen = new Set<string>()
    const heading = (Tag: "h2" | "h3") => ({ children }: { children?: React.ReactNode }) => {
      const text = typeof children === "string" ? children : String(children ?? "")
      const id = headingId(text, seen)
      return <Tag data-heading-id={id} id={idPrefix + id}>{children}</Tag>
    }
    return {
      h2: heading("h2"),
      h3: heading("h3"),
      a: ({ href, children, ...rest }) => {
        if (href?.startsWith("/knowledge/") || href?.startsWith("/triage/") || href?.startsWith("/reports/")) {
          return (
            <a
              {...rest}
              href={href}
              onClick={(event) => { event.preventDefault(); navigate(href) }}
            >{children}</a>
          )
        }
        return <a {...rest} href={href} rel="noopener noreferrer" target="_blank">{children}</a>
      },
    }
  }, [navigate])

  const markdownComponents = useMemo(
    () => makeMarkdownComponents(), [makeMarkdownComponents])
  const fullscreenMarkdownComponents = useMemo(
    () => makeMarkdownComponents("reader-fullscreen-"), [makeMarkdownComponents])

  const plugins = useMemo(
    () => (doc ? [remarkWikiLink(doc.wikilinks)] : []), [doc])

  const notice = indexNotice(results?.index_state ?? tree?.index_state ?? null)
  const searching = Boolean(query.trim())
  const matchesCategory = (category: string) => categoryFilter === "all" || category === categoryFilter
  const searchHits = results?.hits.filter((hit) => matchesCategory(hit.category)) ?? []

  function scrollToHeading(container: HTMLElement | null, id: string) {
    const target = Array.from(
      container?.querySelectorAll<HTMLElement>("[data-heading-id]") ?? [],
    ).find((item) => item.dataset.headingId === id)
    target?.scrollIntoView({ behavior: "smooth", block: "start" })
  }

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        meta={tree
          ? `${bases.length > 1 ? `${bases.length} 个知识库，` : ""}${countDocs(bases.flatMap((base) => base.platforms))} 篇文档，直读知识库源文件`
          : "加载中"}
        title="知识库"
        actions={(
          <div className="flex items-center gap-1 rounded-md border p-1">
            <Button
              aria-pressed={view === "documents"}
              className={view === "documents" ? "bg-accent text-accent-foreground" : ""}
              onClick={() => {
                setView("documents")
                if (!currentPath) navigate(routeFromPath(baseHomeDoc(activeBaseNode?.id)))
              }}
              size="sm"
              variant="ghost"
            ><BookOpen className="size-4" />文档</Button>
            <Button
              aria-pressed={view === "topology"}
              className={view === "topology" ? "bg-accent text-accent-foreground" : ""}
              onClick={() => setView("topology")}
              size="sm"
              variant="ghost"
            ><Network className="size-4" />拓扑</Button>
            {graphs.map((item) => <Button
              aria-pressed={view === `graph:${item.id}`}
              className={view === `graph:${item.id}` ? "bg-accent text-accent-foreground" : ""}
              key={item.id}
              onClick={() => setView(`graph:${item.id}`)}
              size="sm"
              variant="ghost"
            ><Workflow className="size-4" />{item.title}</Button>)}
          </div>
        )}
      />

      {treeError ? (
        <Alert variant="destructive"><AlertDescription>{treeError}</AlertDescription></Alert>
      ) : null}
      {notice ? (
        <Alert variant={notice.level === "error" ? "destructive" : "default"}>
          <AlertDescription>{notice.message}</AlertDescription>
        </Alert>
      ) : null}
      {tree?.warnings.length ? (
        <Alert>
          <AlertDescription>
            {tree.warnings.length} 篇文档 frontmatter 有问题，已跳过：{tree.warnings[0]}
            {tree.warnings.length > 1 ? ` 等 ${tree.warnings.length} 处` : ""}
          </AlertDescription>
        </Alert>
      ) : null}

      <KnowledgeHealthPanel health={health} onOpenDocument={openHealthDocument} />

      {view === "documents" ? (
        <Button
          aria-label="全屏阅读"
          className="fixed right-5 top-24 z-40 size-10 rounded-full border bg-background/95 shadow-lg backdrop-blur hover:scale-105"
          disabled={!doc || docLoading}
          onClick={() => setIsReaderFullscreen(true)}
          size="icon"
          title="全屏阅读"
          variant="outline"
        ><Maximize2 className="size-4" /></Button>
      ) : null}

      {view === "topology" ? (
        <KnowledgeTopology onOpenDocument={open} />
      ) : view.startsWith("graph:") ? (
        <KnowledgeTopology
          endpoint={`/api/knowledge/graphs/${encodeURIComponent(view.slice("graph:".length))}`}
          key={view}
          onOpenDocument={open}
          title={graphs.find((item) => `graph:${item.id}` === view)?.title ?? "图谱"}
          variant="graph"
        />
      ) : (
      <div
        aria-hidden={isReaderFullscreen}
        className="grid items-stretch gap-4 lg:h-[calc(100dvh-15rem)] lg:min-h-0 lg:grid-cols-[264px_minmax(0,1fr)] xl:grid-cols-[264px_minmax(0,1fr)_208px]"
      >
        <aside className="flex min-h-0 flex-col rounded-md border bg-muted/25 lg:h-full">
          <div className="relative border-b p-3">
            <Search className="absolute left-5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              className="pl-8"
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索全库"
              value={query}
            />
            {query ? (
              <Button
                aria-label="清空搜索"
                className="absolute right-4 top-1/2 size-6 -translate-y-1/2"
                onClick={() => setQuery("")}
                size="icon"
                variant="ghost"
              ><X className="size-3.5" /></Button>
            ) : null}
          </div>

          {bases.length > 1 ? (
            <div aria-label="知识库切换" className="flex flex-wrap gap-1 border-b p-2" role="group">
              {bases.map((base) => (
                <Button
                  aria-pressed={activeBaseNode?.id === base.id}
                  className="h-7 flex-1 px-2 text-xs"
                  key={base.id}
                  onClick={() => {
                    setActiveBase(base.id)
                    navigate(routeFromPath(baseHomeDoc(base.id)))
                  }}
                  size="sm"
                  title={base.description || base.name}
                  variant={activeBaseNode?.id === base.id ? "secondary" : "ghost"}
                >{base.name}</Button>
              ))}
            </div>
          ) : null}

          <div aria-label="文档类别筛选" className="grid grid-cols-3 gap-1 border-b p-2" role="group">
            {([
              ["all", "全部"] as const,
              ...CATEGORY_ORDER.map((category) => [category, CATEGORY_LABELS[category]] as const),
            ]).map(([value, label]) => (
              <Button
                aria-pressed={categoryFilter === value}
                className="h-7 px-2 text-xs"
                key={value}
                onClick={() => setCategoryFilter(value)}
                size="sm"
                variant={categoryFilter === value ? "secondary" : "ghost"}
              >{label}</Button>
            ))}
          </div>

          <nav className="min-h-0 flex-1 overflow-y-auto p-2 text-sm">
            {searchError ? (
              <p className="px-2 py-3 text-xs text-destructive">{searchError}</p>
            ) : null}
            {searching ? (
              searchHits.length ? groupHits(searchHits).map((group) => (
                <div className="mb-3" key={group.key}>
                  <p className="px-2 pb-1 text-[11px] uppercase tracking-wide text-muted-foreground">
                    {platformLabel(group.platform)} / {serviceLabel(group.service)}
                  </p>
                  {group.hits.map((hit) => (
                    <button
                      className={`flex w-full items-center gap-2 rounded px-2 py-1.5 text-left hover:bg-muted ${
                        hit.path === currentPath ? "bg-muted font-medium" : ""}`}
                      key={hit.path}
                      onClick={() => open(hit.path)}
                      type="button"
                    >
                      <StatusDot docType={hit.doc_type} status={hit.status} />
                      <span className="min-w-0 flex-1 truncate">{docProblemLabel(hit)}</span>
                      <Badge className="shrink-0" variant="outline">{categoryLabel(hit.category, hit.sop_kind)}</Badge>
                    </button>
                  ))}
                </div>
              )) : (
                <p className="px-2 py-3 text-xs text-muted-foreground">
                  {searchError ? "" : "无匹配文档"}
                </p>
              )
            ) : tree ? (activeBaseNode?.platforms ?? []).map((platform) => {
              const visiblePlatformDocs = platform.services.reduce(
                (total, service) => total + service.docs.filter((item) => matchesCategory(item.category)).length,
                0,
              )
              if (!visiblePlatformDocs) return null
              return (
              <div className="mb-2" key={platform.platform}>
                <div className="mb-1 flex items-center gap-1 px-2 py-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                  <button
                    aria-expanded={!collapsedPlatforms.has(platform.platform)}
                    aria-label={`${collapsedPlatforms.has(platform.platform) ? "展开" : "收起"} ${platformLabel(platform.platform)}`}
                    className="flex size-5 shrink-0 items-center justify-center rounded hover:bg-muted"
                    onClick={() => togglePlatform(platform.platform)}
                    type="button"
                  >
                    {collapsedPlatforms.has(platform.platform)
                      ? <ChevronRight className="size-3.5" />
                      : <ChevronDown className="size-3.5" />}
                  </button>
                  <span className="min-w-0 flex-1 truncate">{platformLabel(platform.platform)}</span>
                  <span className="font-normal">{visiblePlatformDocs}</span>
                </div>
                {!collapsedPlatforms.has(platform.platform) && platform.services.map((service) => {
                  const key = serviceKey(platform.platform, service.service)
                  const isOpen = expanded.has(key)
                  const visibleDocs = service.docs.filter((item) => matchesCategory(item.category))
                  if (!visibleDocs.length) return null
                  return (
                    <div key={key}>
                      <div className="flex items-center">
                        <button
                          aria-expanded={isOpen}
                          aria-label={`${isOpen ? "收起" : "展开"} ${serviceLabel(service.service)}`}
                          className="flex size-6 shrink-0 items-center justify-center rounded hover:bg-muted"
                          onClick={() => toggle(key)}
                          type="button"
                        >
                          {isOpen
                            ? <ChevronDown className="size-3.5 shrink-0 text-muted-foreground" />
                            : <ChevronRight className="size-3.5 shrink-0 text-muted-foreground" />}
                        </button>
                        {service.index_doc ? (
                          <button
                            aria-label={`打开 ${serviceLabel(service.service)} 服务概览`}
                            className="min-w-0 flex-1 truncate rounded px-1 py-1 text-left hover:bg-muted"
                            onClick={() => open(service.index_doc as string)}
                            title={service.index_title ?? `${serviceLabel(service.service)} 服务概览`}
                            type="button"
                          >{serviceLabel(service.service)}</button>
                        ) : (
                          <span className="min-w-0 flex-1 truncate px-1 py-1">
                            {serviceLabel(service.service)}
                          </span>
                        )}
                        <span className="pr-2 text-[11px] text-muted-foreground">{visibleDocs.length}</span>
                      </div>
                      {isOpen ? (
                        <div className="pb-1">
                          {visibleDocs.filter((item) => !item.component).map((item) => (
                            <TreeDocumentButton
                              currentPath={currentPath}
                              item={item}
                              key={item.path}
                              open={open}
                            />
                          ))}
                          {[...new Set(visibleDocs.map((item) => item.component).filter(Boolean))].map((component) => {
                            const componentName = component as string
                            const componentIndex = service.component_indexes.find(
                              (item) => item.component === componentName)
                            const componentDocs = visibleDocs.filter(
                              (item) => item.component === componentName)
                            const componentKey = `${key}/${componentName}`
                            const componentOpen = !collapsedComponents.has(componentKey)
                            return (
                              <div className="mt-1" key={componentName}>
                                <div className="ml-5 flex items-center">
                                  <button
                                    aria-expanded={componentOpen}
                                    aria-label={`${componentOpen ? "收起" : "展开"} ${componentLabel(componentName)}`}
                                    className="flex size-6 shrink-0 items-center justify-center rounded hover:bg-muted"
                                    onClick={() => toggleComponent(componentKey)}
                                    type="button"
                                  >
                                    {componentOpen
                                      ? <ChevronDown className="size-3.5 text-muted-foreground" />
                                      : <ChevronRight className="size-3.5 text-muted-foreground" />}
                                  </button>
                                  {componentIndex ? (
                                    <button
                                      aria-label={`打开 ${componentLabel(componentName)} 概览`}
                                      className={`min-w-0 flex-1 truncate rounded px-1 py-1 text-left text-[11px] font-medium text-muted-foreground hover:bg-muted ${
                                        componentIndex.index_doc === currentPath ? "bg-muted" : ""}`}
                                      onClick={() => open(componentIndex.index_doc)}
                                      title={componentIndex.index_title ?? `${componentLabel(componentName)} 概览`}
                                      type="button"
                                    >{componentLabel(componentName)}</button>
                                  ) : (
                                    <span className="min-w-0 flex-1 truncate px-1 py-1 text-[11px] font-medium text-muted-foreground">
                                      {componentLabel(componentName)}
                                    </span>
                                  )}
                                  <span className="pr-2 text-[11px] text-muted-foreground">{componentDocs.length}</span>
                                </div>
                                {componentOpen ? componentDocs.map((item) => (
                                  <TreeDocumentButton
                                    currentPath={currentPath}
                                    indent="pl-11"
                                    item={item}
                                    key={item.path}
                                    open={open}
                                  />
                                )) : null}
                              </div>
                            )
                          })}
                        </div>
                      ) : null}
                    </div>
                  )
                })}
              </div>
              )
            }) : <Skeleton className="h-40 w-full" />}
          </nav>
        </aside>

        <article className="min-h-0 overflow-y-auto rounded-md border p-6 lg:h-full" ref={articleRef}>
          {docError ? (
            <Alert variant="destructive"><AlertDescription>{docError}</AlertDescription></Alert>
          ) : docLoading && !doc ? (
            <div className="flex flex-col gap-3">
              <Skeleton className="h-6 w-1/3" /><Skeleton className="h-4 w-full" />
              <Skeleton className="h-4 w-5/6" />
            </div>
          ) : doc ? (
            <>
              <DocumentContent
                bases={bases}
                components={markdownComponents}
                currentPath={currentPath}
                doc={doc}
                plugins={plugins}
              />
              <ReferencePanel navigate={navigate} refs={refs} />
            </>
          ) : (
            <div className="flex h-full flex-col items-center justify-center gap-2 py-16 text-muted-foreground">
              <FileText className="size-8" />
              <p className="text-sm">从左侧选择一篇文档</p>
            </div>
          )}
        </article>

        <aside className="hidden min-h-0 xl:block">
          {doc ? (
            <div className="h-full min-h-0 overflow-y-auto pr-1 text-sm">
              <div className="flex flex-col gap-4">
                <DocumentOutline
                  headings={headings}
                  onSelect={(id) => scrollToHeading(articleRef.current, id)}
                />
                <div>
                  <p className="pb-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                    元信息
                  </p>
                  <dl className="flex flex-col gap-1.5 text-xs">
                    <div className="flex gap-2">
                      <dt className="w-14 shrink-0 text-muted-foreground">路径</dt>
                      <dd className="min-w-0 break-all font-mono">{doc.path}</dd>
                    </div>
                    {doc.meta.playbook ? (
                      <div className="flex gap-2">
                        <dt className="w-14 shrink-0 text-muted-foreground">playbook</dt>
                        <dd className="min-w-0 break-all font-mono">{doc.meta.playbook}</dd>
                      </div>
                    ) : null}
                    {doc.meta.schema_version ? (
                      <div className="flex gap-2">
                        <dt className="w-14 shrink-0 text-muted-foreground">契约</dt>
                        <dd>v{doc.meta.schema_version}</dd>
                      </div>
                    ) : null}
                    {doc.meta.source ? (
                      <div className="flex gap-2">
                        <dt className="w-14 shrink-0 text-muted-foreground">来源</dt>
                        <dd className="min-w-0 break-all">
                          {doc.meta.source === "triage" ? "中台排查" : "手动导入"}
                          {doc.meta.source_ref ? ` · ${doc.meta.source_ref}` : ""}
                        </dd>
                      </div>
                    ) : null}
                    {doc.meta.verified_at ? (
                      <div className="flex gap-2">
                        <dt className="w-14 shrink-0 text-muted-foreground">验证于</dt>
                        <dd className="min-w-0">{doc.meta.verified_at}</dd>
                      </div>
                    ) : null}
                    {doc.meta.verified_reason ? (
                      <div className="flex gap-2">
                        <dt className="w-14 shrink-0 text-muted-foreground">依据</dt>
                        <dd className="min-w-0">{doc.meta.verified_reason}</dd>
                      </div>
                    ) : null}
                    {doc.meta.symptoms.length ? (
                      <div className="flex gap-2">
                        <dt className="w-14 shrink-0 text-muted-foreground">症状</dt>
                        <dd className="min-w-0">{doc.meta.symptoms.join("、")}</dd>
                      </div>
                    ) : null}
                  </dl>
                </div>
              </div>
            </div>
          ) : null}
        </aside>
      </div>
      )}

      {isReaderFullscreen && doc ? (
        <div
          aria-label="全屏阅读"
          aria-modal="true"
          className="fixed inset-0 z-[100] bg-background/95 p-3 backdrop-blur-sm sm:p-5"
          role="dialog"
        >
          <div className="flex h-full min-h-0 flex-col overflow-hidden rounded-lg border bg-background shadow-2xl">
            <div className="flex shrink-0 items-center justify-between gap-4 border-b px-4 py-3">
              <div className="min-w-0">
                <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                  全屏阅读
                </p>
                <p className="truncate text-sm font-medium">{doc.meta.display_title}</p>
              </div>
              <Button
                aria-label="退出全屏阅读"
                className="shrink-0"
                onClick={() => setIsReaderFullscreen(false)}
                ref={fullscreenCloseRef}
                size="icon"
                title="退出全屏阅读"
                variant="ghost"
              ><X /></Button>
            </div>
            <div className="grid min-h-0 flex-1 grid-rows-[minmax(0,1fr)_auto] gap-5 p-4 lg:grid-cols-[minmax(0,1fr)_240px] lg:grid-rows-1 lg:p-6">
              <article
                className="min-h-0 overflow-y-auto pr-1 lg:pr-3"
                ref={fullscreenArticleRef}
              >
                <DocumentContent
                  bases={bases}
                  components={fullscreenMarkdownComponents}
                  currentPath={currentPath}
                  doc={doc}
                  plugins={plugins}
                />
                <ReferencePanel navigate={navigate} refs={refs} />
              </article>
              <aside className="max-h-40 min-h-0 overflow-y-auto border-t pt-4 lg:max-h-none lg:border-l lg:border-t-0 lg:pl-5 lg:pt-0">
                <DocumentOutline
                  headings={headings}
                  idPrefix="reader-fullscreen-"
                  onSelect={(id) => scrollToHeading(fullscreenArticleRef.current, id)}
                />
              </aside>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  )
}
