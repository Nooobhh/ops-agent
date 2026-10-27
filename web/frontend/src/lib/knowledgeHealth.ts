import type { KnowledgeHealth } from "@/types"
// 相对路径 + .ts 后缀:node:test 直接跑 TS,不解析 vite 的 @ 别名
// (值 import 不像 type import 那样会被 strip 掉)
import { platformLabel } from "./knowledgeView.ts"

export type HealthFinding = {
  key: string
  level: "error" | "warn" | "info"
  title: string
  detail: string
  /** 面板只诊断；写入由排查 worker、本地导入器或人工晋升 CLI 执行。 */
  command: string | null
}

function percent(part: number, whole: number): string {
  if (!whole) return "0%"
  return `${Math.round((part / whole) * 100)}%`
}

export const BUILD_WIKI_COMMAND = ".venv/bin/python scripts/build_wiki.py"

/**
 * 把健康度指标翻成「问题 + 怎么修」。
 *
 * 排序按可行动性:索引问题一条命令就好，召回孤儿是批量补 frontmatter，
 * 晋升断链要改流程所以垫底 —— 它是给人看的诊断，不是待办。
 */
export function healthFindings(health: KnowledgeHealth | null): HealthFinding[] {
  if (!health) return []
  const findings: HealthFinding[] = []

  if (!health.index.available) {
    findings.push({
      key: "index-missing",
      level: "error",
      title: "检索索引未生成",
      detail: "全文搜索不可用。索引是本地派生物，不入 Git，新克隆的仓库需要先构建。",
      command: BUILD_WIKI_COMMAND,
    })
  } else if (health.index.stale) {
    findings.push({
      key: "index-stale",
      level: "warn",
      title: "检索索引早于最新文档",
      detail: "服务树与正文直读源文件所以始终最新，但搜索走索引，新写入的内容暂时搜不到。",
      command: BUILD_WIKI_COMMAND,
    })
  }

  const orphans = health.recall_orphans
  if (orphans.count) {
    findings.push({
      key: "recall-orphans",
      level: "warn",
      title: `${orphans.count} 篇排查文档召回不到`,
      detail: `占 troubleshoot 总数的 ${percent(orphans.count, orphans.troubleshoot_total)}`
        + "。symptoms 与 alerts 都为空，而召回排序最看重这两个字段——"
        + "文档在库里，但 agent 检索时基本命中不了。补上 frontmatter 后重建索引即可。",
      command: BUILD_WIKI_COMMAND,
    })
  }

  if (health.promotion_candidates.length) {
    findings.push({
      key: "promotion-candidates",
      level: "info",
      title: `${health.promotion_candidates.length} 篇草稿满足晋升条件`,
      detail: "后续独立 conclusive、历史案例、平台/服务和内容呼应检查均已通过。新版中台写入会自动晋升；残留草稿可人工复核后补晋升。",
      command: ".venv/bin/python -m lib.kb promote <doc> --reason \"<人工确认理由>\" [--run-id <run-id>]",
    })
  }

  const blocked = health.promotion_reviews.filter((item) => !item.eligible)
  if (blocked.length) {
    findings.push({
      key: "promotion-blocked",
      level: "info",
      title: `${blocked.length} 篇被引用草稿暂不满足晋升条件`,
      detail: "路由命中或查阅本身不算验证。展开后可查看缺少后续报告、历史案例或内容呼应等具体原因。",
      command: null,
    })
  }

  return findings
}

export type CoverageRow = {
  platform: string
  label: string
  documents: number
  troubleshoot: number
  withAlerts: number
  draft: number
  verified: number
  verifiedRatio: string
}

export function coverageRows(health: KnowledgeHealth | null): CoverageRow[] {
  if (!health) return []
  return health.platform_coverage.map((row) => ({
    platform: row.platform,
    label: platformLabel(row.platform),
    documents: row.documents,
    troubleshoot: row.troubleshoot,
    withAlerts: row.with_alerts,
    draft: row.draft,
    verified: row.verified,
    // 基础 knowledge 不参与状态生命周期，已验证比例只在可晋升文档中计算。
    verifiedRatio: percent(row.verified, row.draft + row.verified),
  }))
}

export function verifiedRatioLabel(health: KnowledgeHealth | null): string {
  if (!health) return "-"
  const { draft, verified } = health.status_counts
  return `${verified} / ${draft + verified}`
}
