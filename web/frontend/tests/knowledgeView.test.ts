import assert from "node:assert/strict"
import test from "node:test"

import {
  baseHomeDoc,
  breadcrumb,
  CATEGORY_ORDER,
  categoryLabel,
  componentLabel,
  countDocs,
  docLabel,
  docProblemLabel,
  expandedKeyFor,
  extractHeadings,
  groupHits,
  headingId,
  indexNotice,
  isVerified,
  locateDoc,
  normalizeKnowledgePath,
  pathFromRoute,
  platformKeys,
  platformLabel,
  relationLabels,
  routeFromPath,
  setPlatformLabels,
  setServiceLabels,
  splitKnowledgeDocs,
  withoutGeneratedIndexMarkers,
  withoutRepeatedTitle,
} from "../src/lib/knowledgeView.ts"
import type { KnowledgeSearchHit, KnowledgeTree } from "../src/types.ts"


function hit(overrides: Partial<KnowledgeSearchHit>): KnowledgeSearchHit {
  return {
    path: "alpha/yarn/a.md", platform: "alpha", service: "yarn", doc_type: "troubleshoot",
    status: "draft", title: "a", display_title: "A", score: 1, snippet: "", ...overrides,
  }
}


test("route splat round-trips a document path", () => {
  assert.equal(pathFromRoute("alpha/yarn/nm-swap-gc.md"), "alpha/yarn/nm-swap-gc.md")
  assert.equal(routeFromPath("alpha/yarn/nm-swap-gc.md"), "/knowledge/alpha/yarn/nm-swap-gc.md")
})


test("stored knowledge paths accept both prefixed and relative formats", () => {
  assert.equal(normalizeKnowledgePath("knowledge/alpha/jobs/example.md"), "alpha/jobs/example.md")
  assert.equal(normalizeKnowledgePath("/knowledge/alpha/jobs/example.md"), "alpha/jobs/example.md")
  assert.equal(normalizeKnowledgePath("alpha/jobs/example.md"), "alpha/jobs/example.md")
})


test("route splat decodes percent-encoded segments", () => {
  assert.equal(pathFromRoute("alpha/yarn/%E4%B8%AD%E6%96%87.md"), "alpha/yarn/中文.md")
})


test("route splat rejects traversal and non-markdown", () => {
  // 后端也会拦，这里先挡一道免得白跑一次请求
  assert.equal(pathFromRoute("../secret.md"), null)
  assert.equal(pathFromRoute("/etc/passwd"), null)
  assert.equal(pathFromRoute("alpha/yarn/notes.txt"), null)
  assert.equal(pathFromRoute(""), null)
  assert.equal(pathFromRoute(undefined), null)
})


test("expanded key targets the service holding the current document", () => {
  assert.equal(expandedKeyFor("alpha/yarn/nm-swap-gc.md"), "alpha/yarn")
  assert.equal(expandedKeyFor(null), null)
})


test("document label uses the backend display title contract", () => {
  assert.equal(docLabel({ title: "alpha-yarn-pressure", doc_title: "YARN 资源压力", display_title: "YARN 资源压力" }), "YARN 资源压力")
  assert.equal(docLabel({ title: "alpha-yarn-pressure", doc_title: null, display_title: "YARN 服务基线（Alpha）" }), "YARN 服务基线（Alpha）")
})


test("directory label keeps only the problem portion of a structured title", () => {
  assert.equal(
    docProblemLabel({
      title: "alpha-yarn-pressure",
      doc_title: "YARN 资源压力",
      display_title: "Alpha - YARN - 资源压力",
    }),
    "资源压力",
  )
  assert.equal(
    docProblemLabel({
      title: "alpha-yarn-nm-pressure",
      display_title: "Alpha - YARN/NodeManager - 内存压力 - I/O 退化",
    }),
    "内存压力 - I/O 退化",
  )
})


test("component labels use the pack's service labels, else title-case the key", () => {
  assert.equal(componentLabel("namenode"), "Namenode")
  setServiceLabels({ namenode: "NameNode", zkfc: "ZKFC" })
  assert.equal(componentLabel("namenode"), "NameNode")
  assert.equal(componentLabel("zkfc"), "ZKFC")
  assert.equal(componentLabel("custom-worker"), "Custom Worker")
})


test("detail view hides only the leading H1 already shown by the page header", () => {
  assert.equal(
    withoutRepeatedTitle("\n# YARN 资源压力\n\n## 快速定位\n", "YARN 资源压力"),
    "\n## 快速定位\n",
  )
  assert.equal(
    withoutRepeatedTitle("前言\n# YARN 资源压力\n", "YARN 资源压力"),
    "前言\n# YARN 资源压力\n",
  )
})


test("managed index markers stay in source but not in the rendered document", () => {
  assert.equal(
    withoutGeneratedIndexMarkers(
      "before\n<!-- GENERATED:KB-INDEX:START -->\nbody\n<!-- GENERATED:KB-INDEX:END -->\nafter",
    ),
    "before\n\nbody\n\nafter",
  )
})


test("only verified status lights the green dot", () => {
  assert.equal(isVerified("verified"), true)
  assert.equal(isVerified("draft"), false)
  assert.equal(isVerified(null), false)
})


test("breadcrumb translates the platform segment and drops the filename", () => {
  setPlatformLabels({ alpha: "Alpha 平台", beta: "Beta 平台", shared: "跨平台" })
  assert.deepEqual(breadcrumb("alpha/hdfs/namenode/x.md"), ["Alpha 平台", "hdfs", "namenode"])
})


test("breadcrumb strips the knowledge base prefix and names the base when several exist", () => {
  const bases = [{ id: "cluster", name: "集群运维" }, { id: "jobs", name: "JobScheduler 任务" }]
  assert.deepEqual(breadcrumb("cluster/alpha/hdfs/x.md", bases), ["集群运维", "Alpha 平台", "hdfs"])
  assert.deepEqual(breadcrumb("jobs/task/x.md", bases), ["JobScheduler 任务", "task"])
  assert.deepEqual(breadcrumb("cluster/alpha/hdfs/x.md", [bases[0]]), ["Alpha 平台", "hdfs"])
})


test("locateDoc finds the owning base and service, including flat bases", () => {
  const doc = (path: string) => ({ path, title: path, doc_title: null, display_title: path, component: null, doc_type: "troubleshoot", status: "draft", problem: null, alerts: [] })
  const tree = {
    index_state: { state: "fresh" },
    warnings: [],
    bases: [
      { id: "cluster", name: "集群运维", description: "", layout: "platform_service", platforms: [
        { platform: "alpha", services: [{ service: "hdfs", index_doc: "cluster/alpha/hdfs/_index.md", index_title: null, component_indexes: [], docs: [doc("cluster/alpha/hdfs/a.md")] }] },
      ] },
      { id: "jobs", name: "JobScheduler 任务", description: "", layout: "flat", platforms: [
        { platform: "alpha", services: [{ service: "jobs", index_doc: "jobs/_index.md", index_title: null, component_indexes: [], docs: [doc("jobs/task/b.md")] }] },
      ] },
    ],
  } as unknown as KnowledgeTree
  assert.deepEqual(locateDoc(tree, "cluster/alpha/hdfs/a.md"), { baseId: "cluster", key: "alpha/hdfs" })
  assert.deepEqual(locateDoc(tree, "jobs/task/b.md"), { baseId: "jobs", key: "alpha/jobs" })
  assert.deepEqual(locateDoc(tree, "jobs/_index.md"), { baseId: "jobs", key: "alpha/jobs" })
  assert.equal(locateDoc(tree, "nowhere/x.md"), null)
  assert.equal(baseHomeDoc("jobs"), "jobs/_index.md")
  assert.equal(baseHomeDoc(""), "_index.md")
})


test("search hits group by service while keeping relevance order", () => {
  const groups = groupHits([
    hit({ path: "alpha/yarn/a.md" }),
    hit({ path: "beta/emqx/b.md", platform: "beta", service: "emqx" }),
    hit({ path: "alpha/yarn/c.md" }),
  ])
  assert.deepEqual(groups.map((group) => group.key), ["alpha/yarn", "beta/emqx"])
  assert.deepEqual(groups[0].hits.map((item) => item.path), ["alpha/yarn/a.md", "alpha/yarn/c.md"])
})


test("document count sums every service", () => {
  assert.equal(countDocs([
    { platform: "alpha", services: [
      { service: "yarn", index_doc: null, index_title: null, docs: [
        { path: "a", title: "a", doc_title: null, display_title: "A", doc_type: "troubleshoot", status: "draft", problem: null, alerts: [] },
        { path: "b", title: "b", doc_title: null, display_title: "B", doc_type: "troubleshoot", status: "draft", problem: null, alerts: [] },
      ] },
      { service: "hdfs", index_doc: null, index_title: null, docs: [] },
    ] },
  ]), 2)
})


test("headings skip fenced code so shell comments never become sections", () => {
  const headings = extractHeadings(
    "## 现象\n\n```bash\n## 这是注释\n```\n\n### 处置\n")
  assert.deepEqual(headings.map((item) => item.text), ["现象", "处置"])
  assert.deepEqual(headings.map((item) => item.depth), [2, 3])
})


test("duplicate headings get distinct anchor ids", () => {
  const headings = extractHeadings("## 排查\n## 排查\n")
  assert.deepEqual(headings.map((item) => item.id), ["排查", "排查-2"])
})


test("heading id strips markdown marks", () => {
  assert.equal(headingId("`free -m` 输出", new Set()), "free--m-输出")
})


test("missing index blocks search loudly, stale index only warns", () => {
  assert.deepEqual(indexNotice({
    available: false, built_at: null, source_latest_at: null, stale: true, hint: "跑 build_wiki",
  }), { level: "error", message: "搜索不可用：索引尚未生成。跑 build_wiki" })

  const stale = indexNotice({
    available: true, built_at: "x", source_latest_at: "y", stale: true, hint: "跑 build_wiki",
  })
  assert.equal(stale?.level, "warn")

  assert.equal(indexNotice({
    available: true, built_at: "x", source_latest_at: "y", stale: false, hint: null,
  }), null)
})


test("routed and consulted documents are split, never merged into one list", () => {
  // 此前两者同图标同字号逐行铺开，只能靠出现顺序区分
  const split = splitKnowledgeDocs("alpha/yarn/a.md", ["alpha/hdfs/b.md", "beta/emqx/c.md"])
  assert.deepEqual(split.matchedOnly, ["alpha/yarn/a.md"])
  assert.deepEqual(split.consultedOnly, ["alpha/hdfs/b.md", "beta/emqx/c.md"])
  assert.deepEqual(split.both, [])
})


test("a document that is both routed and consulted collapses into one entry", () => {
  const split = splitKnowledgeDocs("alpha/yarn/a.md", ["alpha/yarn/a.md", "alpha/hdfs/b.md"])
  assert.deepEqual(split.both, ["alpha/yarn/a.md"])
  assert.deepEqual(split.matchedOnly, [])
  assert.deepEqual(split.consultedOnly, ["alpha/hdfs/b.md"])
})


test("split handles no routing hit and deduplicates consulted docs", () => {
  const split = splitKnowledgeDocs(null, ["alpha/yarn/a.md", "alpha/yarn/a.md"])
  assert.deepEqual(split.matchedOnly, [])
  assert.deepEqual(split.both, [])
  assert.deepEqual(split.consultedOnly, ["alpha/yarn/a.md"])
})


test("relation labels are ordered by how much they say", () => {
  assert.deepEqual(relationLabels(["consulted", "produced", "matched"]),
    ["产出本文", "路由命中", "已查阅"])
  assert.deepEqual(relationLabels(["consulted"]), ["已查阅"])
})


test("five categories have fixed order and labels; SOP carries its kind", () => {
  assert.deepEqual(CATEGORY_ORDER, ["troubleshoot", "procedure", "cluster_info", "reference", "oplog"])
  assert.equal(categoryLabel("troubleshoot"), "故障处理")
  assert.equal(categoryLabel("procedure", "deploy"), "操作手册·部署")
  assert.equal(categoryLabel("procedure", "routine"), "操作手册·日常")
  assert.equal(categoryLabel("cluster_info"), "集群信息")
  assert.equal(categoryLabel("oplog"), "操作记录")
  assert.equal(categoryLabel(undefined), "参考知识")
})

test("platform labels come from the tree payload in declared order", () => {
  setPlatformLabels({ alpha: "Alpha 平台", mysql: "MySQL 集群", shared: "跨平台" })
  assert.deepEqual(platformKeys(), ["alpha", "mysql", "shared"])
  assert.equal(platformLabel("mysql"), "MySQL 集群")
  assert.equal(platformLabel("unknown"), "unknown")
  setPlatformLabels(undefined)
  assert.equal(platformLabel("mysql"), "MySQL 集群")
})
