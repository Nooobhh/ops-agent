import { useState } from "react"
import { ChevronDown, ChevronRight, Stethoscope } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { coverageRows, healthFindings, verifiedRatioLabel } from "@/lib/knowledgeHealth"
import type { KnowledgeHealth } from "@/types"


const LEVEL_STYLE: Record<string, string> = {
  error: "border-destructive/40 bg-destructive/10",
  warn: "border-[#d69e2e]/40 bg-[#d69e2e]/10",
  info: "border-border bg-muted/40",
}


function DocList({ paths, onOpenDocument }: {
  paths: { path: string; title: string }[]
  onOpenDocument: (path: string) => void
}) {
  const [expanded, setExpanded] = useState(false)
  const visible = expanded ? paths : paths.slice(0, 5)
  return (
    <div className="mt-2">
      <ul className="flex flex-col gap-0.5">
        {visible.map((item) => (
          <li key={item.path}>
            <button
              className="break-all text-left font-mono text-[11px] text-[var(--link)] hover:underline"
              onClick={() => onOpenDocument(item.path)}
              type="button"
            >{item.title}</button>
            <span className="ml-2 break-all font-mono text-[10px] text-muted-foreground">
              {item.path}
            </span>
          </li>
        ))}
      </ul>
      {paths.length > 5 ? (
        <Button
          className="mt-1 h-auto p-0 text-[11px]"
          onClick={() => setExpanded(!expanded)}
          size="sm"
          variant="link"
        >{expanded ? "收起" : `展开全部 ${paths.length} 篇`}</Button>
      ) : null}
    </div>
  )
}


function PromotionReviews({ health, onOpenDocument }: {
  health: KnowledgeHealth
  onOpenDocument: (path: string) => void
}) {
  return (
    <ul className="mt-2 flex flex-col gap-2 text-[11px]">
      {health.promotion_reviews.filter((item) => !item.eligible).map((item) => {
        const attempt = item.attempts[0]
        return (
          <li key={item.path}>
            <button
              className="text-left text-[var(--link)] hover:underline"
              onClick={() => onOpenDocument(item.path)}
              type="button"
            >
              {item.title}
            </button>
            <p className="mt-0.5 text-muted-foreground">
              {attempt
                ? `${attempt.run_id}：${attempt.reasons.join("；")}`
                : "没有可审查的后续排查"}
            </p>
          </li>
        )
      })}
    </ul>
  )
}


function PromotionCandidates({ health, onOpenDocument }: {
  health: KnowledgeHealth
  onOpenDocument: (path: string) => void
}) {
  return (
    <ul className="mt-2 flex flex-col gap-3 text-[11px]">
      {health.promotion_candidates.map((item) => {
        const attempt = item.attempts.find((value) => value.eligible)
        return (
          <li key={item.path}>
            <button
              className="text-left text-[var(--link)] hover:underline"
              onClick={() => onOpenDocument(item.path)}
              type="button"
            >
              {item.title}
            </button>
            <p className="mt-0.5 text-muted-foreground">
              验证 run：<span className="font-mono">{item.run_id}</span>
              {attempt?.explicit_doc_reference ? " · 报告明确引用本文" : ""}
              {attempt?.matched_terms.length
                ? ` · 呼应词：${attempt.matched_terms.join("、")}`
                : ""}
            </p>
            <pre className="mt-1 overflow-x-auto bg-[var(--pre-bg)] px-2 py-1.5 font-mono text-[11px] text-[var(--pre-fg)]">
              {`.venv/bin/python -m lib.kb promote ${item.path} --reason "人工复核通过" --run-id ${item.run_id}`}
            </pre>
          </li>
        )
      })}
    </ul>
  )
}


/** 知识库健康度面板只诊断；写入由排查 worker 或本地导入器负责。 */
export function KnowledgeHealthPanel({ health, onOpenDocument }: {
  health: KnowledgeHealth | null
  onOpenDocument: (path: string) => void
}) {
  const [open, setOpen] = useState(false)
  if (!health) return null

  const findings = healthFindings(health)
  const rows = coverageRows(health)
  const worst = findings.some((item) => item.level === "error")
    ? "error"
    : findings.some((item) => item.level === "warn") ? "warn" : "info"

  return (
    <section className="rounded-md border">
      <button
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-4 py-2.5 text-left"
        onClick={() => setOpen(!open)}
        type="button"
      >
        {open
          ? <ChevronDown className="size-4 shrink-0 text-muted-foreground" />
          : <ChevronRight className="size-4 shrink-0 text-muted-foreground" />}
        <Stethoscope className="size-4 shrink-0 text-muted-foreground" />
        <span className="text-sm font-medium">知识库健康度</span>
        <span className="text-xs text-muted-foreground">
          已验证 {verifiedRatioLabel(health)}
        </span>
        {findings.length ? (
          <Badge
            className="ml-auto"
            variant={worst === "error" ? "destructive" : "secondary"}
          >{findings.length} 项待处理</Badge>
        ) : (
          <Badge className="ml-auto" variant="secondary">无异常</Badge>
        )}
      </button>

      {open ? (
        <div className="border-t px-4 py-4">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[520px] border-collapse text-xs">
              <thead>
                <tr className="text-left text-muted-foreground">
                  <th className="border-b py-1.5 pr-3 font-medium">平台</th>
                  <th className="border-b py-1.5 pr-3 font-medium">文档</th>
                  <th className="border-b py-1.5 pr-3 font-medium">排查类</th>
                  <th className="border-b py-1.5 pr-3 font-medium">绑定告警</th>
                  <th className="border-b py-1.5 pr-3 font-medium">已验证</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.platform}>
                    <td className="border-b py-1.5 pr-3">{row.label}</td>
                    <td className="border-b py-1.5 pr-3">{row.documents}</td>
                    <td className="border-b py-1.5 pr-3">{row.troubleshoot}</td>
                    <td className="border-b py-1.5 pr-3">{row.withAlerts}</td>
                    <td className="border-b py-1.5 pr-3">
                      {row.verified}
                      <span className="ml-1 text-muted-foreground">({row.verifiedRatio})</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {findings.map((finding) => (
            <div
              className={`mt-3 rounded border px-3 py-2.5 ${LEVEL_STYLE[finding.level]}`}
              key={finding.key}
            >
              <p className="text-xs font-medium">{finding.title}</p>
              <p className="mt-1 text-xs text-muted-foreground">{finding.detail}</p>
              {finding.command ? (
                <pre className="mt-2 overflow-x-auto bg-[var(--pre-bg)] px-2 py-1.5 font-mono text-[11px] text-[var(--pre-fg)]">
                  {finding.command}
                </pre>
              ) : null}
              {finding.key === "recall-orphans" ? (
                <DocList onOpenDocument={onOpenDocument} paths={health.recall_orphans.documents} />
              ) : null}
              {finding.key === "promotion-candidates" ? (
                <PromotionCandidates health={health} onOpenDocument={onOpenDocument} />
              ) : null}
              {finding.key === "promotion-blocked" ? (
                <PromotionReviews health={health} onOpenDocument={onOpenDocument} />
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
    </section>
  )
}
