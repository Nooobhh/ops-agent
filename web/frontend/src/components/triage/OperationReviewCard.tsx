import { useEffect, useState } from "react"
import { ShieldAlert } from "lucide-react"
import { ApprovalActions, type ApprovalActionsText } from "@/components/triage/ApprovalActions"
import {
  operationOutcomeTone,
  operationPhaseLabel,
  planRemainingSeconds,
} from "@/lib/triageView"
import type {
  ApprovalDecision,
  KbReviewOutcome,
  OperationExecution,
  OperationReviewRequest,
} from "@/types"


const TEXT: ApprovalActionsText = {
  outcome: {
    approve: "已批准,由 Harness 签发令牌后执行",
    revise: "已打回,Agent 会按意见调整后重新提出",
    reject: "已拒绝,不执行",
  },
  autoTask: "自动任务没有人工审批通道,写操作一律驳回。",
  feedbackLabel: "打回意见",
  feedbackPlaceholder: "说明要怎么调整(换目标、换时间、先做什么),Agent 会据此重新提出计划",
  submitFeedback: "提交打回意见",
  reject: "拒绝",
  revise: "打回",
  approve: "批准执行",
}


type OperationReviewCardProps = {
  request: OperationReviewRequest
  outcome: KbReviewOutcome | null
  decidedBy: string | null
  execution: OperationExecution | null
  manual: boolean
  busy: boolean
  onDecision: (
    approvalId: string,
    decision: ApprovalDecision,
    feedback?: string,
  ) => Promise<void>
}


function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return undefined
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [active])
  return now
}


function duration(seconds: number): string {
  if (seconds >= 60) return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
  return `${seconds} 秒`
}


/** 写操作审批卡:计划、预检状态、影响面与裁决;批准后同一张卡显示执行与跟踪结论。 */
export function OperationReviewCard({
  request,
  outcome,
  decidedBy,
  execution,
  manual,
  busy,
  onDecision,
}: OperationReviewCardProps) {
  const pending = outcome === null
  const now = useNow(pending)
  const remaining = planRemainingSeconds(request.expiresAt, now)
  const tone = execution?.outcome ? operationOutcomeTone(execution.outcome) : null

  return (
    <section className="triage-approval" aria-label="写操作审批">
      <div className="flex items-start gap-3">
        <ShieldAlert className="mt-0.5 size-5 shrink-0 text-[#b7791f]" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <strong className="text-sm">{pending ? "写操作待审批" : "写操作"} · {request.title || "-"}</strong>
            <span className="font-mono text-[11px] text-muted-foreground">{request.planId}</span>
          </div>
          {pending ? (
            <p className="mt-1 text-xs text-muted-foreground">
              {remaining !== null
                ? `计划 ${duration(remaining)} 内有效,批准后由 Harness 签发令牌执行;模型拿不到令牌`
                : "计划已过期:批准时 Harness 会用原参数重新预检,再出一张卡片确认"}
            </p>
          ) : null}
          {request.replannedFrom ? (
            <p className="mt-2 text-xs text-[#9a6518]">
              原计划已失效({request.replanReason || "过期"}),这是用原参数重新预检的计划,需要再次确认。
            </p>
          ) : null}

          <dl className="triage-approval-facts">
            <div><dt>插件</dt><dd>{request.plugin}{request.cluster ? ` · 集群 ${request.cluster}` : ""}</dd></div>
            <div><dt>理由</dt><dd>{request.reason || "Agent 未说明"}</dd></div>
            <div><dt>预计耗时</dt><dd>最长约 {duration(request.expectedMaxDurationS)}</dd></div>
            <div><dt>计划</dt><dd className="font-mono">{request.planHash.slice(0, 16)}</dd></div>
          </dl>

          {request.summary.length ? (
            <ul className="mt-3 list-inside list-disc text-xs">
              {request.summary.map((line, index) => <li key={index}>{line}</li>)}
            </ul>
          ) : null}

          {request.current.length ? (
            <div className="mt-3 text-xs">
              <p className="text-muted-foreground">预检时状态</p>
              <div className="triage-operation-rows">
                {request.current.map((item, index) => (
                  <div key={`${item.host}-${item.component}-${index}`}>
                    <span className="font-mono">{item.component}@{item.host}</span>
                    <span className="text-muted-foreground">
                      实际 {item.state || "-"} · 目标 {item.desiredState || "-"} · 维护模式 {item.maintenance || "-"}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          ) : null}

          <div className="mt-3 text-xs">
            <p className="text-muted-foreground">影响面</p>
            {request.pendingChanges.length ? (
              <details className="mt-1">
                <summary className="cursor-pointer">本次操作会让 {request.pendingChanges.length} 项已修改的配置生效</summary>
                <ul className="mt-1 list-inside list-disc font-mono text-[11px]">
                  {request.pendingChanges.map((line, index) => <li className="break-all" key={index}>{line}</li>)}
                </ul>
              </details>
            ) : <p className="mt-1">没有待生效的配置变更</p>}
            {request.impact ? (
              request.impact.ok ? (
                <p className="mt-1">
                  下游服务:{request.impact.downstream.length ? request.impact.downstream.join("、") : "无"}
                  {request.impact.upstream.length ? `(依赖上游 ${request.impact.upstream.join("、")})` : ""}
                </p>
              ) : <p className="mt-1 text-muted-foreground">下游影响未取得:{request.impact.error || "查询失败"}</p>
            ) : null}
          </div>

          {request.warnings.length ? (
            <ul className="mt-3 list-inside list-disc text-xs text-[#9a6518]">
              {request.warnings.map((line, index) => <li key={index}>{line}</li>)}
            </ul>
          ) : null}

          <details className="mt-3 text-xs">
            <summary className="cursor-pointer break-all text-muted-foreground">
              将发出的请求:{request.request.method} {request.request.path}
            </summary>
            <pre className="mt-2 max-h-60 overflow-auto whitespace-pre-wrap break-words bg-[var(--pre-bg)] p-3 font-mono text-[11px] leading-5 text-[var(--pre-fg)]">
              {request.request.body || "-"}
            </pre>
          </details>

          <ApprovalActions
            approvalId={request.approvalId}
            busy={busy}
            manual={manual}
            onDecision={onDecision}
            outcome={outcome}
            text={TEXT}
          />
          {outcome && decidedBy ? (
            <p className="mt-1 text-xs text-muted-foreground">审批人:{decidedBy}</p>
          ) : null}

          {execution ? (
            <div className="triage-operation-exec">
              <p className={`font-medium ${tone ? `triage-operation-tone-${tone}` : ""}`}>
                {operationPhaseLabel(execution)}
              </p>
              {execution.reason ? <p className="mt-1 break-words">{execution.reason}</p> : null}
              {execution.tasks.length ? (
                <div className="triage-operation-rows">
                  {execution.tasks.map((item, index) => (
                    <div key={`${item.host}-${item.component}-${index}`}>
                      <span className="font-mono">{item.component}@{item.host}</span>
                      <span className="text-muted-foreground">
                        {item.status}{item.exitCode ? ` · exit ${item.exitCode}` : ""}
                        {item.durationS !== null ? ` · ${Math.round(item.durationS)}s` : ""}
                      </span>
                    </div>
                  ))}
                </div>
              ) : null}
              {execution.tokenDigest ? (
                <p className="mt-1 text-muted-foreground">令牌摘要 <span className="font-mono">{execution.tokenDigest}</span></p>
              ) : null}
              {execution.oplog ? (
                <p className="mt-1 break-all">oplog:<span className="font-mono">{execution.oplog}</span></p>
              ) : execution.oplogError ? (
                <p className="mt-1 text-destructive">oplog 未生成:{execution.oplogError}(收口前会再试)</p>
              ) : null}
            </div>
          ) : null}
        </div>
      </div>
    </section>
  )
}
