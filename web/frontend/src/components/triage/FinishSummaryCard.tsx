import { CircleCheck, CircleHelp, CircleSlash, ClipboardCheck } from "lucide-react"
import type { FinishEvidence, FinishHypothesis, FinishStatus, FinishSummary } from "@/types"


const STATUS = {
  supported: {label: "成立", className: "border-[#2f855a]/40 bg-[#2f855a]/10 text-[#276749]", icon: CircleCheck},
  insufficient: {label: "缺少证据", className: "border-[#d69e2e]/50 bg-[#d69e2e]/10 text-[#975a16]", icon: CircleHelp},
  refuted: {label: "推翻", className: "border-slate-400/50 bg-slate-500/10 text-slate-700 dark:text-slate-300", icon: CircleSlash},
} satisfies Record<FinishStatus, {label: string; className: string; icon: typeof CircleCheck}>

const ROLE_LABELS: Record<string, string> = {
  observation: "异常观测",
  criterion: "判据",
  absence: "反证",
  related: "旁证",
  fact: "事实",
}


export function FinishStatusBadge({status}: {status: FinishStatus}) {
  const value = STATUS[status]
  const Icon = value.icon
  return (
    <span className={`inline-flex shrink-0 items-center gap-1 rounded border px-1.5 py-0.5 text-[11px] font-medium ${value.className}`}>
      <Icon className="size-3" aria-hidden="true" />
      {value.label}
    </span>
  )
}


function EvidenceLine({item}: {item: FinishEvidence}) {
  return (
    <li className="min-w-0">
      <span className="text-[11px] text-muted-foreground">
        {ROLE_LABELS[item.role] || item.role || "证据"} · <span className="font-mono">{item.ref}</span>
        {item.found ? null : <span className="ml-1 text-[#975a16]">未摘录:{item.reason || "原文不在该步输出中"}</span>}
      </span>
      {item.found && item.text ? (
        <pre className="mt-0.5 whitespace-pre-wrap break-all rounded bg-muted/60 px-2 py-1 font-mono text-[11px]">{item.text}</pre>
      ) : null}
    </li>
  )
}


export function HypothesisView({hypothesis}: {hypothesis: FinishHypothesis}) {
  const downgraded = hypothesis.declared && hypothesis.declared !== hypothesis.status
  return (
    <div className="min-w-0 space-y-1.5">
      <div className="flex items-start gap-2">
        <FinishStatusBadge status={hypothesis.status} />
        <span className="min-w-0 whitespace-pre-wrap break-words text-sm">{hypothesis.statement}</span>
      </div>
      {downgraded || hypothesis.reasons.length ? (
        <p className="text-[11px] text-[#975a16]">
          {downgraded ? `模型判为${STATUS[hypothesis.declared!].label},已降级:` : "未满足:"}
          {hypothesis.reasons.join(";")}
        </p>
      ) : null}
      {hypothesis.evidence.length || hypothesis.related.length ? (
        <ul className="space-y-1 border-l pl-3">
          {[...hypothesis.evidence, ...hypothesis.related].map((item, index) => (
            <EvidenceLine item={item} key={`${item.ref}-${index}`} />
          ))}
        </ul>
      ) : null}
    </div>
  )
}


export function FinishSummaryCard({summary}: {summary: FinishSummary}) {
  return (
    <section aria-label="收口结论核验" className="rounded-md border bg-card p-3">
      <div className="flex flex-wrap items-center gap-2">
        <ClipboardCheck className="size-4 text-muted-foreground" aria-hidden="true" />
        <strong className="text-sm">收口核验</strong>
        <span className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${summary.grounded
          ? "bg-[#2f855a]/10 text-[#276749]"
          : "bg-[#d69e2e]/15 text-[#975a16]"}`}
        >
          {summary.grounded ? "结论有服务器事实依据" : "结论缺少服务器事实依据"}
        </span>
        <span className="text-[11px] text-muted-foreground">verdict {summary.verdict}</span>
      </div>
      <p className="mt-1 text-[11px] text-muted-foreground">
        证据原文由 Harness 从执行记录摘录;成立需故障窗口内的异常观测与判据,推翻需两个不同命令的窗口内反证。
      </p>
      {summary.hypotheses.length ? (
        <div className="mt-3 space-y-3">
          {summary.hypotheses.map((item, index) => <HypothesisView hypothesis={item} key={index} />)}
        </div>
      ) : null}
      {summary.facts.length ? (
        <div className="mt-3">
          <span className="text-xs font-medium">支撑回答的事实</span>
          <ul className="mt-1 space-y-1 border-l pl-3">
            {summary.facts.map((item, index) => <EvidenceLine item={item} key={`${item.ref}-${index}`} />)}
          </ul>
        </div>
      ) : null}
    </section>
  )
}
