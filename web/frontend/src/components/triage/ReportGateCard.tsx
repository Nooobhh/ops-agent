import { ShieldAlert } from "lucide-react"
import { ApprovalActions, type ApprovalActionsText } from "@/components/triage/ApprovalActions"
import { HypothesisView } from "@/components/triage/FinishSummaryCard"
import type { ApprovalDecision, KbReviewOutcome, ReportGateRequest } from "@/types"


const TEXT: ApprovalActionsText = {
  outcome: {
    approve: "已放行，报告按原引用落档",
    revise: "已打回，Agent 会按意见修正证据引用后重新收口",
    reject: "已终止，本次报告不落档",
  },
  autoTask: "自动任务的报告闸不在中台裁决。",
  feedbackLabel: "证据修正意见",
  feedbackPlaceholder: "说明证据引用要怎么改（例如指明该逐字引用哪段输出），Agent 会修正后重新收口",
  submitFeedback: "提交修正意见",
  reject: "终止任务",
  revise: "打回修改",
  approve: "放行落档",
}


type ReportGateCardProps = {
  request: ReportGateRequest
  outcome: KbReviewOutcome | null
  manual: boolean
  busy: boolean
  onDecision: (
    approvalId: string,
    decision: ApprovalDecision,
    feedback?: string,
  ) => Promise<void>
}


export function ReportGateCard({
  request,
  outcome,
  manual,
  busy,
  onDecision,
}: ReportGateCardProps) {
  return (
    <section className="triage-approval" aria-label="报告证据引用审核">
      <div className="flex items-start gap-3">
        <ShieldAlert className="mt-0.5 size-5 shrink-0 text-[#b7791f]" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <strong className="text-sm">
            {request.hypotheses ? "收口结论没有服务器事实依据，请人工裁决" : "报告证据引用未过校验，请人工裁决"}
          </strong>
          <p className="mt-1 whitespace-pre-wrap break-all text-[11px] text-muted-foreground">
            {request.reason}
          </p>
          {request.hypotheses ? (
            <div className="mt-3 max-h-96 space-y-3 overflow-y-auto border bg-background p-3 text-xs">
              <p className="whitespace-pre-wrap">
                <span className="font-medium">回答：</span>
                {request.answer}
              </p>
              {request.hypotheses.map((item, index) => <HypothesisView hypothesis={item} key={index} />)}
              {request.hypotheses.length === 0 ? (
                <p className="text-muted-foreground">未给出猜测，也没有可摘录的事实。</p>
              ) : null}
            </div>
          ) : (
          <div className="mt-3 max-h-96 space-y-2 overflow-y-auto border bg-background p-3 text-xs">
            <p>
              <span className="font-medium">结论：</span>
              {request.verdict} · {request.severity}
            </p>
            <p className="whitespace-pre-wrap">
              <span className="font-medium">根因：</span>
              {request.rootCause}
            </p>
            {request.evidence.length > 0 ? (
              <div>
                <span className="font-medium">证据引用：</span>
                <ul className="mt-1 list-disc space-y-1 pl-5">
                  {request.evidence.map((item, index) => (
                    <li className="whitespace-pre-wrap break-all" key={index}>{item}</li>
                  ))}
                </ul>
              </div>
            ) : null}
            {request.remediation.length > 0 ? (
              <div>
                <span className="font-medium">处置建议：</span>
                <ul className="mt-1 list-disc space-y-1 pl-5">
                  {request.remediation.map((item, index) => (
                    <li className="whitespace-pre-wrap break-all" key={index}>{item}</li>
                  ))}
                </ul>
              </div>
            ) : null}
          </div>
          )}

          <ApprovalActions
            approvalId={request.approvalId}
            busy={busy}
            manual={manual}
            onDecision={onDecision}
            outcome={outcome}
            text={TEXT}
          />
        </div>
      </div>
    </section>
  )
}
