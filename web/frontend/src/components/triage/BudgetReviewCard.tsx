import { CheckCircle2, Gauge, XCircle } from "lucide-react"
import { Button } from "@/components/ui/button"
import type { ApprovalDecision, BudgetReviewRequest, KbReviewOutcome } from "@/types"


type BudgetReviewCardProps = {
  request: BudgetReviewRequest
  outcome: KbReviewOutcome | null
  manual: boolean
  busy: boolean
  onDecision: (
    approvalId: string,
    decision: ApprovalDecision,
    feedback?: string,
  ) => Promise<void>
}


export function BudgetReviewCard({
  request,
  outcome,
  manual,
  busy,
  onDecision,
}: BudgetReviewCardProps) {
  return (
    <section className="triage-approval" aria-label="排查预算续期确认">
      <div className="flex items-start gap-3">
        <Gauge className="mt-0.5 size-5 shrink-0 text-[#b7791f]" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <strong className="text-sm">排查预算已用尽，是否继续？</strong>
          <p className="mt-1 text-xs text-muted-foreground">{request.reason}</p>
          <dl className="triage-approval-facts">
            <div>
              <dt>轮次</dt>
              <dd>{request.toolTurns} / {request.turnBudget}</dd>
            </div>
            <div>
              <dt>诊断</dt>
              <dd>{request.diagnosticCalls} / {request.diagnosticBudget}</dd>
            </div>
            {request.grants ? (
              <div><dt>已续期</dt><dd>{request.grants} 次</dd></div>
            ) : null}
            <div>
              <dt>报告</dt>
              <dd>{request.hasReport ? "已生成，可随时收口" : "尚未生成"}</dd>
            </div>
          </dl>
          <p className="mt-2 text-xs text-muted-foreground">
            继续将再放宽 {request.grantTurns} 轮工具调用；停止则按当前进度收尾
            {request.hasReport ? "（已有报告会保留）" : "（无报告，草稿落 artifacts）"}。
          </p>

          {outcome ? (
            <p className={`mt-4 flex items-center gap-2 text-sm ${
              outcome.decision === "approve" ? "text-[#247a56]" : "text-muted-foreground"
            }`}>
              {outcome.decision === "approve"
                ? <CheckCircle2 className="size-4" />
                : <XCircle className="size-4" />}
              <span>
                {outcome.decision === "approve"
                  ? `已同意续期，Agent 继续排查`
                  : "已确认停止，任务按当前进度收尾"}
              </span>
            </p>
          ) : !manual ? (
            <p className="mt-4 text-xs text-muted-foreground">
              自动任务不申请续期，预算用尽即终止。
            </p>
          ) : (
            <div className="mt-4 flex flex-wrap justify-end gap-2 border-t pt-4">
              <Button
                disabled={busy}
                onClick={() => void onDecision(request.approvalId, "reject")}
                size="sm"
                variant="outline"
              >
                停止排查
              </Button>
              <Button
                disabled={busy}
                onClick={() => void onDecision(request.approvalId, "approve")}
                size="sm"
              >
                继续排查
              </Button>
            </div>
          )}
        </div>
      </div>
    </section>
  )
}
